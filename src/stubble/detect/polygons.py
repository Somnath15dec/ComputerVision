"""Clean the per-pixel baseline mask, vectorise candidates, attach quality and S1 support.

Polygons are connected patches of pixels that share the same *first post-burn observation
date*, so one polygon = one detection event (adjacent fields first seen burnt on different
dates stay separate). Each polygon carries:

* burn-date window ``[window_start, detect_date]`` (modal over its pixels),
* per-pixel quality summaries (clear-obs counts, gaps, haze, single-observation support),
* Sentinel-1 VV/VH change across the window (**supporting evidence only**; S1 never creates
  or removes a candidate in M2),
* ``status = pending_field_verification`` and ``burn_confidence = None`` (calibrated
  confidence arrives with the model in M4).

Outputs::

    data/outputs/m2/burnt_area.tif         burnt (0/1), week index (1..8) - COG
    data/outputs/m2/candidate_labels.tif   int32 polygon label per pixel
    data/outputs/m2/candidates.gpkg        polygons + attributes (EPSG:4326)
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import shapes
from scipy import ndimage
from shapely.geometry import shape

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.data.composites import NODATA, _profile, _to_cog, db_int16_to_linear
from stubble.detect.timeseries import (
    DOY_BASE,
    INDEX_SCALE,
    doy_to_date,
)
from stubble.provenance import read_provenance, write_provenance

log = logging.getLogger(__name__)

STATUS = "pending_field_verification"


# ---------------------------------------------------------------------------- mask cleaning

def clean_mask(mask: np.ndarray, radius_px: int) -> np.ndarray:
    """Morphological opening with a ``(2r+1)`` square structuring element."""
    if radius_px <= 0:
        return mask.copy()
    st = np.ones((2 * radius_px + 1, 2 * radius_px + 1), dtype=bool)
    return ndimage.binary_opening(mask, structure=st)


def min_patch_px(min_ha: float, resolution_m: float) -> int:
    """Smallest patch size in pixels for an area threshold in hectares."""
    return math.ceil(min_ha * 1e4 / resolution_m ** 2 - 1e-9)


def label_by_event(mask: np.ndarray, event: np.ndarray, min_px: int, connectivity: int = 8
                   ) -> np.ndarray:
    """Label connected patches of ``mask`` sharing the same ``event`` value; drop small ones.

    Returns int32 labels 1..N (0 = background), numbered in order of event value.
    """
    st = np.ones((3, 3), bool) if connectivity == 8 else ndimage.generate_binary_structure(2, 1)
    out = np.zeros(mask.shape, dtype=np.int32)
    nxt = 0
    for ev in np.unique(event[mask]):
        lab, n = ndimage.label(mask & (event == ev), structure=st)
        if n == 0:
            continue
        sizes = np.bincount(lab.ravel())
        keep = np.flatnonzero(sizes >= min_px)
        keep = keep[keep > 0]
        remap = np.zeros(n + 1, dtype=np.int32)
        remap[keep] = np.arange(nxt + 1, nxt + 1 + len(keep), dtype=np.int32)
        sel = lab > 0
        out[sel] = remap[lab[sel]]
        nxt += len(keep)
    return out


def week_index(doy: np.ndarray, cfg: Config) -> np.ndarray:
    """Map day-of-year to the 1-based post-harvest week (0 = none)."""
    out = np.zeros(doy.shape, dtype=np.int16)
    wins = [w for w in cfg.compositing_windows() if w.id != "pre"]
    for i, w in enumerate(wins, 1):
        a, b = w.start.toordinal() - DOY_BASE, w.end.toordinal() - DOY_BASE
        out[(doy >= a) & (doy <= b)] = i
    return out


# ---------------------------------------------------------------------------- helpers

def _read_bands(path: Path) -> dict[str, np.ndarray]:
    with rasterio.open(path) as ds:
        return {d: ds.read(i + 1) for i, d in enumerate(ds.descriptions)}


def _group_mean(vals: np.ndarray, lab: np.ndarray, n: int, valid: np.ndarray | None = None
                ) -> np.ndarray:
    w = np.ones(len(vals)) if valid is None else valid.astype(float)
    s = np.bincount(lab, weights=np.where(w > 0, vals, 0.0), minlength=n + 1)
    c = np.bincount(lab, weights=w, minlength=n + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (s / c)[1:]


def _group_ext(vals: np.ndarray, lab: np.ndarray, n: int, fn: str) -> np.ndarray:
    init = -np.inf if fn == "max" else np.inf
    out = np.full(n + 1, init)
    (np.maximum if fn == "max" else np.minimum).at(out, lab, vals)
    return out[1:]


def _modal_window(lab: np.ndarray, det: np.ndarray, start: np.ndarray) -> pd.DataFrame:
    df = pd.DataFrame({"lab": lab, "det": det, "start": start})
    c = df.groupby(["lab", "det", "start"], sort=False).size().rename("n").reset_index()
    c = c.sort_values(["lab", "n", "start"], ascending=[True, False, False])
    best = c.drop_duplicates("lab").set_index("lab")
    span = df.groupby("lab")["start"].agg(["min", "max"])
    tot = df.groupby("lab").size()
    best["frac_modal"] = best["n"] / tot
    best["start_min"], best["start_max"] = span["min"], span["max"]
    return best


def _s1_acqs(cfg: Config) -> list[tuple[date, str, Path]]:
    out = []
    for p in sorted(cfg.path("raw", "s1").glob("*.tif")):
        if p.name.endswith(".tmp.tif"):
            continue
        s, orbit = p.stem.split("_")
        out.append((date(int(s[:4]), int(s[4:6]), int(s[6:8])), orbit, p))
    return out


def s1_support(cfg: Config, flat_idx: np.ndarray, lab: np.ndarray, n: int,
               win_start: np.ndarray, det: np.ndarray) -> pd.DataFrame:
    """Per-polygon S1 change across the burn window (same orbit direction).

    ``pre`` = latest acquisition on/before ``window_start``; ``post`` = earliest on/after the
    first post-burn S2 observation. Means are taken in linear power, change reported in dB.
    The orbit direction with the shorter pre->post span is kept. Supporting evidence only.
    """
    acqs = _s1_acqs(cfg)
    scale = cfg.sentinel1.db_scale
    npx = np.bincount(lab, minlength=n + 1)[1:].astype(float)
    rows = []
    for day, orbit, p in acqs:
        with rasterio.open(p) as ds:
            ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
            vv = db_int16_to_linear(ds.read(ix["VV_db"]).ravel()[flat_idx], scale)
            vh = db_int16_to_linear(ds.read(ix["VH_db"]).ravel()[flat_idx], scale)
        ok = np.isfinite(vv) & np.isfinite(vh)
        cnt = np.bincount(lab, weights=ok.astype(float), minlength=n + 1)[1:]
        rows.append({
            "day": day.toordinal(), "orbit": orbit, "file": p.name,
            "vv": _group_mean(np.nan_to_num(vv), lab, n, ok),
            "vh": _group_mean(np.nan_to_num(vh), lab, n, ok),
            "ok": cnt / np.maximum(npx, 1) >= cfg.s1_support.min_valid_frac,
        })
    best: dict[str, np.ndarray] = {}
    for orbit in sorted({r["orbit"] for r in rows}):
        rs = sorted((r for r in rows if r["orbit"] == orbit), key=lambda r: r["day"])
        pre_i = np.full(n, -1)
        post_i = np.full(n, -1)
        for i, r in enumerate(rs):
            pre_i = np.where(r["ok"] & (r["day"] <= win_start), i, pre_i)
        for i in range(len(rs) - 1, -1, -1):
            r = rs[i]
            post_i = np.where(r["ok"] & (r["day"] >= det), i, post_i)
        have = (pre_i >= 0) & (post_i >= 0)
        days = np.array([r["day"] for r in rs])
        span = np.where(have, days[np.maximum(post_i, 0)] - days[np.maximum(pre_i, 0)], 10**6)

        def pick(key: str, idx: np.ndarray, rs: list = rs) -> np.ndarray:
            stack = np.stack([r[key] for r in rs])
            return stack[np.maximum(idx, 0), np.arange(n)]

        with np.errstate(divide="ignore", invalid="ignore"):
            cand = {
                "span": span,
                "vv_change_db": np.where(have, 10 * np.log10(pick("vv", post_i)
                                                            / pick("vv", pre_i)), np.nan),
                "vh_change_db": np.where(have, 10 * np.log10(pick("vh", post_i)
                                                            / pick("vh", pre_i)), np.nan),
                "s1_pre": np.where(have, days[np.maximum(pre_i, 0)], 0),
                "s1_post": np.where(have, days[np.maximum(post_i, 0)], 0),
                "s1_orbit": np.where(have, orbit, ""),
            }
        if not best:
            best = cand
        else:
            better = cand["span"] < best["span"]
            best = {k: np.where(better, cand[k], best[k]) for k in best}
    df = pd.DataFrame(best)
    df["vvvh_ratio_change_db"] = df["vv_change_db"] - df["vh_change_db"]
    df["s1_pair_days"] = np.where(df["span"] < 10**6, df["span"], np.nan)
    for k in ("s1_pre", "s1_post"):
        df[k] = [date.fromordinal(int(v)).isoformat() if v else None for v in df[k]]
    return df.drop(columns="span")


def _field_ids(gdf: gpd.GeoDataFrame, code: str) -> tuple[list[str], list[str]]:
    c = gdf.geometry.representative_point()
    order = np.lexsort((np.round(c.x, -1), -np.round(c.y, -1)))  # N->S, then W->E
    ids = [""] * len(gdf)
    for rank, i in enumerate(order, 1):
        ids[i] = f"{code}-{rank:04d}"
    hashes = [hashlib.sha1(g.wkb).hexdigest()[:12] for g in gdf.geometry]
    return ids, hashes


# ---------------------------------------------------------------------------- main

@dataclass
class Product:
    """What differs between the M2 baseline and the M2.1 harvest-aware candidates."""

    name: str                     # output folder under data/outputs
    det_path: Path
    raw_mask: Callable[[dict[str, np.ndarray]], np.ndarray]
    event_band: str               # first burnt-view day-of-year
    start_band: str               # last unburnt-view day-of-year
    index_bands: list[str]        # x10000 bands averaged per polygon
    id_prefix: str
    detection_source: str
    params: dict[str, Any]
    opening_radius_px: int
    min_patch_ha: float
    connectivity: int
    haze_band: str | None = None  # per-pixel 0/1 haze at detection
    extra: Callable[..., dict] | None = None


def baseline_product(cfg: Config) -> Product:
    """M2 baseline (brief): dNBR against the pre-season median. IDs ``SGR-B-0001``."""
    d = cfg.detection
    return Product(
        name="m2", det_path=cfg.path("processed", "m2", "s2_pixel_detect.tif"),
        raw_mask=lambda b: b["burnt_raw"] == 1, event_band="detect_doy",
        start_band="window_start_doy",
        index_bands=["dnbr_detect", "dnbr_peak", "dnbr_local", "nbr_pre"],
        id_prefix=f"{cfg.project.district_code}-B", detection_source="s2_dnbr_baseline",
        params={"detection": d.model_dump(), "quality": cfg.quality.model_dump(),
                "s1_support": cfg.s1_support.model_dump()},
        opening_radius_px=d.opening_radius_px, min_patch_ha=d.min_patch_ha,
        connectivity=d.connectivity, haze_band="haze_at_detect")


def harvest_product(cfg: Config) -> Product:
    """M2.1 harvest-aware candidates. IDs ``SGR-0001``."""
    from stubble.detect.harvest import BURNT

    b = cfg.burn_detection

    def extra(det: dict, g: Callable, lab: np.ndarray, n: int) -> dict:
        hd = g(det["harvest_doy"])
        mode = (pd.DataFrame({"lab": lab, "h": hd}).groupby(["lab", "h"]).size()
                .rename("n").reset_index().sort_values(["lab", "n"], ascending=[True, False])
                .drop_duplicates("lab").set_index("lab")["h"].reindex(np.arange(1, n + 1)))
        return {"harvest_date": [doy_to_date(int(v)).isoformat() for v in mode],
                "n_hazy_skipped_mean": np.round(
                    _group_mean(g(det["n_hazy_skipped"]).astype(float), lab, n), 2),
                "n_views_post_harvest_mean": np.round(
                    _group_mean(g(det["n_views_post_harvest"]).astype(float), lab, n), 1)}

    return Product(
        name="m21", det_path=cfg.path("processed", "m21", "s2_harvest_burn.tif"),
        raw_mask=lambda d: d["label"] == BURNT, event_band="burn_doy",
        start_band="burn_prev_doy",
        index_bands=["dnbr_short", "albedo_drop_rel", "dnbr2", "nbr_prev", "nbr_burn",
                     "albedo_burn", "albedo_harvest", "nbr_harvest", "ndvi_peak"],
        id_prefix=cfg.project.district_code, detection_source="s2_harvest_aware_v1",
        params={"harvest": cfg.harvest_detection.model_dump(), "burn": b.model_dump(),
                "quality": cfg.quality.model_dump(),
                "s1_support": cfg.s1_support.model_dump()},
        opening_radius_px=b.opening_radius_px, min_patch_ha=b.min_patch_ha,
        connectivity=b.connectivity, extra=extra)


def build_candidates(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray,
                     cells: gpd.GeoDataFrame, product: Product | None = None
                     ) -> dict[str, Any]:
    """Clean, vectorise and attribute candidate burn polygons. Returns summary stats."""
    pr = product or baseline_product(cfg)
    q_path = cfg.path("processed", "m2", "s2_quality.tif")
    det = _read_bands(pr.det_path)
    q = _read_bands(q_path)
    px_ha = grid.resolution ** 2 / 1e4

    raw = pr.raw_mask(det) & aoi_mask
    opened = clean_mask(raw, pr.opening_radius_px)
    min_px = min_patch_px(pr.min_patch_ha, grid.resolution)
    labels = label_by_event(opened, det[pr.event_band], min_px, pr.connectivity)
    cleaned = labels > 0
    weeks = np.where(cleaned, week_index(det[pr.event_band], cfg), 0).astype(np.int16)
    n = int(labels.max())
    log.info("%d candidate polygons", n)

    out_dir = cfg.path("outputs", pr.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    burnt_tif = out_dir / "burnt_area.tif"
    tmp = burnt_tif.with_suffix(".tmp.tif")
    with rasterio.open(tmp, "w", **_profile(grid, 2)) as ds:
        ds.write(cleaned.astype(np.int16), 1)
        ds.set_band_description(1, "burnt")
        ds.write(weeks, 2)
        ds.set_band_description(2, "week_detected")
    _to_cog(tmp, burnt_tif)
    os.remove(tmp)
    lab_tif = out_dir / "candidate_labels.tif"
    prof = _profile(grid, 1) | {"dtype": "int32", "nodata": 0, "predictor": 2}
    with rasterio.open(lab_tif, "w", **prof) as ds:
        ds.write(labels, 1)
        ds.set_band_description(1, "polygon_label")

    # ------------------------------------------------------------ per-polygon attributes
    flat = np.flatnonzero(labels.ravel())
    lab = labels.ravel()[flat]
    g = lambda a: a.ravel()[flat]  # noqa: E731
    sc = lambda a: np.where(g(a) == NODATA, np.nan, g(a) / INDEX_SCALE)  # noqa: E731

    npx = np.bincount(lab, minlength=n + 1)[1:]
    att = pd.DataFrame({"label": np.arange(1, n + 1), "n_px": npx,
                        "area_ha": np.round(npx * px_ha, 2)})
    for k in pr.index_bands:
        v = sc(det[k])
        att[f"{k}_mean"] = np.round(_group_mean(np.nan_to_num(v), lab, n, np.isfinite(v)), 4)
    sup = g(det["n_support"])
    att["n_support_mean"] = np.round(_group_mean(sup.astype(float), lab, n), 2)
    att["frac_single_obs"] = np.round(_group_mean((sup == 1).astype(float), lab, n), 3)
    att["haze_frac"] = (np.round(_group_mean(g(det[pr.haze_band]).astype(float), lab, n), 3)
                        if pr.haze_band else 0.0)
    att["n_clear_mean"] = np.round(_group_mean(g(q["n_clear"]).astype(float), lab, n), 1)
    att["n_clear_min"] = _group_ext(g(q["n_clear"]).astype(float), lab, n, "min").astype(int)
    att["n_clear_pre_min"] = _group_ext(g(q["n_clear_pre"]).astype(float), lab, n,
                                        "min").astype(int)
    att["n_hazy_mean"] = np.round(_group_mean(g(q["n_hazy"]).astype(float), lab, n), 2)
    att["max_gap_days"] = _group_ext(g(q["max_gap_days"]).astype(float), lab, n,
                                     "max").astype(int)
    att["days_since_last_clear_max"] = _group_ext(
        g(q["days_since_last_clear"]).astype(float), lab, n, "max").astype(int)
    if pr.extra:
        for k, v in pr.extra(det, g, lab, n).items():
            att[k] = v

    mw = _modal_window(lab, g(det[pr.event_band]), g(det[pr.start_band]))
    mw = mw.reindex(np.arange(1, n + 1))
    det_doy = mw["det"].to_numpy().astype(int)
    start_doy = mw["start"].to_numpy().astype(int)
    att["detect_date"] = [doy_to_date(v).isoformat() for v in det_doy]
    att["window_start"] = [doy_to_date(v).isoformat() for v in start_doy]
    att["detected_window"] = att["window_start"] + "/" + att["detect_date"]
    att["window_days"] = det_doy - start_doy
    att["window_start_spread_days"] = (mw["start_max"] - mw["start_min"]).to_numpy().astype(int)
    wk = week_index(det_doy, cfg)
    wins = [w.id for w in cfg.compositing_windows() if w.id != "pre"]
    att["week_detected"] = [wins[i - 1] for i in wk]
    wq = {w: g(q[f"n_clear_{w}"]).astype(float) for w in wins}
    att["n_clear_week_mean"] = np.round(
        _week_mean(wq, lab, n, att["week_detected"].to_numpy()), 2)

    s1 = s1_support(cfg, flat, lab, n, start_doy + DOY_BASE, det_doy + DOY_BASE)
    for c in s1.columns:
        att[c] = s1[c].round(3) if s1[c].dtype.kind == "f" else s1[c].to_numpy()
    att["s1_role"] = "supporting_only"

    qd = cfg.quality
    flags = []
    for r in att.itertuples():
        f = []
        if r.frac_single_obs >= 0.5:
            f.append("single_observation")
        if r.haze_frac >= 0.5:
            f.append("haze_at_detection")
        if r.window_days > qd.wide_window_days:
            f.append("wide_window")
        if r.n_clear_pre_min < 2:
            f.append("thin_preseason")
        if not isinstance(r.s1_orbit, str) or not r.s1_orbit:
            f.append("no_s1_pair")
        flags.append(",".join(f))
    att["quality_flags"] = flags
    att["single_observation"] = att["frac_single_obs"] >= 0.5
    att["burn_confidence"] = None
    att["confidence_note"] = "not calibrated: threshold rules only (model + calibration in M4)"
    att["detection_source"] = pr.detection_source
    att["status"] = STATUS

    # ------------------------------------------------------------ vectorise
    geoms: dict[int, list] = {}
    for gj, v in shapes(labels, mask=cleaned, connectivity=8, transform=grid.transform):
        geoms.setdefault(int(v), []).append(shape(gj))
    from shapely.ops import unary_union
    att["geometry"] = [unary_union(geoms[i]) if len(geoms[i]) > 1 else geoms[i][0]
                       for i in att["label"]]
    gdf = gpd.GeoDataFrame(att, geometry="geometry", crs=grid.crs)
    ids, hashes = _field_ids(gdf, pr.id_prefix)
    gdf.insert(0, "field_id", ids)
    gdf.insert(1, "geom_hash", hashes)
    cu = cells.to_crs(grid.crs)[["cell_id", "geometry"]]
    pts = gpd.GeoDataFrame({"label": gdf["label"]},
                           geometry=gdf.geometry.representative_point(), crs=grid.crs)
    j = gpd.sjoin(pts, cu, how="left", predicate="within").drop_duplicates("label")
    gdf.insert(2, "grid_cell", j.set_index("label").loc[gdf["label"], "cell_id"].to_numpy())
    gdf = gdf.sort_values("field_id").reset_index(drop=True)
    out = out_dir / "candidates.gpkg"
    if out.exists():
        out.unlink()
    gdf.to_crs(4326).to_file(out, driver="GPKG", layer="candidates")

    stats = {
        "aoi_ha": round(float(aoi_mask.sum() * px_ha), 1),
        "raw_ha": round(float(raw.sum() * px_ha), 1),
        "after_opening_ha": round(float(opened.sum() * px_ha), 1),
        "final_ha": round(float(cleaned.sum() * px_ha), 1),
        "min_patch_px": min_px,
        "n_polygons": n,
        "n_single_observation": int(gdf["single_observation"].sum()),
        "single_observation_ha": round(float(gdf.loc[gdf.single_observation, "area_ha"].sum()),
                                       1),
        "n_no_s1_pair": int(gdf["quality_flags"].str.contains("no_s1_pair").sum()),
    }
    inputs = [{"file": p.name, "provenance": (read_provenance(p) or {}).get("params_hash")}
              for p in (pr.det_path, q_path)]
    inputs += [{"file": p.name} for _, _, p in _s1_acqs(cfg)]
    for p in (burnt_tif, lab_tif, out):
        write_provenance(p, step=f"{pr.name}_candidates", params=pr.params, inputs=inputs,
                         extra={"stats": stats})
    return stats


def _week_mean(wq: dict[str, np.ndarray], lab: np.ndarray, n: int, week_ids: np.ndarray
               ) -> np.ndarray:
    """Mean clear-obs count of each polygon in its own detection week."""
    out = np.full(n, np.nan)
    for w, vals in wq.items():
        m = _group_mean(vals, lab, n)
        sel = week_ids == w
        out[sel] = m[sel]
    return out
