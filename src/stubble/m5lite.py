"""M5-lite: M2.1 burnt area vs VIIRS hotspot counts per 5 km grid cell and week.

Weeks follow the compositing calendar: ``w00`` = 1-5 Oct (the tail of the pre-season window),
``w01`` .. ``w08`` = the weekly windows from 6 Oct (``w08`` = 24-30 Nov).

* Burnt area: M2.1 candidate polygons (``data/outputs/m21/candidates.gpkg``), each assigned
  to its ``grid_cell`` and to the week of its ``detect_date`` (the first clear view showing the
  burn; the burn happened in ``detected_window``, up to ``window_days`` earlier). The stored
  ``week_detected`` is not used: it labels detections before 6 Oct as ``w08``. Detections
  before 1 Oct are outside the season and are counted but not reported.
* Hotspots: VIIRS S-NPP + NOAA-20 (:mod:`stubble.firms`), in the AOI, by ``acq_date_ist``.
* The units differ (hectares vs detections), so the comparison is relative: ``ha_per_hotspot``
  and ``hotspot_share - area_share`` per cell. Everything is an estimate; M2.1 area is
  uncalibrated threshold output (``status = pending_field_verification``).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from stubble.config import Config

REGION_SPLIT_E = 584602.0  # EPSG:32643 easting, the midpoint of the AOI bounding box (M4 split)


def week_of(d: pd.Series, cfg: Config) -> pd.Series:
    """Season week id (``w00`` .. ``w08``) of each date; NaN outside the season."""
    t = pd.to_datetime(d)
    w0 = pd.Timestamp(cfg.windows.post.start)
    k = ((t - w0).dt.days // cfg.windows.post.step_days + 1).astype("Int64")
    ok = (t >= pd.Timestamp(cfg.season.start)) & (t <= pd.Timestamp(cfg.season.end))
    out = k.clip(lower=0).map(lambda x: f"w{x:02d}" if pd.notna(x) else None)
    return out.where(ok)


def week_table(cfg: Config) -> pd.DataFrame:
    """Week ids with their date ranges (inclusive)."""
    rows = [("w00", cfg.season.start, cfg.windows.post.start - timedelta(days=1))]
    d, k = cfg.windows.post.start, 1
    while d <= cfg.season.end:
        e = min(d + timedelta(days=cfg.windows.post.step_days - 1), cfg.season.end)
        rows.append((f"w{k:02d}", d, e))
        d, k = e + timedelta(days=1), k + 1
    return pd.DataFrame(rows, columns=["week", "start", "end"])


def burnt_by_cell_week(cfg: Config) -> tuple[pd.DataFrame, dict[str, Any]]:
    c = gpd.read_file(cfg.path("outputs", "m21", "candidates.gpkg"), ignore_geometry=True)
    c["week"] = week_of(c.detect_date, cfg)
    pre = c[pd.to_datetime(c.detect_date) < pd.Timestamp(cfg.season.start)]
    meta = {"n_polygons": len(c), "ha_total": round(float(c.area_ha.sum()), 1),
            "outside_season": {"n": len(pre), "ha": round(float(pre.area_ha.sum()), 1),
                               "detect_dates": sorted(pre.detect_date.unique().tolist())}}
    c = c[c.week.notna()]
    g = c.groupby(["grid_cell", "week"]).agg(burnt_ha=("area_ha", "sum"),
                                             n_polygons=("area_ha", "size")).reset_index()
    return g, meta


def hotspots_by_cell_week(cfg: Config, grid: gpd.GeoDataFrame) -> tuple[pd.DataFrame,
                                                                       gpd.GeoDataFrame]:
    h = gpd.read_file(cfg.path("processed", "firms", "viirs_hotspots.gpkg"))
    h["week"] = week_of(h.acq_date_ist, cfg)
    h = gpd.sjoin(h, grid[["cell_id", "geometry"]], how="left", predicate="within")
    h = h.drop(columns="index_right").rename(columns={"cell_id": "grid_cell"})
    g = h[h.week.notna()].groupby(["grid_cell", "week"]).size().rename(
        "hotspots").reset_index()
    return g, h


def observation_density(cfg: Config) -> pd.DataFrame:
    """Mean usable clear S2 views per candidate polygon, per cell (from M2.1 attributes)."""
    c = gpd.read_file(cfg.path("outputs", "m21", "candidates.gpkg"), ignore_geometry=True)
    return c.groupby("grid_cell").n_clear_mean.mean().rename("mean_clear_views").reset_index()


def run(cfg: Config) -> dict[str, Any]:
    out = cfg.path("outputs", "m5lite")
    out.mkdir(parents=True, exist_ok=True)
    grid = gpd.read_file(cfg.path("processed", "aoi", "grid.geojson"))
    weeks = week_table(cfg)
    b, meta = burnt_by_cell_week(cfg)
    hs, pts = hotspots_by_cell_week(cfg, grid)
    full = pd.MultiIndex.from_product([grid.cell_id, weeks.week],
                                      names=["grid_cell", "week"]).to_frame(index=False)
    gw = (full.merge(b, how="left").merge(hs, how="left")
          .fillna({"burnt_ha": 0.0, "n_polygons": 0, "hotspots": 0}))
    gw["n_polygons"] = gw.n_polygons.astype(int)
    gw["hotspots"] = gw.hotspots.astype(int)
    gw["burnt_ha"] = gw.burnt_ha.round(2)
    gw = gw.merge(weeks.astype({"start": str, "end": str}), on="week")
    gw.to_csv(out / "grid_week.csv", index=False)

    # season per cell
    cell = gw.groupby("grid_cell").agg(burnt_ha=("burnt_ha", "sum"),
                                       hotspots=("hotspots", "sum")).reset_index()
    cell = cell.merge(observation_density(cfg), how="left")
    cell = grid.rename(columns={"cell_id": "grid_cell"}).merge(cell, on="grid_cell")
    cen = cell.to_crs(cfg.aoi.crs).geometry.centroid
    cell["region"] = np.where(cen.x < REGION_SPLIT_E, "west", "east")
    tot_ha, tot_h = cell.burnt_ha.sum(), cell.hotspots.sum()
    cell["area_share"] = (cell.burnt_ha / tot_ha).round(4)
    cell["hotspot_share"] = (cell.hotspots / tot_h).round(4) if tot_h else 0.0
    cell["share_gap"] = (cell.hotspot_share - cell.area_share).round(4)
    cell["ha_per_hotspot"] = (cell.burnt_ha / cell.hotspots.replace(0, np.nan)).round(1)
    cell["burnt_ha"] = cell.burnt_ha.round(1)
    cell.to_file(out / "grid_season.geojson", driver="GeoJSON")
    cell.drop(columns="geometry").to_csv(out / "grid_season.csv", index=False)

    wk = gw.groupby(["week", "start", "end"]).agg(burnt_ha=("burnt_ha", "sum"),
                                                  hotspots=("hotspots", "sum")).reset_index()
    wk["burnt_ha"] = wk.burnt_ha.round(1)
    wk.to_csv(out / "weekly.csv", index=False)
    reg = cell.groupby("region").agg(burnt_ha=("burnt_ha", "sum"), hotspots=("hotspots", "sum"),
                                     n_cells=("grid_cell", "size"),
                                     mean_clear_views=("mean_clear_views", "mean")).reset_index()

    ist_hour = pd.to_datetime(pts.acq_time_ist, format="%H:%M").dt.hour
    r = cell[["burnt_ha", "hotspots"]]
    summary = {
        "m21": meta, "season_burnt_ha": round(float(tot_ha), 1),
        "season_hotspots": int(tot_h),
        "hotspots_by_sensor": {k: int(v) for k, v in pts.sensor.value_counts().items()},
        "hotspots_outside_grid": int(pts.grid_cell.isna().sum()),
        "ha_per_hotspot_district": round(float(tot_ha / tot_h), 1) if tot_h else None,
        "hotspot_ist_hour_counts": {int(k): int(v) for k, v in
                                    ist_hour.value_counts().sort_index().items()},
        "cell_spearman_burnt_vs_hotspots": round(float(
            r.burnt_ha.rank().corr(r.hotspots.rank())), 3),
        "weekly": wk.to_dict("records"),
        "region": reg.round(2).to_dict("records"),
        "top_gap_cells_fewer_hotspots_than_area_share": cell.nsmallest(10, "share_gap")[
            ["grid_cell", "region", "burnt_ha", "hotspots", "area_share", "hotspot_share",
             "share_gap", "mean_clear_views"]].to_dict("records"),
        "top_gap_cells_more_hotspots_than_area_share": cell.nlargest(10, "share_gap")[
            ["grid_cell", "region", "burnt_ha", "hotspots", "area_share", "hotspot_share",
             "share_gap", "mean_clear_views"]].to_dict("records"),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), "utf-8")
    pts.to_file(out / "viirs_hotspots_aoi.geojson", driver="GeoJSON")
    _figures(cfg, wk, cell, summary)
    return summary


def _figures(cfg: Config, wk: pd.DataFrame, cell: gpd.GeoDataFrame,
             summary: dict[str, Any]) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rep = cfg.path("reports", "m5lite")
    rep.mkdir(parents=True, exist_ok=True)
    out = []
    lab = [f"{w}\n{pd.Timestamp(s):%d %b}" for w, s in zip(wk.week, wk.start, strict=True)]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = np.arange(len(wk))
    ax.bar(x - 0.2, wk.burnt_ha / 1000, 0.4, color="#c0392b",
           label="M2.1 burnt area (k ha, by first clear burnt view)")
    ax.set_ylabel("burnt area estimate (thousand ha)")
    ax2 = ax.twinx()
    ax2.bar(x + 0.2, wk.hotspots, 0.4, color="#2c3e50", label="VIIRS hotspots (S-NPP + NOAA-20)")
    ax2.set_ylabel("VIIRS hotspot detections")
    ax.set_xticks(x, lab, fontsize=8)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left", frameon=False)
    ax.set_title("Sangrur 2025: estimated burnt area vs VIIRS hotspots per week "
                 "(estimates, not verified)", fontsize=10)
    fig.tight_layout()
    p = rep / "weekly_area_vs_hotspots.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    out.append(p)

    fig, axs = plt.subplots(1, 3, figsize=(14, 4.8))
    for ax, col, title, cmap in ((axs[0], "burnt_ha", "M2.1 burnt area (ha)", "Reds"),
                                 (axs[1], "hotspots", "VIIRS hotspots", "Greys"),
                                 (axs[2], "share_gap", "hotspot share - area share", "RdBu")):
        kw = {}
        if col == "share_gap":
            m = float(np.nanmax(np.abs(cell[col]))) or 1.0
            kw = {"vmin": -m, "vmax": m}
        cell.plot(column=col, cmap=cmap, legend=True, ax=ax, edgecolor="0.6", linewidth=0.3,
                  **kw)
        ax.set_title(title, fontsize=10)
        ax.set_axis_off()
    fig.suptitle("Per 5 km cell, 1 Oct - 30 Nov 2025 (red = fewer hotspots than the cell's "
                 "share of estimated burnt area)", fontsize=10)
    fig.tight_layout()
    p = rep / "grid_maps.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    out.append(p)

    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    for reg, col in (("west", "#e67e22"), ("east", "#2980b9")):
        x = cell[cell.region == reg]
        ax.scatter(x.burnt_ha, x.hotspots, s=14, color=col, alpha=0.8, label=reg)
    ax.set_xlabel("M2.1 burnt area per cell (ha)")
    ax.set_ylabel("VIIRS hotspots per cell")
    ax.set_title(f"Per cell (Spearman {summary['cell_spearman_burnt_vs_hotspots']})",
                 fontsize=10)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    p = rep / "cell_scatter.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    out.append(p)

    hrs = summary["hotspot_ist_hour_counts"]
    fig, ax = plt.subplots(figsize=(6, 3.2))
    ax.bar(list(hrs), list(hrs.values()), color="#2c3e50")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlim(-0.5, 23.5)
    ax.set_xlabel("acquisition hour (IST)")
    ax.set_ylabel("hotspots")
    ax.set_title("VIIRS detections by overpass hour: two daily passes, none in the late "
                 "afternoon", fontsize=9)
    fig.tight_layout()
    p = rep / "hotspot_hours_ist.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    out.append(p)
    return out


