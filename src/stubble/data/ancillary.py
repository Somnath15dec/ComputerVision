"""Ancillary rasters added in M2.1 without re-downloading the cached reflectance tiles.

Layout::

    data/raw/s2_aot/<acq_key>.tif         Sen2Cor AOT (x1000, int16) for the SAME scenes as
                                          data/raw/s2/<acq_key>.tif (scene IDs from its sidecar)
    data/raw/static/worldcover_2021.tif   ESA WorldCover 10 m 2021 v200 class codes

ESA WorldCover: (c) ESA WorldCover project 2021 / contains modified Copernicus Sentinel data
(2021) processed by ESA WorldCover consortium, CC BY 4.0.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.data.base import Acquisition
from stubble.data.composites import NODATA, _write_raster
from stubble.provenance import is_cached, read_provenance, write_provenance

log = logging.getLogger(__name__)

AOT_SCALE = 1000  # Sen2Cor AOT_QUANTIFICATION_VALUE
WORLDCOVER = {10: "tree", 20: "shrub", 30: "grass", 40: "cropland", 50: "built_up",
              60: "bare_sparse", 70: "snow_ice", 80: "water", 90: "herbaceous_wetland",
              95: "mangrove", 100: "moss_lichen"}


def aot_path(cfg: Config, s2_raw: Path) -> Path:
    """AOT raster that belongs to a cached S2 acquisition file."""
    return cfg.path("raw", "s2_aot", s2_raw.name)


def worldcover_path(cfg: Config) -> Path:
    return cfg.path("raw", "static", "worldcover_2021.tif")


def _grid_param(grid: GridSpec) -> list:
    return [grid.crs, list(grid.transform)[:6], grid.width, grid.height]


def download_aot(cfg: Config, provider, grid: GridSpec) -> list[tuple[str, bool]]:
    """Fetch the AOT asset for every cached S2 acquisition (same scene IDs). Cached."""
    done = []
    for raw in sorted(cfg.path("raw", "s2").glob("*.tif")):
        if raw.name.endswith(".tmp.tif"):
            continue
        prov = read_provenance(raw) or {}
        ids = sorted(prov.get("params", {}).get("scene_ids", []))
        if not ids:
            raise RuntimeError(f"{raw.name}: no scene IDs in provenance")
        out = aot_path(cfg, raw)
        params = {"asset": "AOT", "scale": AOT_SCALE, "scene_ids": ids,
                  "grid": _grid_param(grid)}
        if is_cached(out, params):
            done.append((raw.stem, False))
            continue
        for attempt in range(1, cfg.io.max_retries + 1):
            try:
                t0 = time.time()
                scenes = provider.get_items("s2", ids)
                acq = Acquisition(sensor="s2", day=scenes[0].datetime.date(), key=raw.stem,
                                  scenes=scenes)
                dn = provider.load(acq, grid, ["AOT"])["AOT"]
                arr = np.where(dn == 0, NODATA, np.clip(dn, 0, 32767)).astype(np.int16)
                _write_raster(out, grid, {"AOT": arr})
                write_provenance(out, step="download_s2_aot", params=params,
                                 inputs=[s.to_record() for s in scenes],
                                 extra={"provider": provider.name, "pairs_with": raw.name,
                                        "seconds": round(time.time() - t0, 1)})
                break
            except Exception as exc:
                log.warning("%s AOT attempt %d failed: %s", raw.stem, attempt, exc)
                if attempt == cfg.io.max_retries:
                    raise
                time.sleep(5 * attempt)
        done.append((raw.stem, True))
    return done


def download_worldcover(cfg: Config, provider, grid: GridSpec,
                        bbox_wgs84: tuple[float, float, float, float]) -> tuple[Path, bool]:
    """Fetch ESA WorldCover 2021 class codes on the AOI grid. Cached."""
    out = worldcover_path(cfg)
    params = {"collection": "esa-worldcover", "asset": "map", "grid": _grid_param(grid)}
    if is_cached(out, params):
        return out, False
    arr, ids = provider.load_static("esa-worldcover", "map", grid, bbox_wgs84)
    _write_raster(out, grid, {"worldcover": np.where(arr == 0, NODATA, arr).astype(np.int16)})
    write_provenance(out, step="download_worldcover", params=params,
                     inputs=[{"id": i} for i in ids],
                     extra={"licence": "CC BY 4.0, (c) ESA WorldCover project 2021",
                            "classes": WORLDCOVER})
    return out, True
