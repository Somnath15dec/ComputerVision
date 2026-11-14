"""Area of interest: district boundary, raster grid definition and analysis grid."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import requests
from affine import Affine
from shapely.geometry import box, mapping, shape

from stubble.config import Config
from stubble.provenance import write_provenance

log = logging.getLogger(__name__)

NOMINATIM_LOOKUP = "https://nominatim.openstreetmap.org/lookup"
USER_AGENT = "stubble-audit/0.1 (open-source research pipeline)"


@dataclass(frozen=True)
class GridSpec:
    """Pixel grid shared by every raster product."""

    crs: str
    transform: Affine
    width: int
    height: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        t = self.transform
        return (t.c, t.f + t.e * self.height, t.c + t.a * self.width, t.f)

    @property
    def resolution(self) -> float:
        return self.transform.a


def _fetch_osm_relation(relation_id: int, cache_file: Path) -> dict:
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    r = requests.get(
        NOMINATIM_LOOKUP,
        params={"osm_ids": f"R{relation_id}", "format": "geojson", "polygon_geojson": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=90,
    )
    r.raise_for_status()
    fc = r.json()
    if not fc.get("features"):
        raise ValueError(f"OSM relation {relation_id} returned no features")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(fc), encoding="utf-8")
    return fc


def build_aoi(cfg: Config, force: bool = False) -> gpd.GeoDataFrame:
    """Create ``processed/aoi/aoi.geojson`` (EPSG:4326) from OSM, or the bbox fallback."""
    out = cfg.path("processed", "aoi", "aoi.geojson")
    if out.exists() and not force:
        return gpd.read_file(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    a = cfg.aoi
    source, note = a.source, None
    geom = None
    if a.source == "osm" and a.osm_relation_id:
        try:
            fc = _fetch_osm_relation(
                a.osm_relation_id, cfg.path("raw", "aoi", f"osm_R{a.osm_relation_id}.geojson")
            )
            feat = fc["features"][0]
            geom = shape(feat["geometry"])
            name = feat["properties"].get("display_name", "")
            note = (f"OSM relation {a.osm_relation_id}: {name}. "
                    "(c) OpenStreetMap contributors, ODbL.")
        except Exception as exc:  # network failure etc.
            log.warning("OSM boundary unavailable (%s); using fallback bbox", exc)
            source = "bbox_fallback"
    if geom is None:
        fb = a.fallback_bbox
        pt = gpd.GeoSeries.from_xy([fb.lon], [fb.lat], crs=4326).to_crs(a.crs)
        geom = pt.buffer(fb.buffer_km * 1000, cap_style="square").to_crs(4326).iloc[0]
        source = "bbox_fallback"
        note = (f"FALLBACK: square of +/-{fb.buffer_km} km around {fb.lat}N {fb.lon}E; "
                "NOT the district boundary.")
    gdf = gpd.GeoDataFrame(
        {"name": ["Sangrur"], "source": [source], "note": [note]}, geometry=[geom], crs=4326
    )
    gdf["area_km2"] = gdf.to_crs(a.crs).area / 1e6
    gdf.to_file(out, driver="GeoJSON")
    write_provenance(out, step="aoi", params={"aoi": a.model_dump()},
                     extra={"source": source, "note": note,
                            "area_km2": float(gdf.area_km2.iloc[0])})
    log.info("AOI: %s (%.1f km2)", note, gdf.area_km2.iloc[0])
    return gdf


def grid_spec(cfg: Config, aoi: gpd.GeoDataFrame) -> GridSpec:
    """Raster grid: AOI bounds (+ context buffer) snapped to the resolution, in the UTM CRS."""
    a = cfg.aoi
    res = a.resolution_m
    if a.subset_bbox:
        g = gpd.GeoSeries([box(*a.subset_bbox)], crs=4326).to_crs(a.crs)
    else:
        g = aoi.to_crs(a.crs).geometry.buffer(a.context_buffer_m)
    minx, miny, maxx, maxy = g.total_bounds
    minx, miny = math.floor(minx / res) * res, math.floor(miny / res) * res
    maxx, maxy = math.ceil(maxx / res) * res, math.ceil(maxy / res) * res
    width, height = round((maxx - minx) / res), round((maxy - miny) / res)
    return GridSpec(a.crs, Affine(res, 0, minx, 0, -res, maxy), width, height)


def aoi_mask(cfg: Config, aoi: gpd.GeoDataFrame, spec: GridSpec) -> np.ndarray:
    """Boolean raster (True inside the AOI polygon) on ``spec``."""
    from rasterio.features import geometry_mask

    geoms = [mapping(g) for g in aoi.to_crs(spec.crs).geometry]
    return ~geometry_mask(geoms, out_shape=(spec.height, spec.width), transform=spec.transform)


def build_analysis_grid(cfg: Config, aoi: gpd.GeoDataFrame, force: bool = False
                        ) -> gpd.GeoDataFrame:
    """Square analysis cells (``grid_km``) intersecting the AOI, IDs ``SGR-G-001`` ...

    IDs are assigned north-to-south, then west-to-east, so they are deterministic.
    """
    out = cfg.path("processed", "aoi", "grid.geojson")
    if out.exists() and not force:
        return gpd.read_file(out)
    size = cfg.aoi.grid_km * 1000
    poly = aoi.to_crs(cfg.aoi.crs).geometry.union_all()
    minx, miny, maxx, maxy = poly.bounds
    x0, y1 = math.floor(minx / size) * size, math.ceil(maxy / size) * size
    cells = []
    y = y1
    while y > miny:
        x = x0
        while x < maxx:
            c = box(x, y - size, x + size, y)
            inter = c.intersection(poly).area
            if inter > 0:
                cells.append((c, inter / c.area))
            x += size
        y -= size
    code = cfg.project.district_code
    gdf = gpd.GeoDataFrame(
        {"cell_id": [f"{code}-G-{i + 1:03d}" for i in range(len(cells))],
         "frac_in_aoi": [round(f, 4) for _, f in cells]},
        geometry=[c for c, _ in cells], crs=cfg.aoi.crs,
    ).to_crs(4326)
    gdf.to_file(out, driver="GeoJSON")
    write_provenance(out, step="analysis_grid", params={"grid_km": cfg.aoi.grid_km},
                     extra={"n_cells": len(gdf)})
    return gdf
