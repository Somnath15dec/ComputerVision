"""Download per-acquisition rasters (cached) and build window composites as COGs.

Layout::

    data/raw/{s2,s1}/<acq_key>.tif           one mosaicked acquisition on the AOI grid
    data/processed/{s2,s1}/<window_id>.tif   median composite (COG) + observation count band

Every file has a ``.provenance.json`` sidecar. Existing files with matching parameters are
never re-downloaded or rebuilt.
"""

from __future__ import annotations

import logging
import os
import time
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window as RioWindow

from stubble.aoi import GridSpec
from stubble.config import Config, Window
from stubble.data.base import Acquisition, ImageryProvider, SceneRef, Sensor
from stubble.provenance import is_cached, read_provenance, write_provenance

log = logging.getLogger(__name__)

NODATA = -32768
GTIFF_OPTS = dict(tiled=True, blockxsize=512, blockysize=512, compress="DEFLATE", predictor=2,
                  BIGTIFF="IF_SAFER")


# ---------------------------------------------------------------------------- grouping

def group_acquisitions(sensor: Sensor, scenes: list[SceneRef]) -> list[Acquisition]:
    """Group scenes into acquisitions: per UTC day + platform (S2) or + orbit direction (S1)."""
    groups: dict[str, list[SceneRef]] = defaultdict(list)
    for s in scenes:
        day = s.datetime.date()
        if sensor == "s2":
            suffix = s.platform.replace("Sentinel-", "S").lower()  # s2a / s2b / s2c
        else:
            suffix = (s.orbit_state or "unk")[:3]  # asc / des
        groups[f"{day:%Y%m%d}_{suffix}"].append(s)
    return [
        Acquisition(sensor=sensor, day=v[0].datetime.date(), key=k, scenes=v)
        for k, v in sorted(groups.items())
    ]


# ---------------------------------------------------------------------------- scaling

def s2_dn_to_int16(dn: np.ndarray, baseline: str, offset: int) -> np.ndarray:
    """Raw L2A DN (uint16, 0 = no data) to int16 reflectance*10000 with BOA offset applied.

    For processing baseline >= 04.00, reflectance = (DN + BOA_ADD_OFFSET) / 10000.
    """
    apply = float(baseline) >= 4.0
    out = dn.astype(np.int32) + (offset if apply else 0)
    out = np.clip(out, NODATA + 1, 32767)
    out[dn == 0] = NODATA
    return out.astype(np.int16)


def linear_to_db_int16(lin: np.ndarray, scale: int) -> np.ndarray:
    """Linear backscatter to int16 dB*scale; non-positive / NaN -> NODATA."""
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 10.0 * np.log10(lin)
    ok = np.isfinite(db)
    out = np.full(lin.shape, NODATA, dtype=np.int16)
    out[ok] = np.clip(np.round(db[ok] * scale), NODATA + 1, 32767).astype(np.int16)
    return out


def db_int16_to_linear(v: np.ndarray, scale: int) -> np.ndarray:
    """Inverse of :func:`linear_to_db_int16`; NODATA -> NaN."""
    out = np.power(10.0, v.astype(np.float32) / (10.0 * scale))
    out[v == NODATA] = np.nan
    return out


def scl_valid(scl: np.ndarray, invalid: list[int]) -> np.ndarray:
    """True where the Scene Classification Layer marks a usable pixel."""
    return ~np.isin(scl, invalid)


# ---------------------------------------------------------------------------- writing

def _profile(grid: GridSpec, count: int) -> dict[str, Any]:
    return dict(driver="GTiff", dtype="int16", nodata=NODATA, width=grid.width,
                height=grid.height, count=count, crs=grid.crs, transform=grid.transform,
                **GTIFF_OPTS)


def _write_raster(path: Path, grid: GridSpec, arrays: dict[str, np.ndarray]) -> None:
    """Atomic write of named int16 bands (tmp file then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.tif")
    with rasterio.open(tmp, "w", **_profile(grid, len(arrays))) as dst:
        for i, (name, arr) in enumerate(arrays.items(), start=1):
            dst.write(arr, i)
            dst.set_band_description(i, name)
    os.replace(tmp, path)


def _to_cog(src: Path, dst: Path) -> None:
    rio_copy(src, dst, driver="COG", compress="DEFLATE", predictor="2", BIGTIFF="IF_SAFER",
             overview_resampling="nearest", blocksize=512)


# ---------------------------------------------------------------------------- download

def _raw_params(cfg: Config, sensor: Sensor, grid: GridSpec, acq: Acquisition) -> dict:
    p: dict[str, Any] = {
        "sensor": sensor, "grid": [grid.crs, list(grid.transform)[:6], grid.width, grid.height],
        "scene_ids": sorted(s.id for s in acq.scenes),
    }
    if sensor == "s2":
        s2 = cfg.sentinel2
        p |= {"bands": [*s2.bands, s2.scl_band], "offset": s2.boa_offset}
    else:
        p |= {"bands": cfg.sentinel1.bands, "db_scale": cfg.sentinel1.db_scale}
    return p


def download_acquisition(cfg: Config, provider: ImageryProvider, acq: Acquisition,
                         grid: GridSpec) -> tuple[Path, bool]:
    """Fetch one acquisition to ``data/raw``. Returns (path, downloaded_now)."""
    out = cfg.path("raw", acq.sensor, f"{acq.key}.tif")
    params = _raw_params(cfg, acq.sensor, grid, acq)
    if is_cached(out, params):
        return out, False
    for attempt in range(1, cfg.io.max_retries + 1):
        try:
            t0 = time.time()
            if acq.sensor == "s2":
                s2 = cfg.sentinel2
                bl = {s.properties.get("s2:processing_baseline", "0") for s in acq.scenes}
                if len(bl) != 1:
                    raise RuntimeError(f"{acq.key}: mixed processing baselines {bl}")
                data = provider.load(acq, grid, [*s2.bands, s2.scl_band])
                baseline = bl.pop()
                arrays = {b: s2_dn_to_int16(data[b], baseline, s2.boa_offset) for b in s2.bands}
                arrays[s2.scl_band] = data[s2.scl_band].astype(np.int16)  # 0 = no data
            else:
                s1 = cfg.sentinel1
                data = provider.load(acq, grid, s1.bands)
                arrays = {f"{b.upper()}_db": linear_to_db_int16(data[b], s1.db_scale)
                          for b in s1.bands}
            _write_raster(out, grid, arrays)
            write_provenance(out, step=f"download_{acq.sensor}", params=params,
                             inputs=[s.to_record() for s in acq.scenes],
                             extra={"provider": provider.name, "day": acq.day.isoformat(),
                                    "seconds": round(time.time() - t0, 1)})
            return out, True
        except Exception as exc:
            log.warning("%s attempt %d failed: %s", acq.key, attempt, exc)
            if attempt == cfg.io.max_retries:
                raise
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------- composite

def _window_acqs(window: Window, raw_dir: Path) -> list[Path]:
    paths = []
    for p in sorted(raw_dir.glob("*.tif")):
        if p.name.endswith(".tmp.tif"):
            continue
        stem = p.stem.split("_")[0]
        d = f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}"
        if window.start.isoformat() <= d <= window.end.isoformat():
            paths.append(p)
    return paths


def composite_window(cfg: Config, sensor: Sensor, window: Window, grid: GridSpec,
                     aoi_mask: np.ndarray, force: bool = False) -> dict[str, Any]:
    """Median composite of all cached acquisitions in ``window``; writes a COG.

    S2: pixels flagged by SCL (``scl_invalid``) are excluded before the median; output bands
    are the configured reflectance bands plus ``valid_obs`` (count of usable observations).
    S1: median of linear backscatter, returned in dB; plus ``n_obs``.
    """
    raw = _window_acqs(window, cfg.path("raw", sensor))
    out = cfg.path("processed", sensor, f"{window.id}.tif")
    inputs = []
    for p in raw:
        prov = read_provenance(p) or {}
        inputs.append({"file": p.name, "scenes": [s["id"] for s in prov.get("inputs", [])]})
    if sensor == "s2":
        bands = list(cfg.sentinel2.bands)
        params = {"sensor": sensor, "window": window.label, "bands": bands,
                  "scl_invalid": cfg.sentinel2.scl_invalid, "method": "nanmedian",
                  "inputs": [i["file"] for i in inputs]}
        names = [*bands, "valid_obs"]
    else:
        bands = [f"{b.upper()}_db" for b in cfg.sentinel1.bands]
        params = {"sensor": sensor, "window": window.label, "bands": bands,
                  "method": "nanmedian_linear", "inputs": [i["file"] for i in inputs]}
        names = [*bands, "n_obs"]

    if not raw:
        log.warning("%s %s: no acquisitions", sensor, window.id)
        return {"sensor": sensor, "window": window.id, "label": window.label,
                "n_acq": 0, "aoi_valid_pct": 0.0, "mean_obs": 0.0, "path": None}
    if is_cached(out, params) and not force:
        prov = read_provenance(out) or {}
        return prov.get("stats", {})

    tmp = out.with_suffix(".tmp.tif")
    out.parent.mkdir(parents=True, exist_ok=True)
    srcs = [rasterio.open(p) for p in raw]
    try:
        idx = [{d: i + 1 for i, d in enumerate(s.descriptions)} for s in srcs]
        with rasterio.open(tmp, "w", **_profile(grid, len(names))) as dst:
            for i, n in enumerate(names, start=1):
                dst.set_band_description(i, n)
            step = cfg.io.block_rows
            for row in range(0, grid.height, step):
                h = min(step, grid.height - row)
                win = RioWindow(0, row, grid.width, h)
                if sensor == "s2":
                    scl = np.stack([s.read(ix["SCL"], window=win) for s, ix in zip(srcs, idx,
                                                                                   strict=True)])
                    ok = scl_valid(scl, cfg.sentinel2.scl_invalid)
                    stack = {}
                    for b in bands:
                        arr = np.stack([s.read(ix[b], window=win) for s, ix in zip(srcs, idx,
                                                                                   strict=True)])
                        ok &= arr != NODATA
                        stack[b] = arr
                    for bi, b in enumerate(bands, start=1):
                        f = np.where(ok, stack[b], np.nan).astype(np.float32)
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", RuntimeWarning)
                            med = np.nanmedian(f, axis=0)
                        o = np.where(np.isfinite(med), np.round(med), NODATA).astype(np.int16)
                        dst.write(o, bi, window=win)
                    dst.write(ok.sum(axis=0).astype(np.int16), len(names), window=win)
                else:
                    scale = cfg.sentinel1.db_scale
                    nobs = None
                    for bi, b in enumerate(bands, start=1):
                        lin = np.stack([db_int16_to_linear(s.read(ix[b], window=win), scale)
                                        for s, ix in zip(srcs, idx, strict=True)])
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", RuntimeWarning)
                            med = np.nanmedian(lin, axis=0)
                        dst.write(linear_to_db_int16(med, scale), bi, window=win)
                        cnt = np.isfinite(lin).sum(axis=0)
                        nobs = cnt if nobs is None else np.minimum(nobs, cnt)
                    dst.write(nobs.astype(np.int16), len(names), window=win)
    finally:
        for s in srcs:
            s.close()
    _to_cog(tmp, out)
    tmp.unlink()

    with rasterio.open(out) as ds:
        cnt = ds.read(len(names))
    inside = cnt[aoi_mask]
    stats = {
        "sensor": sensor, "window": window.id, "label": window.label, "n_acq": len(raw),
        "aoi_valid_pct": round(float((inside > 0).mean() * 100), 2),
        "aoi_valid2_pct": round(float((inside > 1).mean() * 100), 2),
        "mean_obs": round(float(inside.mean()), 2), "path": str(out.name),
    }
    write_provenance(out, step=f"composite_{sensor}", params=params, inputs=inputs,
                     extra={"stats": stats})
    return stats
