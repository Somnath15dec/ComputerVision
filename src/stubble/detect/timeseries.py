"""Per-pixel burn-date windows from the Sentinel-2 observation time series.

Strict weekly median composites leave many pixels with a single clear look per week (w04:
27 % of AOI pixels have two or more). So detection does not use them. For every pixel the
whole series of clear observations is scanned:

* ``NBR_pre``: median NBR of clear observations in the pre-season window.
* First post-harvest clear observation with ``NBR_pre - NBR_obs > threshold`` = *first
  post-burn observation* (``detect_day``).
* Latest clear observation before it = *last clear pre-burn observation*
  (``window_start``). The burn happened within ``[window_start, detect_day]``; both ends are
  inclusive because burning after the ~10:30 IST overpass on ``window_start`` is possible.

Per-pixel quality bands (clear-observation counts, gaps, haze) are produced in the same pass.

Outputs (int16 COGs, ``NODATA`` = -32768, dates as day-of-year 2025)::

    data/processed/m2/s2_pixel_detect.tif
    data/processed/m2/s2_quality.tif
"""

from __future__ import annotations

import logging
import os
import warnings
from collections import defaultdict
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
from stubble.detect.indices import haze_flag, nbr
from stubble.provenance import is_cached, read_provenance, write_provenance

log = logging.getLogger(__name__)

DOY_BASE = date(2024, 12, 31).toordinal()  # ordinal -> day-of-year 2025

DETECT_BANDS = ["nbr_pre", "dnbr_detect", "dnbr_peak", "dnbr_local", "detect_doy",
                "window_start_doy", "n_support", "n_clear_after", "haze_at_detect",
                "burnt_raw"]
SCALED = {"nbr_pre", "dnbr_detect", "dnbr_peak", "dnbr_local"}  # stored x10000
INDEX_SCALE = 10000


def quality_bands(cfg: Config) -> list[str]:
    """Names of the per-pixel quality bands written by the M2 scan."""
    weeks = [w.id for w in cfg.compositing_windows() if w.id != "pre"]
    return ["n_clear", "n_clear_pre", "n_clear_post", "n_hazy", "days_since_last_clear",
            "max_gap_days", *[f"n_clear_{w}" for w in weeks]]


def doy_to_date(doy: int) -> date:
    """Convert the stored day number back to a date."""
    return date.fromordinal(DOY_BASE + int(doy))


# ---------------------------------------------------------------------------- core scan

@dataclass
class SeriesSpec:
    """Dates of the (same-day merged) observations and the windows used by the scan."""

    days: np.ndarray              # (T,) proleptic ordinals, strictly increasing
    pre: np.ndarray               # (T,) bool: inside the pre-season window
    post: np.ndarray              # (T,) bool: inside the post-harvest windows
    weeks: list[tuple[str, int, int]]  # (id, start ordinal, end ordinal) inclusive
    season_start: int
    season_end: int


def scan_pixels(nbr_t: np.ndarray, clear: np.ndarray, hazy: np.ndarray, spec: SeriesSpec,
                threshold: float, confirm_days: int) -> dict[str, np.ndarray]:
    """Scan per-pixel time series. Arrays are ``(T, ...)``; results are ``(...)``.

    ``clear`` must already exclude missing NBR. Returns float arrays for indices (NaN =
    undefined) and int arrays for days (ordinals, 0 = undefined) and counts.
    """
    n_t = len(spec.days)
    bshape = (n_t,) + (1,) * (nbr_t.ndim - 1)
    days = spec.days.reshape(bshape)
    pre = spec.pre.reshape(bshape)
    post = spec.post.reshape(bshape)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        nbr_pre = np.nanmedian(np.where(clear & pre, nbr_t, np.nan), axis=0)
        d = nbr_pre[None] - nbr_t
        post_clear = clear & post
        dnbr_peak = np.nanmax(np.where(post_clear, d, np.nan), axis=0)
    exceed = post_clear & (d > threshold)          # NaN nbr_pre -> False
    burnt = exceed.any(axis=0)
    t_star = np.argmax(exceed, axis=0)

    idx = np.where(clear, np.arange(n_t).reshape(bshape), -1)
    last_clear_idx = np.maximum.accumulate(idx, axis=0)          # (T, ...)
    prev = np.where(t_star > 0, np.take_along_axis(
        last_clear_idx, np.maximum(t_star - 1, 0)[None], axis=0)[0], -1)
    burnt &= prev >= 0                                              # needs a pre-burn look

    detect_day = np.where(burnt, spec.days[t_star], 0)
    start_day = np.where(burnt, spec.days[np.maximum(prev, 0)], 0)
    within = (days >= detect_day[None]) & (days <= detect_day[None] + confirm_days)
    n_support = np.where(burnt, (exceed & within).sum(axis=0), 0)
    n_after = np.where(burnt, (clear & within & (days > detect_day[None])).sum(axis=0), 0)

    pick = lambda a, i: np.take_along_axis(a, i[None], axis=0)[0]  # noqa: E731
    nbr_star = pick(nbr_t, t_star)
    nbr_prev = pick(nbr_t, np.maximum(prev, 0))
    nan = np.float32(np.nan)
    out = {
        "nbr_pre": nbr_pre.astype(np.float32),
        "dnbr_detect": np.where(burnt, nbr_pre - nbr_star, nan),
        "dnbr_peak": dnbr_peak.astype(np.float32),
        "dnbr_local": np.where(burnt, nbr_prev - nbr_star, nan),
        "detect_day": detect_day,
        "window_start_day": start_day,
        "n_support": n_support,
        "n_clear_after": n_after,
        "haze_at_detect": burnt & pick(hazy, t_star),
        "burnt_raw": burnt,
    }

    # ------------------------------------------------------------ quality
    in_season = (spec.days >= spec.season_start) & (spec.days <= spec.season_end)
    out["n_clear"] = clear.sum(axis=0)
    out["n_clear_pre"] = (clear & pre).sum(axis=0)
    out["n_clear_post"] = post_clear.sum(axis=0)
    out["n_hazy"] = (clear & hazy).sum(axis=0)
    last = last_clear_idx[-1]
    out["days_since_last_clear"] = np.where(
        last >= 0, spec.season_end - spec.days[np.maximum(last, 0)], -1)
    prev_day = np.full(nbr_t.shape[1:], spec.season_start, dtype=np.int64)
    max_gap = np.zeros(nbr_t.shape[1:], dtype=np.int64)
    for t in np.flatnonzero(in_season):
        c = clear[t]
        max_gap = np.where(c, np.maximum(max_gap, spec.days[t] - prev_day), max_gap)
        prev_day = np.where(c, spec.days[t], prev_day)
    out["max_gap_days"] = np.maximum(max_gap, spec.season_end - prev_day)
    for wid, ws, we in spec.weeks:
        sel = ((spec.days >= ws) & (spec.days <= we)).reshape(bshape)
        out[f"n_clear_{wid}"] = (clear & sel).sum(axis=0)
    return out


# ---------------------------------------------------------------------------- I/O runner

def _s2_days(cfg: Config) -> dict[date, list[Path]]:
    by_day: dict[date, list[Path]] = defaultdict(list)
    for p in sorted(cfg.path("raw", "s2").glob("*.tif")):
        if p.name.endswith(".tmp.tif"):
            continue
        s = p.stem.split("_")[0]
        by_day[date(int(s[:4]), int(s[4:6]), int(s[6:8]))].append(p)
    return dict(sorted(by_day.items()))


def series_spec(cfg: Config, days: list[date]) -> SeriesSpec:
    """Masks of acquisition days falling in the pre-season, post and weekly windows."""
    w = cfg.windows
    wins = [x for x in cfg.compositing_windows() if x.id != "pre"]
    o = np.array([d.toordinal() for d in days], dtype=np.int64)
    return SeriesSpec(
        days=o,
        pre=(o >= w.pre.start.toordinal()) & (o <= w.pre.end.toordinal()),
        post=(o >= w.post.start.toordinal()) & (o <= w.post.end.toordinal()),
        weeks=[(x.id, x.start.toordinal(), x.end.toordinal()) for x in wins],
        season_start=cfg.season.start.toordinal(), season_end=cfg.season.end.toordinal(),
    )


def _read_obs(path: Path, win: RioWindow) -> dict[str, np.ndarray]:
    with rasterio.open(path) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        names = ["B02", "B08", "B12", "SCL"]
        arr = ds.read([ix[n] for n in names], window=win)
    return dict(zip(names, arr, strict=True))


def _block(cfg: Config, by_day: dict[date, list[Path]], spec: SeriesSpec, grid: GridSpec,
           row: int, h: int, pool: ThreadPoolExecutor) -> dict[str, np.ndarray]:
    q, s2 = cfg.quality, cfg.sentinel2
    radius = round(q.haze_cloud_buffer_m / grid.resolution)
    r0, r1 = max(0, row - radius), min(grid.height, row + h + radius)
    core = slice(row - r0, row - r0 + h)
    win = RioWindow(0, r0, grid.width, r1 - r0)
    files = [p for ps in by_day.values() for p in ps]
    reads = dict(zip(files, pool.map(lambda p: _read_obs(p, win), files), strict=True))

    n_t = len(by_day)
    shape = (h, grid.width)
    nbr_t = np.full((n_t, *shape), np.nan, dtype=np.float32)
    clear = np.zeros((n_t, *shape), dtype=bool)
    hazy = np.zeros((n_t, *shape), dtype=bool)
    for t, paths in enumerate(by_day.values()):
        acc = np.zeros(shape, np.float32)
        n = np.zeros(shape, np.int16)
        hz = np.zeros(shape, bool)
        for p in paths:
            r = reads[p]
            ok = (scl_valid(r["SCL"], s2.scl_invalid) & (r["B02"] != NODATA))[core]
            v = nbr(r["B08"], r["B12"])[core]
            ok &= np.isfinite(v)
            hzp = haze_flag(r["B02"], r["SCL"], blue_threshold=q.haze_blue_threshold,
                            scale=s2.reflectance_scale, cloud_classes=q.haze_cloud_classes,
                            radius_px=radius)[core]
            acc[ok] += v[ok]
            n[ok] += 1
            hz |= ok & hzp
        c = n > 0
        nbr_t[t][c] = acc[c] / n[c]
        clear[t] = c
        hazy[t] = hz
    d = cfg.detection
    return scan_pixels(nbr_t, clear, hazy, spec, d.dnbr_threshold, d.confirm_days)


def _encode(name: str, a: np.ndarray) -> np.ndarray:
    if name in SCALED:
        return np.where(np.isfinite(a), np.round(np.clip(a, -3, 3) * INDEX_SCALE),
                        NODATA).astype(np.int16)
    if name.endswith("_doy"):
        src = a  # ordinal or 0
        return np.where(src > 0, src - DOY_BASE, 0).astype(np.int16)
    return a.astype(np.int16)


def run_pixel_scan(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray, force: bool = False,
                   block_rows: int = 256, threads: int = 8) -> dict[str, Any]:
    """Run the per-pixel scan over the whole grid; write detect + quality COGs."""
    by_day = _s2_days(cfg)
    spec = series_spec(cfg, list(by_day))
    out_det = cfg.path("processed", "m2", "s2_pixel_detect.tif")
    out_q = cfg.path("processed", "m2", "s2_quality.tif")
    qb = quality_bands(cfg)
    params = {
        "inputs": [p.name for ps in by_day.values() for p in ps],
        "detection": cfg.detection.model_dump(), "quality": cfg.quality.model_dump(),
        "scl_invalid": cfg.sentinel2.scl_invalid, "pre": cfg.windows.pre.model_dump(),
        "post": cfg.windows.post.model_dump(), "season": cfg.season.model_dump(),
        "grid": [grid.crs, list(grid.transform)[:6], grid.width, grid.height],
    }
    if not force and is_cached(out_det, params) and is_cached(out_q, params):
        log.info("pixel scan cached")
        return (read_provenance(out_det) or {}).get("stats", {})

    out_det.parent.mkdir(parents=True, exist_ok=True)
    tmp_d, tmp_q = out_det.with_suffix(".tmp.tif"), out_q.with_suffix(".tmp.tif")
    with (rasterio.open(tmp_d, "w", **_profile(grid, len(DETECT_BANDS))) as dd,
          rasterio.open(tmp_q, "w", **_profile(grid, len(qb))) as dq,
          ThreadPoolExecutor(max_workers=threads) as pool):
        for i, n in enumerate(DETECT_BANDS, 1):
            dd.set_band_description(i, n)
        for i, n in enumerate(qb, 1):
            dq.set_band_description(i, n)
        for row in range(0, grid.height, block_rows):
            h = min(block_rows, grid.height - row)
            res = _block(cfg, by_day, spec, grid, row, h, pool)
            res["detect_doy"] = res.pop("detect_day")
            res["window_start_doy"] = res.pop("window_start_day")
            win = RioWindow(0, row, grid.width, h)
            for i, n in enumerate(DETECT_BANDS, 1):
                dd.write(_encode(n, res[n]), i, window=win)
            for i, n in enumerate(qb, 1):
                dq.write(_encode(n, res[n]), i, window=win)
            log.info("pixel scan rows %d-%d / %d", row, row + h, grid.height)
    for tmp, out in ((tmp_d, out_det), (tmp_q, out_q)):
        _to_cog(tmp, out)
        os.remove(tmp)

    with rasterio.open(out_det) as ds:
        burnt = ds.read(DETECT_BANDS.index("burnt_raw") + 1)
    px_ha = grid.resolution ** 2 / 1e4
    stats = {"n_days": len(by_day), "days": [d.isoformat() for d in by_day],
             "aoi_px": int(aoi_mask.sum()),
             "raw_burnt_ha_in_aoi": round(float((burnt[aoi_mask] == 1).sum() * px_ha), 1)}
    inputs = []
    for ps in by_day.values():
        for p in ps:
            prov = read_provenance(p) or {}
            inputs.append({"file": p.name, "scenes": [s["id"] for s in prov.get("inputs", [])]})
    for out in (out_det, out_q):
        write_provenance(out, step="m2_pixel_scan", params=params, inputs=inputs,
                         extra={"stats": stats,
                                "encoding": {"indices": f"x{INDEX_SCALE}",
                                             "dates": "day-of-year 2025, 0 = none"}})
    return stats
