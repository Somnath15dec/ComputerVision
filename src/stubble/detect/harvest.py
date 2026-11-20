"""M2.1 harvest-aware burn detection (per-pixel S2 time series).

The M2 baseline compared every post-harvest view with the pre-season (green paddy) median,
so harvest alone crossed the dNBR threshold. Here, per pixel:

1. **Harvest**: the first usable view with ``NDVI < ndvi_harvest`` after at least
   ``min_green_views`` views with ``NDVI > ndvi_green``. Harvest window =
   ``[last usable view before it, harvest view]``.
2. **Burn**: tested only between *consecutive* usable views after the harvest view. A pair
   ``(prev, cur)`` is a burn when all three drops exceed their thresholds:
   short-interval dNBR (``NBR_prev - NBR_cur``), relative visible+NIR albedo drop
   (albedo = mean of B02, B03, B04, B08) and an NBR2 drop (``NBR2 = (B11-B12)/(B11+B12)``).
   Burn-date window = ``[last usable unburnt view, first usable burnt view]``.
3. **Early burns**: if the harvest view (the first non-green view) already has a strong
   absolute char signature (``NBR <= char_nbr_max`` and albedo ``<= char_albedo_max``), the
   field was harvested and burnt between two views: ``harvest_and_burn_same_window``, with
   burn window = harvest window. Otherwise that interval stays unobserved for burning.
4. **Labels**: ``no_harvest`` (never green-then-bare), ``unobserved`` (harvest seen but no
   usable view after it; never reported as not burnt), ``not_burnt`` (views after harvest,
   no burn signal), ``burnt``, ``harvest_and_burn_same_window``.

"Usable" = clear by SCL and, unless ``use_hazy_views``, not haze-flagged (blue, cloud buffer,
Sen2Cor AOT; see :mod:`stubble.detect.radiometry`) and not on an excluded date. Unusable
views are skipped: they can widen a window but never trigger or end one. Reflectances are
corrected with the per-acquisition PIF offsets before any index is computed.

Output: ``data/processed/m21/s2_harvest_burn.tif`` (int16 COG, dates as day-of-year 2025).
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window as RioWindow

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.data.composites import NODATA, _profile, _to_cog, scl_valid
from stubble.detect.indices import nbr
from stubble.detect.timeseries import DOY_BASE, INDEX_SCALE, _s2_days, series_spec
from stubble.provenance import is_cached, read_provenance, write_provenance

log = logging.getLogger(__name__)

LABELS = {0: "outside", 1: "no_harvest", 2: "unobserved", 3: "not_burnt", 4: "burnt",
          5: "harvest_and_burn_same_window"}
NO_HARVEST, UNOBSERVED, NOT_BURNT, BURNT, SAME_WINDOW = 1, 2, 3, 4, 5
BURN_LABELS = (BURNT, SAME_WINDOW)

BANDS = ["label", "harvest_doy", "harvest_prev_doy", "burn_doy", "burn_prev_doy",
         "dnbr_short", "albedo_drop_rel", "dnbr2", "nbr_prev", "nbr_burn", "albedo_burn",
         "n_support", "n_after", "n_views_post_harvest", "last_view_doy", "n_hazy_skipped",
         "ndvi_peak", "nbr_harvest", "albedo_harvest"]
SCALED = {"dnbr_short", "albedo_drop_rel", "dnbr2", "nbr_prev", "nbr_burn", "albedo_burn",
          "ndvi_peak", "nbr_harvest", "albedo_harvest"}
S2_READ = ["B02", "B03", "B04", "B08", "B11", "B12", "SCL"]


@dataclass
class Thresholds:
    """Harvest, burn and early-burn thresholds for the per-pixel M2.1 scan."""
    ndvi_green: float
    min_green_views: int
    ndvi_harvest: float
    dnbr_short_min: float
    albedo_rel_drop_min: float
    dnbr2_min: float
    confirm_days: int
    char_nbr_max: float | None = None      # None disables the early-burn label
    char_albedo_max: float | None = None

    @classmethod
    def from_config(cls, cfg: Config) -> Thresholds:
        """Build the thresholds from ``harvest_detection`` and ``burn_detection``."""
        h, b = cfg.harvest_detection, cfg.burn_detection
        return cls(h.ndvi_green, h.min_green_views, h.ndvi_harvest, b.dnbr_short_min,
                   b.albedo_rel_drop_min, b.dnbr2_min, b.confirm_days, b.char_nbr_max,
                   b.char_albedo_max)


def norm_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``(a - b) / (a + b)`` on int16 reflectance; NaN for missing or non-positive sums."""
    return nbr(a, b)


def albedo(*bands: np.ndarray, scale: int = 10000) -> np.ndarray:
    """Mean reflectance of the given int16 bands (NaN if any is missing)."""
    st = np.stack(bands).astype(np.float32)
    bad = (np.stack(bands) == NODATA).any(axis=0)
    out = st.mean(axis=0) / scale
    out[bad] = np.nan
    return out


def scan_harvest_burn(days: np.ndarray, ndvi: np.ndarray, nbr_t: np.ndarray,
                      nbr2: np.ndarray, alb: np.ndarray, usable: np.ndarray,
                      hazy_clear: np.ndarray, th: Thresholds) -> dict[str, np.ndarray]:
    """Run the harvest/burn state machine over ``(T, ...)`` arrays; returns ``(...)`` arrays.

    ``usable`` marks views that may be used (clear, finite indices, not skipped for haze).
    ``hazy_clear`` marks clear views skipped because of haze (counted only). Day values are
    ordinals; 0 = undefined.
    """
    shp = ndvi.shape[1:]
    i64 = lambda: np.zeros(shp, np.int64)  # noqa: E731
    f32 = lambda: np.full(shp, np.nan, np.float32)  # noqa: E731
    green = np.zeros(shp, np.int16)
    harvested = np.zeros(shp, bool)
    burnt = np.zeros(shp, bool)
    same = np.zeros(shp, bool)
    lp_nbr, lp_nbr2, lp_alb = f32(), f32(), f32()  # last usable pre-harvest view
    use_char = th.char_nbr_max is not None and th.char_albedo_max is not None
    last_day = i64()
    harvest_day, harvest_prev = i64(), i64()
    burn_day, burn_prev = i64(), i64()
    p_nbr, p_nbr2, p_alb, p_day = f32(), f32(), f32(), i64()
    o = {k: f32() for k in ("dnbr_short", "albedo_drop_rel", "dnbr2", "nbr_prev", "nbr_burn",
                            "albedo_burn")}
    n_support, n_after, n_post, n_hazy = i64(), i64(), i64(), i64()
    last_view = i64()
    ndvi_peak = f32()
    nbr_h, alb_h = f32(), f32()

    for t, day in enumerate(days):
        u = usable[t]
        # post-burn support (views after the first burnt view, within confirm_days)
        sup = u & burnt & (day > burn_day) & (day <= burn_day + th.confirm_days)
        n_after += sup
        n_support += sup & (nbr_t[t] <= o["nbr_prev"] - th.dnbr_short_min)
        n_hazy += hazy_clear[t] & harvested

        # burn test between consecutive usable views after harvest
        m = u & harvested & ~burnt
        with np.errstate(invalid="ignore", divide="ignore"):
            d1 = p_nbr - nbr_t[t]
            d2 = (p_alb - alb[t]) / p_alb
            d3 = p_nbr2 - nbr2[t]
        hit = m & (d1 > th.dnbr_short_min) & (d2 > th.albedo_rel_drop_min) & (d3 > th.dnbr2_min)
        burnt |= hit
        burn_day = np.where(hit, day, burn_day)
        burn_prev = np.where(hit, p_day, burn_prev)
        o["dnbr_short"] = np.where(hit, d1, o["dnbr_short"])
        o["albedo_drop_rel"] = np.where(hit, d2, o["albedo_drop_rel"])
        o["dnbr2"] = np.where(hit, d3, o["dnbr2"])
        o["nbr_prev"] = np.where(hit, p_nbr, o["nbr_prev"])
        o["nbr_burn"] = np.where(hit, nbr_t[t], o["nbr_burn"])
        o["albedo_burn"] = np.where(hit, alb[t], o["albedo_burn"])
        n_support += hit
        post = u & harvested
        n_post += post
        last_view = np.where(post, day, last_view)
        upd = m & ~hit                     # roll the "previous unburnt view" forward
        p_nbr = np.where(upd, nbr_t[t], p_nbr)
        p_nbr2 = np.where(upd, nbr2[t], p_nbr2)
        p_alb = np.where(upd, alb[t], p_alb)
        p_day = np.where(upd, day, p_day)

        # harvest detection
        pre_h = u & ~harvested
        ndvi_peak = np.where(pre_h, np.fmax(ndvi_peak, ndvi[t]), ndvi_peak)
        green += (pre_h & (ndvi[t] > th.ndvi_green)).astype(np.int16)
        new_h = pre_h & (green >= th.min_green_views) & (ndvi[t] < th.ndvi_harvest)
        harvested |= new_h
        harvest_day = np.where(new_h, day, harvest_day)
        harvest_prev = np.where(new_h, last_day, harvest_prev)
        nbr_h = np.where(new_h, nbr_t[t], nbr_h)
        alb_h = np.where(new_h, alb[t], alb_h)
        p_nbr = np.where(new_h, nbr_t[t], p_nbr)
        p_nbr2 = np.where(new_h, nbr2[t], p_nbr2)
        p_alb = np.where(new_h, alb[t], p_alb)
        p_day = np.where(new_h, day, p_day)
        if use_char:  # harvested and burnt between the last green view and this one
            with np.errstate(invalid="ignore", divide="ignore"):
                ch = new_h & (nbr_t[t] <= th.char_nbr_max) & (alb[t] <= th.char_albedo_max)
                e1 = lp_nbr - nbr_t[t]
                e2 = (lp_alb - alb[t]) / lp_alb
                e3 = lp_nbr2 - nbr2[t]
            burnt |= ch
            same |= ch
            burn_day = np.where(ch, day, burn_day)
            burn_prev = np.where(ch, last_day, burn_prev)
            o["dnbr_short"] = np.where(ch, e1, o["dnbr_short"])
            o["albedo_drop_rel"] = np.where(ch, e2, o["albedo_drop_rel"])
            o["dnbr2"] = np.where(ch, e3, o["dnbr2"])
            o["nbr_prev"] = np.where(ch, lp_nbr, o["nbr_prev"])
            o["nbr_burn"] = np.where(ch, nbr_t[t], o["nbr_burn"])
            o["albedo_burn"] = np.where(ch, alb[t], o["albedo_burn"])
            n_support += ch
        keep = pre_h & ~new_h
        lp_nbr = np.where(keep, nbr_t[t], lp_nbr)
        lp_nbr2 = np.where(keep, nbr2[t], lp_nbr2)
        lp_alb = np.where(keep, alb[t], lp_alb)
        last_day = np.where(u, day, last_day)

    label = np.full(shp, NO_HARVEST, np.int16)
    label[harvested & (n_post == 0)] = UNOBSERVED
    label[harvested & (n_post > 0)] = NOT_BURNT
    label[burnt] = BURNT
    label[same] = SAME_WINDOW
    return {"label": label, "harvest_day": harvest_day, "harvest_prev_day": harvest_prev,
            "burn_day": burn_day, "burn_prev_day": burn_prev, **o, "n_support": n_support,
            "n_after": n_after, "n_views_post_harvest": n_post, "last_view_day": last_view,
            "n_hazy_skipped": n_hazy, "ndvi_peak": ndvi_peak, "nbr_harvest": nbr_h,
            "albedo_harvest": alb_h}


# ---------------------------------------------------------------------------- runner

def _read(path: Path, win: RioWindow, aot: Path | None = None) -> dict[str, np.ndarray]:
    with rasterio.open(path) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        arr = ds.read([ix[n] for n in S2_READ], window=win)
    out = dict(zip(S2_READ, arr, strict=True))
    if aot is not None and aot.exists():
        with rasterio.open(aot) as ds:
            out["AOT"] = ds.read(1, window=win)
    return out


def apply_offsets(r: dict[str, np.ndarray], off: dict[str, float] | None,
                  scale: int) -> dict[str, np.ndarray]:
    """Subtract per-band offsets (reflectance units) from int16 bands; NODATA kept."""
    if not off:
        return r
    out = dict(r)
    for b, v in off.items():
        if b in out:
            a = out[b]
            out[b] = np.where(a == NODATA, NODATA,
                              np.clip(np.round(a - v * scale), NODATA + 1, 32767)
                              ).astype(np.int16)
    return out


def _radiometry(cfg: Config) -> tuple[dict[str, dict[str, float]], set[date]]:
    if cfg.radiometry is None:
        return {}, set(cfg.haze_mask.exclude_days)
    from stubble.detect.radiometry import load_radiometry
    return load_radiometry(cfg)


def _block_inputs(cfg: Config, by_day: dict[date, list[Path]], grid: GridSpec, row: int,
                  h: int, pool: ThreadPoolExecutor) -> tuple:
    """Read one row block and build the per-day index stacks used by the scan."""
    from stubble.data.ancillary import aot_path
    from stubble.detect.radiometry import haze_components

    q, s2 = cfg.quality, cfg.sentinel2
    offsets, excluded = _radiometry(cfg)
    radius = round(q.haze_cloud_buffer_m / grid.resolution)
    r0, r1 = max(0, row - radius), min(grid.height, row + h + radius)
    core = slice(row - r0, row - r0 + h)
    win = RioWindow(0, r0, grid.width, r1 - r0)
    files = [p for ps in by_day.values() for p in ps]
    reads = dict(zip(files, pool.map(lambda p: _read(p, win, aot_path(cfg, p)), files),
                     strict=True))
    n_t, shape = len(by_day), (h, grid.width)
    arrs = {k: np.full((n_t, *shape), np.nan, np.float32) for k in ("ndvi", "nbr", "nbr2",
                                                                     "alb")}
    usable = np.zeros((n_t, *shape), bool)
    hazy_clear = np.zeros((n_t, *shape), bool)
    for t, (day, paths) in enumerate(by_day.items()):
        acc = {k: np.zeros(shape, np.float32) for k in arrs}
        n = np.zeros(shape, np.int16)
        hz_any = np.zeros(shape, bool)
        for p in paths:
            full = reads[p]
            r = apply_offsets({k: v[core] for k, v in full.items()}, offsets.get(p.name),
                              s2.reflectance_scale)
            v = {"ndvi": norm_diff(r["B08"], r["B04"]), "nbr": norm_diff(r["B08"], r["B12"]),
                 "nbr2": norm_diff(r["B11"], r["B12"]),
                 "alb": albedo(r["B02"], r["B03"], r["B04"], r["B08"],
                               scale=s2.reflectance_scale)}
            ok = scl_valid(r["SCL"], s2.scl_invalid)
            for a in v.values():
                ok &= np.isfinite(a)
            hc = haze_components(full["B02"], full["SCL"], full.get("AOT"), cfg, radius)
            hz = (hc["blue"] | hc["buffer"] | hc["aot"])[core]
            if day in excluded:
                hz = np.ones_like(hz)
            if not cfg.burn_detection.use_hazy_views:
                hz_any |= ok & hz
                ok &= ~hz
            for k in arrs:
                acc[k][ok] += v[k][ok]
            n[ok] += 1
        c = n > 0
        for k in arrs:
            arrs[k][t][c] = acc[k][c] / n[c]
        usable[t] = c
        hazy_clear[t] = hz_any & ~c
    spec = series_spec(cfg, list(by_day))
    return (spec.days, arrs["ndvi"], arrs["nbr"], arrs["nbr2"], arrs["alb"], usable,
            hazy_clear)


def _block(cfg: Config, by_day: dict[date, list[Path]], grid: GridSpec, row: int, h: int,
           pool: ThreadPoolExecutor, th: Thresholds) -> dict[str, np.ndarray]:
    return scan_harvest_burn(*_block_inputs(cfg, by_day, grid, row, h, pool), th)


SENSITIVITY = [
    ("default", {}),
    ("dnbr_short_min x0.5", {"dnbr_short_min": 0.5}), ("dnbr_short_min x2", {"dnbr_short_min": 2}),
    ("albedo_rel_drop_min x0.5", {"albedo_rel_drop_min": 0.5}),
    ("albedo_rel_drop_min x2", {"albedo_rel_drop_min": 2}),
    ("dnbr2_min x0.5", {"dnbr2_min": 0.5}), ("dnbr2_min x2", {"dnbr2_min": 2}),
    ("ndvi_harvest -0.05", {"ndvi_harvest": -0.05}), ("ndvi_harvest +0.05", {"ndvi_harvest": 0.05}),
    ("ndvi_green -0.05", {"ndvi_green": -0.05}), ("ndvi_green +0.05", {"ndvi_green": 0.05}),
]


def sensitivity(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray, n_blocks: int = 6,
                block_rows: int = 128) -> list[dict[str, Any]]:
    """Raw burnt / unobserved share of AOI pixels in sample row blocks under varied thresholds.

    Multiplicative factors for the burn thresholds, additive offsets for the NDVI ones. Raw
    pixel shares (before cleaning), so they compare variants, not final areas.
    """
    import dataclasses

    by_day = _s2_days(cfg)
    base = Thresholds.from_config(cfg)
    rows_idx = np.flatnonzero(aoi_mask.any(axis=1))
    starts = np.linspace(rows_idx[0], rows_idx[-1] - block_rows, n_blocks).astype(int)
    res = {name: [0, 0, 0, 0] for name, _ in SENSITIVITY}  # burnt, unobs, harvested, aoi
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r0 in starts:
            inp = _block_inputs(cfg, by_day, grid, int(r0), block_rows, pool)
            m = aoi_mask[r0:r0 + block_rows]
            for name, mod in SENSITIVITY:
                kw = {k: (getattr(base, k) * v if k.startswith(("dnbr", "albedo"))
                          else getattr(base, k) + v) for k, v in mod.items()}
                lab = scan_harvest_burn(*inp, dataclasses.replace(base, **kw))["label"][m]
                acc = res[name]
                acc[0] += int(np.isin(lab, BURN_LABELS).sum())
                acc[1] += int((lab == UNOBSERVED).sum())
                acc[2] += int((lab >= UNOBSERVED).sum())
                acc[3] += int(lab.size)
    return [{"variant": k, "burnt_pct_aoi": round(100 * v[0] / v[3], 1),
             "burnt_pct_harvested": round(100 * v[0] / max(v[2], 1), 1),
             "unobserved_pct_aoi": round(100 * v[1] / v[3], 2),
             "harvested_pct_aoi": round(100 * v[2] / v[3], 1)} for k, v in res.items()]


def _encode(name: str, a: np.ndarray) -> np.ndarray:
    if name in SCALED:
        return np.where(np.isfinite(a), np.round(np.clip(a, -3, 3) * INDEX_SCALE),
                        NODATA).astype(np.int16)
    if name.endswith("_doy"):
        return np.where(a > 0, a - DOY_BASE, 0).astype(np.int16)
    return a.astype(np.int16)


def _radiometry_hash(cfg: Config) -> str | None:
    if cfg.radiometry is None:
        return None
    from stubble.detect.radiometry import radiometry_path
    return (read_provenance(radiometry_path(cfg)) or {}).get("params_hash")


def run_harvest_scan(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray, force: bool = False,
                     block_rows: int = 256, threads: int = 8) -> dict[str, Any]:
    """Run the harvest-aware scan over the grid; write ``s2_harvest_burn.tif``."""
    by_day = _s2_days(cfg)
    out = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    params = {
        "inputs": [p.name for ps in by_day.values() for p in ps],
        "harvest": cfg.harvest_detection.model_dump(), "burn": cfg.burn_detection.model_dump(),
        "quality": cfg.quality.model_dump(), "scl_invalid": cfg.sentinel2.scl_invalid,
        "haze_mask": cfg.haze_mask.model_dump(mode="json"),
        "radiometry": _radiometry_hash(cfg),
        "grid": [grid.crs, list(grid.transform)[:6], grid.width, grid.height],
    }
    if not force and is_cached(out, params):
        return (read_provenance(out) or {}).get("stats", {})
    th = Thresholds.from_config(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.tif")
    with (rasterio.open(tmp, "w", **_profile(grid, len(BANDS))) as dst,
          ThreadPoolExecutor(max_workers=threads) as pool):
        for i, n in enumerate(BANDS, 1):
            dst.set_band_description(i, n)
        for row in range(0, grid.height, block_rows):
            h = min(block_rows, grid.height - row)
            res = _block(cfg, by_day, grid, row, h, pool, th)
            for k in ("harvest_day", "harvest_prev_day", "burn_day", "burn_prev_day",
                      "last_view_day"):
                res[k.replace("_day", "_doy")] = res.pop(k)
            w = RioWindow(0, row, grid.width, h)
            for i, n in enumerate(BANDS, 1):
                dst.write(_encode(n, res[n]), i, window=w)
            log.info("harvest scan rows %d-%d / %d", row, row + h, grid.height)
    _to_cog(tmp, out)
    os.remove(tmp)
    with rasterio.open(out) as ds:
        lab = ds.read(1)[aoi_mask]
    px_ha = grid.resolution ** 2 / 1e4
    stats = {"aoi_px": int(aoi_mask.sum()),
             **{f"{LABELS[k]}_px": int((lab == k).sum()) for k in (1, 2, 3, 4, 5)},
             "burnt_raw_ha": round(float(np.isin(lab, BURN_LABELS).sum() * px_ha), 1)}
    inputs = []
    for ps in by_day.values():
        for p in ps:
            prov = read_provenance(p) or {}
            inputs.append({"file": p.name, "scenes": [s["id"] for s in prov.get("inputs", [])]})
    write_provenance(out, step="m21_harvest_scan", params=params, inputs=inputs,
                     extra={"stats": stats, "labels": LABELS,
                            "encoding": {"indices": f"x{INDEX_SCALE}",
                                         "dates": "day-of-year 2025, 0 = none"}})
    return stats
