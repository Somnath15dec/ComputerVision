"""Small precomputed files for the public dashboard (``deploy/data/``, <= 50 MB in total).

The dashboard (``app/dashboard.py``) reads only these files. Weeks come from ``detect_date``
(:func:`stubble.m5lite.week_of`), never from the stored ``week_detected``.

Files:
* ``grid_week_summary.geojson``: 5 km cells with season totals and per-week columns
  (``ha_w00`` .. ``ha_w08``, ``hs_w00`` .. ``hs_w08``), the share gap and mean clear views.
* ``weekly.csv``: district totals per week.
* ``firms_hotspots.geojson``: VIIRS detections in the AOI (date, IST time, sensor, FRP,
  confidence, week).
* ``m21_polygons_top.geojson``: the largest M2.1 candidate polygons, simplified, up to a size
  budget; ``m21_centroids.csv``: every in-season candidate as a point (area, week, score).
* ``labelled_sample.geojson``: the 200 labelled sample items at their crosshair point
  (sample id, source, label, burn date, display). No field geometry, no personal data.
* ``aoi.geojson``, ``summary.json`` (M5-lite + label-study numbers), ``figures/*.png``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from stubble.config import Config
from stubble.m5lite import week_of

POLY_BUDGET_MB = 8.0  # browser payload for the polygon layer


def _write_geojson(g: gpd.GeoDataFrame, p: Path, precision: int = 5) -> float:
    g.to_crs("EPSG:4326").to_file(p, driver="GeoJSON", COORDINATE_PRECISION=precision)
    return p.stat().st_size / 1e6


def build(cfg: Config, out: Path) -> dict[str, Any]:
    """Write the dashboard package to ``out`` and return file sizes and polygon-layer metadata."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "figures").mkdir(exist_ok=True)
    m5 = cfg.path("outputs", "m5lite")
    sizes: dict[str, float] = {}

    # grid: season + wide per-week columns
    cell = gpd.read_file(m5 / "grid_season.geojson")
    gw = pd.read_csv(m5 / "grid_week.csv")
    wide = gw.pivot(index="grid_cell", columns="week", values=["burnt_ha", "hotspots"])
    wide.columns = [f"{'ha' if a == 'burnt_ha' else 'hs'}_{w}" for a, w in wide.columns]
    cell = cell.merge(wide.reset_index(), on="grid_cell")
    cell["mean_clear_views"] = cell.mean_clear_views.round(1)
    sizes["grid_week_summary.geojson"] = _write_geojson(cell, out / "grid_week_summary.geojson")
    shutil.copy(m5 / "weekly.csv", out / "weekly.csv")

    # FIRMS points
    h = gpd.read_file(m5 / "viirs_hotspots_aoi.geojson")
    keep = ["acq_date_ist", "acq_time_ist", "sensor", "frp", "confidence", "daynight", "week",
            "grid_cell", "geometry"]
    sizes["firms_hotspots.geojson"] = _write_geojson(h[[c for c in keep if c in h]],
                                                     out / "firms_hotspots.geojson")

    # M2.1 candidates
    c = gpd.read_file(cfg.path("outputs", "m21", "candidates.gpkg"))
    c["week"] = week_of(c.detect_date, cfg)
    c = c[c.week.notna()].copy()
    c = c[["field_id", "area_ha", "week", "detect_date", "detected_window", "detection_score",
           "burn_type", "quality_flags", "status", "geometry"]]
    cen = c.to_crs(cfg.aoi.crs).geometry.representative_point().to_crs("EPSG:4326")
    pts = pd.DataFrame({"lon": cen.x.round(5), "lat": cen.y.round(5),
                        "area_ha": c.area_ha.round(2), "week": c.week,
                        "score": c.detection_score.round(2)})
    pts.to_csv(out / "m21_centroids.csv", index=False)
    sizes["m21_centroids.csv"] = (out / "m21_centroids.csv").stat().st_size / 1e6
    big = c.sort_values("area_ha", ascending=False)
    big = big.to_crs(cfg.aoi.crs)
    big["geometry"] = big.geometry.simplify(10, preserve_topology=True)
    n = min(len(big), 12000)
    while True:
        mb = _write_geojson(big.head(n), out / "m21_polygons_top.geojson")
        if mb <= POLY_BUDGET_MB or n <= 1000:
            break
        n = int(n * POLY_BUDGET_MB / mb * 0.95)
    top = big.head(n)
    sizes["m21_polygons_top.geojson"] = mb
    poly_meta = {"n_shown": int(n), "n_total_in_season": len(c),
                 "ha_shown": round(float(top.area_ha.sum()), 1),
                 "ha_total_in_season": round(float(c.area_ha.sum()), 1),
                 "min_area_ha_shown": round(float(top.area_ha.min()), 2),
                 "simplify_m": 10}

    # labelled sample
    lab = gpd.read_file(cfg.path("labels", "m3_labels.geojson"), ignore_geometry=True)
    lab = lab[lab.label_3.notna()]
    lg = gpd.GeoDataFrame(
        lab[["sample_id", "source", "sample_week", "label_3", "burn_first_seen", "display",
             "repeat_label_3"]].rename(columns={"label_3": "label"}),
        geometry=gpd.points_from_xy(lab.crosshair_lon, lab.crosshair_lat), crs="EPSG:4326")
    sizes["labelled_sample.geojson"] = _write_geojson(lg, out / "labelled_sample.geojson")

    aoi = gpd.read_file(cfg.path("processed", "aoi", "aoi.geojson"))
    sizes["aoi.geojson"] = _write_geojson(aoi[["geometry"]], out / "aoi.geojson")

    figs = {"weekly_area_vs_hotspots.png": cfg.path("reports", "m5lite"),
            "grid_maps.png": cfg.path("reports", "m5lite"),
            "cell_scatter.png": cfg.path("reports", "m5lite"),
            "hotspot_hours_ist.png": cfg.path("reports", "m5lite"),
            "labels_and_harvest_dates.png": cfg.path("reports", "m21"),
            "random_candidates_rgb.png": cfg.path("reports", "m21")}
    for f, d in figs.items():
        if (d / f).exists():
            shutil.copy(d / f, out / "figures" / f)

    m5s = json.loads((m5 / "summary.json").read_text("utf-8"))
    r2 = json.loads(cfg.path("labels", "m3_review_r2_report.json").read_text("utf-8"))
    summary = {"m5lite": m5s, "polygons": poly_meta,
               "labels": {"final_counts_first_pass": r2["final_counts_first_pass"],
                          "burnt_rate_by_source": r2["burnt_rate_by_source"],
                          "repeat_agreement": {k: r2["repeat_agreement"].get(k) for k in
                                               ("n_pairs", "agreement", "kappa",
                                                "chart_effect")},
                          "review_groups": {"r2": r2["groups"]}}}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), "utf-8")
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    return {"sizes_mb": {k: round(v, 2) for k, v in sizes.items()},
            "total_mb": round(total, 2), "polygons": poly_meta}
