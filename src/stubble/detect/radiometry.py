"""M2.1 haze masking + relative radiometric normalisation (one pass over the cached S2 views).

Per acquisition file (one S2 platform pass on one date):

1. **Haze loss.** Inside the AOI, of the pixels clear by SCL: share flagged by the blue test
   (``quality.haze_blue_threshold``), by the cloud-proximity buffer, by Sen2Cor AOT above
   ``haze_mask.haze_aot_threshold``, and the share left usable.
2. **Pseudo-invariant pixels (PIFs).** ESA WorldCover built-up / bare pixels (eroded), never
   green and spectrally stable over the usable views of the season. A seeded random sample of
   candidates is read; the reference value of each PIF is its season median per band.
3. **Offsets.** Per file and band: median over usable PIFs of ``value - reference``. The
   detector subtracts it from every pixel before computing indices (additive correction).
4. **Scene exclusion** (rule fixed in config before the run): a date is dropped when, after
   haze masking, fewer than ``scene_min_usable_frac`` of its SCL-clear AOI pixels remain, or
   when its PIF residual spread in any band exceeds ``scene_max_pif_spread_factor`` x the
   median spread across files. Dropped dates are skipped like hazy views (windows widen).

Output: ``data/processed/m21/radiometry.json`` (+ provenance). Offsets in reflectance units.
"""

from __future__ import annotations

import json
import logging
import warnings
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window as RioWindow
from scipy.ndimage import binary_erosion

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.data.ancillary import AOT_SCALE, aot_path, worldcover_path
from stubble.data.composites import NODATA, scl_valid
from stubble.detect.indices import near_class
from stubble.detect.timeseries import _s2_days
from stubble.provenance import is_cached, read_provenance, write_provenance

log = logging.getLogger(__name__)

AOT_BINS = np.arange(0, 3.01, 0.01)


def radiometry_path(cfg: Config) -> Path:
    return cfg.path("processed", "m21", "radiometry.json")


def haze_components(b02: np.ndarray, scl: np.ndarray, aot: np.ndarray | None, cfg: Config,
                    radius_px: int) -> dict[str, np.ndarray]:
    """Per-pixel haze tests on one view (full arrays incl. halo).

    ``aot`` is int16 x1000 (``NODATA`` = missing; missing AOT never flags).
    """
    q, s2, hm = cfg.quality, cfg.sentinel2, cfg.haze_mask
    out = {"blue": (b02 != NODATA) & (b02 > q.haze_blue_threshold * s2.reflectance_scale),
           "buffer": near_class(scl, q.haze_cloud_classes, radius_px)}
    if aot is not None and hm.haze_aot_threshold is not None:
        out["aot"] = (aot != NODATA) & (aot > hm.haze_aot_threshold * AOT_SCALE)
    else:
        out["aot"] = np.zeros(b02.shape, bool)
    return out


def robust_spread(x: np.ndarray, axis: int | None = None) -> np.ndarray:
    """``(p84 - p16) / 2``, a sigma-like spread that ignores NaN and outliers."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        p16, p84 = np.nanpercentile(x, [16, 84], axis=axis)
    return (p84 - p16) / 2


def pif_candidates(cfg: Config, grid: GridSpec) -> np.ndarray:
    """Boolean grid: WorldCover PIF classes, each class eroded by ``erode_px``."""
    rc = cfg.radiometry
    with rasterio.open(worldcover_path(cfg)) as ds:
        wc = ds.read(1)
    out = np.zeros(wc.shape, bool)
    for c in rc.worldcover_classes:
        m = wc == c
        out |= binary_erosion(m, iterations=rc.erode_px) if rc.erode_px > 0 else m
    return out


def select_pifs(vals: np.ndarray, ndvi: np.ndarray, min_views: int, ndvi_max: float,
                max_rel_spread: float) -> np.ndarray:
    """Pick stable pixels from ``vals (files, bands, px)`` (NaN = unusable view).

    Stable = at least ``min_views`` usable views, season NDVI max below ``ndvi_max`` and, in
    every band, robust spread / median below ``max_rel_spread``.
    """
    n = np.isfinite(vals[:, 0]).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        nd_max = np.nanmax(ndvi, axis=0)
        med = np.nanmedian(vals, axis=0)
    rel = robust_spread(vals, axis=0) / np.abs(med)
    ok = (n >= min_views) & (nd_max < ndvi_max) & np.all(rel < max_rel_spread, axis=0)
    return ok & np.isfinite(med).all(axis=0)


def offsets_from_pifs(vals: np.ndarray, min_px: int) -> tuple[np.ndarray, np.ndarray,
                                                             np.ndarray]:
    """Per-file, per-band median deviation of PIFs from their season median.

    ``vals``: ``(files, bands, pifs)``. Returns offsets, residual spreads (after removing the
    offset) and usable PIF counts. Files with fewer than ``min_px`` usable PIFs get offset 0
    and NaN spread.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ref = np.nanmedian(vals, axis=0)
        dev = vals - ref[None]
        off = np.nanmedian(dev, axis=2)
    n = np.isfinite(dev[:, 0]).sum(axis=1)
    spread = robust_spread(dev - off[:, :, None], axis=2)
    few = n < min_px
    off[few] = 0.0
    spread[few] = np.nan
    return off, spread, n


def decide_exclusions(days: list[str], usable_frac: dict[str, float],
                      spread: np.ndarray, min_frac: float, factor: float,
                      manual: list[str]) -> dict[str, list[str]]:
    """Apply the pre-registered scene rule. ``spread`` is ``(files, bands)`` (file = day row).

    Returns ``{day: [reasons]}`` for excluded days.
    """
    out: dict[str, list[str]] = defaultdict(list)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(spread, axis=0)
    for i, d in enumerate(days):
        if usable_frac.get(d, 1.0) < min_frac:
            out[d].append(f"usable {usable_frac[d]:.1%} of SCL-clear < {min_frac:.0%}")
        bad = np.flatnonzero(spread[i] > factor * med)
        if bad.size:
            out[d].append(f"PIF residual spread > {factor:g}x median in band idx {bad.tolist()}")
    for d in manual:
        out[d].append("manual (haze_mask.exclude_days)")
    return dict(out)


def run_radiometry(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray, force: bool = False,
                   block_rows: int = 1024) -> dict[str, Any]:
    """Compute haze loss, PIF offsets and scene exclusions; write ``radiometry.json``."""
    rc, q, s2, hm = cfg.radiometry, cfg.quality, cfg.sentinel2, cfg.haze_mask
    if rc is None:
        raise ValueError("config has no `radiometry` section")
    by_day = _s2_days(cfg)
    files = [p for ps in by_day.values() for p in ps]
    out = radiometry_path(cfg)
    params = {"radiometry": rc.model_dump(), "haze_mask": hm.model_dump(mode="json"),
              "quality": q.model_dump(), "scl_invalid": s2.scl_invalid,
              "inputs": [p.name for p in files],
              "grid": [grid.crs, list(grid.transform)[:6], grid.width, grid.height]}
    if not force and is_cached(out, params):
        return json.loads(out.read_text(encoding="utf-8"))

    cand = pif_candidates(cfg, grid)
    flat = np.flatnonzero(cand.ravel())
    rng = np.random.default_rng(rc.seed)
    if flat.size > rc.sample_px:
        flat = np.sort(rng.choice(flat, rc.sample_px, replace=False))
    rows, cols = np.unravel_index(flat, cand.shape)
    log.info("PIF candidates: %d px (%d sampled)", int(cand.sum()), flat.size)
    del cand

    bands = list(rc.bands)
    nb = len(bands)
    scale = s2.reflectance_scale
    vals = np.full((len(files), nb, flat.size), np.nan, np.float32)
    ndvi = np.full((len(files), flat.size), np.nan, np.float32)
    radius = round(q.haze_cloud_buffer_m / grid.resolution)
    keys = ("aoi_valid", "scl_clear", "blue", "buffer", "aot", "aot_only", "hazy", "usable",
            "aot_missing")
    scene: list[dict[str, Any]] = []
    for fi, p in enumerate(files):
        cnt = dict.fromkeys(keys, 0)
        hist = np.zeros(len(AOT_BINS) - 1, np.int64)
        ap = aot_path(cfg, p)
        has_aot = ap.exists()
        with rasterio.open(p) as ds:
            ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
            aot_ds = rasterio.open(ap) if has_aot else None
            try:
                for row in range(0, grid.height, block_rows):
                    h = min(block_rows, grid.height - row)
                    r0, r1 = max(0, row - radius), min(grid.height, row + h + radius)
                    core = slice(row - r0, row - r0 + h)
                    win = RioWindow(0, r0, grid.width, r1 - r0)
                    need = sorted({*bands, "B02", "B04", "B08", "SCL"})
                    r = {b: ds.read(ix[b], window=win) for b in need}
                    aot = aot_ds.read(1, window=win) if aot_ds is not None else None
                    hz = {k: v[core] for k, v in
                          haze_components(r["B02"], r["SCL"], aot, cfg, radius).items()}
                    r = {k: v[core] for k, v in r.items()}
                    clear = scl_valid(r["SCL"], s2.scl_invalid)
                    for b in need:
                        if b != "SCL":
                            clear &= r[b] != NODATA
                    hazy = hz["blue"] | hz["buffer"] | hz["aot"]
                    usable = clear & ~hazy
                    m = aoi_mask[row:row + h]
                    cm = clear & m
                    cnt["aoi_valid"] += int((m & (r["B02"] != NODATA)).sum())
                    cnt["scl_clear"] += int(cm.sum())
                    for k in ("blue", "buffer", "aot"):
                        cnt[k] += int((cm & hz[k]).sum())
                    cnt["aot_only"] += int((cm & hz["aot"] & ~hz["blue"] & ~hz["buffer"]).sum())
                    cnt["hazy"] += int((cm & hazy).sum())
                    cnt["usable"] += int((cm & ~hazy).sum())
                    if aot is not None:
                        a = aot[core]
                        cnt["aot_missing"] += int((cm & (a == NODATA)).sum())
                        av = a[cm & (a != NODATA)] / AOT_SCALE
                        hist += np.histogram(np.clip(av, 0, 2.999), AOT_BINS)[0]
                    sel = (rows >= row) & (rows < row + h)
                    if sel.any():
                        rr, cc = rows[sel] - row, cols[sel]
                        ok = usable[rr, cc]
                        for bi, b in enumerate(bands):
                            v = r[b][rr, cc].astype(np.float32) / scale
                            vals[fi, bi, sel] = np.where(ok, v, np.nan)
                        b8 = r["B08"][rr, cc].astype(np.float32)
                        b4 = r["B04"][rr, cc].astype(np.float32)
                        with np.errstate(invalid="ignore", divide="ignore"):
                            ndvi[fi, sel] = np.where(ok, (b8 - b4) / (b8 + b4), np.nan)
            finally:
                if aot_ds is not None:
                    aot_ds.close()
        pct = {}
        if hist.sum():
            c = np.cumsum(hist) / hist.sum()
            pct = {f"aot_p{k}": round(float(AOT_BINS[np.searchsorted(c, k / 100) + 1]), 2)
                   for k in (10, 50, 90)}
        sc = max(cnt["scl_clear"], 1)
        scene.append({
            "file": p.name, "day": p.stem[:4] + "-" + p.stem[4:6] + "-" + p.stem[6:8],
            "has_aot": has_aot, **cnt,
            "scl_clear_pct_aoi": round(100 * cnt["scl_clear"] / max(int(aoi_mask.sum()), 1), 1),
            **{f"{k}_pct_of_clear": round(100 * cnt[k] / sc, 1)
               for k in ("blue", "buffer", "aot", "aot_only", "hazy", "usable")},
            **pct,
        })
        log.info("radiometry %s: usable %.1f%% of SCL-clear", p.name,
                 scene[-1]["usable_pct_of_clear"])

    pif = select_pifs(vals, ndvi, rc.pif_min_views, rc.pif_ndvi_max, rc.pif_max_rel_spread)
    off, spread, npif = offsets_from_pifs(vals[:, :, pif], rc.min_pif_px)

    # scene rule at day level: usable share pooled over the day's files, worst file spread
    day_of = [s["day"] for s in scene]
    days = sorted(set(day_of))
    frac = {}
    day_spread = np.full((len(days), nb), np.nan)
    for di, d in enumerate(days):
        idx = [i for i, x in enumerate(day_of) if x == d]
        cl = sum(scene[i]["scl_clear"] for i in idx)
        frac[d] = sum(scene[i]["usable"] for i in idx) / cl if cl else 0.0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            day_spread[di] = np.nanmax(spread[idx], axis=0)
    excluded = decide_exclusions(days, frac, day_spread, hm.scene_min_usable_frac,
                                 hm.scene_max_pif_spread_factor,
                                 [d.isoformat() for d in hm.exclude_days])
    for i, s in enumerate(scene):
        s["offset"] = {b: round(float(off[i, bi]), 5) for bi, b in enumerate(bands)}
        s["pif_residual_spread"] = {b: (None if not np.isfinite(spread[i, bi])
                                        else round(float(spread[i, bi]), 5))
                                    for bi, b in enumerate(bands)}
        s["n_pif_usable"] = int(npif[i])
        s["offset_applied"] = bool(npif[i] >= rc.min_pif_px)
        s["excluded"] = s["day"] in excluded
    res = {
        "bands": bands, "offset_units": "reflectance (subtract from the view)",
        "n_pif_candidates_sampled": int(flat.size), "n_pif": int(pif.sum()),
        "pif_pixels_csv": "radiometry_pif_pixels.csv",
        "excluded_days": excluded, "day_usable_frac": {d: round(v, 4) for d, v in frac.items()},
        "scenes": scene,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    np.savetxt(out.parent / res["pif_pixels_csv"], np.column_stack([rows[pif], cols[pif]]),
               fmt="%d", delimiter=",", header="row,col", comments="")
    inputs = [{"file": p.name, "aot": aot_path(cfg, p).name if aot_path(cfg, p).exists()
               else None} for p in files]
    inputs.append({"file": worldcover_path(cfg).name,
                   "provenance": (read_provenance(worldcover_path(cfg)) or {}).get("params_hash")})
    write_provenance(out, step="m21_radiometry", params=params, inputs=inputs,
                     extra={"stats": {"n_pif": res["n_pif"],
                                      "excluded_days": sorted(excluded)}})
    return res


def load_radiometry(cfg: Config) -> tuple[dict[str, dict[str, float]], set[date]]:
    """Offsets per file name (reflectance units) and the excluded dates, for the detector."""
    res = json.loads(radiometry_path(cfg).read_text(encoding="utf-8"))
    offs = {s["file"]: s["offset"] for s in res["scenes"] if s["offset_applied"]}
    ex = {date.fromisoformat(d) for d in res["excluded_days"]}
    return offs, ex
