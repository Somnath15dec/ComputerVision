"""M3 stratified labelling sample (seeded, reproducible).

* **Flagged** (``flagged_frac`` of ``n_total``): M2.1 candidate polygons, split into
  ``n_score_bins`` equal-count bins of ``detection_score`` (quantiles over all candidates with
  a finite score); each bin gets the same share. Inside a bin the share is spread across
  detection weeks with weight 1 per week (``extra_weeks`` overrides, e.g. w04 = 2).
* **Unflagged cropland** (the rest): 100 m grid cells (``unflagged_unit_px``) inside the AOI
  where at least ``unflagged_min_frac`` of pixels were green in the season (NDVI peak above
  ``harvest_detection.ndvi_green``) and are neither a raw burn label nor a cleaned candidate.
  Spread across harvest weeks the same way; cells with no detected harvest form stratum
  ``none``.

If a stratum holds fewer units than its share, all are taken and the rest goes to the
other strata of the same group in proportion to their weights. Every item carries its
stratum population and inclusion weight (population / sampled), so labels can later be
used for design-based estimates. The labelling queue order is shuffled with the same seed.

Outputs: ``data/labels/m3_sample.geojson`` and ``data/labels/m3_sample_manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from shapely.geometry import box

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.detect.harvest import BURN_LABELS
from stubble.detect.polygons import week_index
from stubble.detect.timeseries import INDEX_SCALE
from stubble.provenance import code_version, read_provenance

log = logging.getLogger(__name__)


def allocate(total: int, weights: dict[str, float], capacity: dict[str, int]) -> dict[str, int]:
    """Split ``total`` across strata by weight (largest remainder), capped by capacity.

    Capacity left over by small strata is redistributed to the others by weight.
    """
    alloc = dict.fromkeys(weights, 0)
    remaining = min(total, sum(capacity.values()))
    active = sorted(k for k in weights if weights[k] > 0 and capacity.get(k, 0) > 0)
    while remaining > 0 and active:
        wsum = sum(weights[k] for k in active)
        quota = {k: remaining * weights[k] / wsum for k in active}
        base = {k: int(np.floor(quota[k])) for k in active}
        rest = remaining - sum(base.values())
        for k in sorted(active, key=lambda k: (-(quota[k] - base[k]), k))[:rest]:
            base[k] += 1
        moved = 0
        for k in active:
            add = min(base[k], capacity[k] - alloc[k])
            alloc[k] += add
            moved += add
        remaining -= moved
        active = [k for k in active if alloc[k] < capacity[k]]
        if moved == 0:
            break
    return alloc


def _weights(keys: list[str], extra: dict[str, float]) -> dict[str, float]:
    return {k: float(extra.get(k, 1.0)) for k in keys}


def _draw(df: pd.DataFrame, strata: list[str], total: int, weights: dict[str, float],
          rng: np.random.Generator) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    """Sample ``total`` rows across ``df[strata[-1]]`` values within each ``strata[0]`` group
    (or across ``strata[0]`` alone if one stratum column)."""
    picks, info = [], {}
    if len(strata) == 1:
        groups = {"all": df}
        per_group = {"all": total}
    else:
        groups = dict(tuple(df.groupby(strata[0], sort=True)))
        per_group = allocate(total, dict.fromkeys(groups, 1.0),
                             {k: len(v) for k, v in groups.items()})
    col = strata[-1]
    for g, sub in groups.items():
        cap = sub[col].value_counts().to_dict()
        keys = sorted(cap)
        al = allocate(per_group[g], {k: weights.get(k, 1.0) for k in keys}, cap)
        for k in keys:
            pool = sub[sub[col] == k]
            n = al[k]
            info[f"{g}|{k}"] = {"population": len(pool), "sampled": n}
            if n:
                idx = rng.choice(len(pool), n, replace=False)
                pick = pool.iloc[np.sort(idx)].copy()
                pick["stratum"] = f"{g}|{k}" if len(strata) > 1 else str(k)
                pick["stratum_population"] = len(pool)
                pick["inclusion_weight"] = round(len(pool) / n, 3)
                picks.append(pick)
    return (pd.concat(picks) if picks else df.iloc[:0]), info


def unflagged_cells(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray) -> pd.DataFrame:
    """Candidate 100 m cells of unflagged cropland with their modal harvest week."""
    lc = cfg.labelling
    k = lc.unflagged_unit_px
    src = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    with rasterio.open(src) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        lab = ds.read(ix["label"])
        peak = ds.read(ix["ndvi_peak"])
        hdoy = ds.read(ix["harvest_doy"])
    with rasterio.open(cfg.path("outputs", "m21", "burnt_area.tif")) as ds:
        burnt = ds.read(1) == 1
    ok = (aoi_mask & (peak > cfg.harvest_detection.ndvi_green * INDEX_SCALE)
          & ~np.isin(lab, BURN_LABELS) & ~burnt)
    del peak, burnt
    h, w = (grid.height // k) * k, (grid.width // k) * k
    blk = lambda a: a[:h, :w].reshape(h // k, k, w // k, k)  # noqa: E731
    frac = blk(ok).mean(axis=(1, 3))
    inside = blk(aoi_mask).all(axis=(1, 3))
    wk = np.where(ok & (hdoy > 0), week_index(hdoy, cfg), 0).astype(np.int16)
    del lab, hdoy
    wins = [x.id for x in cfg.compositing_windows() if x.id != "pre"]
    counts = np.stack([blk(wk == i).sum(axis=(1, 3)) for i in range(len(wins) + 1)])
    sel = inside & (frac >= lc.unflagged_min_frac)
    r, c = np.nonzero(sel)
    cnt = counts[:, r, c]
    harvested = cnt[1:].sum(axis=0)
    mode = np.where(cnt[1:].max(axis=0) * 2 >= harvested, cnt[1:].argmax(axis=0) + 1, 0)
    mode = np.where(harvested * 2 >= cnt.sum(axis=0), mode, 0)  # mostly harvested, one week
    return pd.DataFrame({"cell_row": r, "cell_col": c, "frac_unflagged_crop": frac[r, c].round(3),
                         "week": [wins[m - 1] if m > 0 else "none" for m in mode]})


def _cell_geom(grid: GridSpec, row: int, col: int, k: int):
    t = grid.transform
    x0, y0 = t.c + col * k * t.a, t.f + row * k * t.e
    return box(x0, y0 + k * t.e, x0 + k * t.a, y0)


def _uid(prefix: str, geom) -> str:
    return f"{prefix}-U-" + hashlib.sha1(geom.wkb).hexdigest()[:8]


def build_sample(cfg: Config, grid: GridSpec, aoi_mask: np.ndarray) -> dict[str, Any]:
    """Draw the M3 sample; write the GeoJSON and the manifest. Returns the manifest."""
    lc = cfg.labelling
    rng = np.random.default_rng(lc.seed)
    cand_path = cfg.path("outputs", "m21", "candidates.gpkg")
    cands = gpd.read_file(cand_path).to_crs(grid.crs)
    n_flag = round(lc.n_total * lc.flagged_frac)
    n_unfl = lc.n_total - n_flag
    wins = [x.id for x in cfg.compositing_windows() if x.id != "pre"]

    scored = cands[np.isfinite(cands.detection_score)].copy()
    q = np.quantile(scored.detection_score, np.linspace(0, 1, lc.n_score_bins + 1))
    scored["score_bin"] = pd.qcut(scored.detection_score, lc.n_score_bins,
                                  labels=[f"b{i + 1}" for i in range(lc.n_score_bins)]
                                  ).astype(str)
    fw = _weights(wins, lc.extra_weeks)
    flag, flag_info = _draw(scored.sort_values("field_id"), ["score_bin", "week_detected"],
                            n_flag, fw, rng)
    flag = flag.assign(source="flagged", sample_week=flag.week_detected,
                       default_before=flag.window_start, default_after=flag.detect_date)

    cells = unflagged_cells(cfg, grid, aoi_mask)
    uw = _weights([*wins, "none"], lc.extra_weeks)
    unfl, unfl_info = _draw(cells, ["week"], n_unfl, uw, rng)
    geoms = [_cell_geom(grid, r.cell_row, r.cell_col, lc.unflagged_unit_px)
             for r in unfl.itertuples()]
    unfl = gpd.GeoDataFrame(unfl.reset_index(drop=True), geometry=geoms, crs=grid.crs)
    unfl["field_id"] = [_uid(cfg.project.district_code, g) for g in unfl.geometry]
    wmap = {x.id: x for x in cfg.compositing_windows()}
    unfl["default_before"] = [wmap[w].start.isoformat() if w in wmap
                              else cfg.windows.post.start.isoformat() for w in unfl.week]
    unfl["default_after"] = [(wmap[w].end if w in wmap else cfg.windows.post.end).isoformat()
                             for w in unfl.week]
    unfl = unfl.assign(source="unflagged_cropland", sample_week=unfl.week, score_bin="",
                       detection_score=np.nan, area_ha=unfl.area / 1e4)

    keep = ["field_id", "source", "stratum", "score_bin", "sample_week", "detection_score",
            "area_ha", "default_before", "default_after", "stratum_population",
            "inclusion_weight", "geometry"]
    extra = ["burn_type", "detected_window", "s1_pre", "s1_post", "s1_orbit", "harvest_date",
             "quality_flags"]
    flag = gpd.GeoDataFrame(flag, geometry="geometry", crs=grid.crs)
    allg = pd.concat([flag[keep + [c for c in extra if c in flag]], unfl[keep]],
                     ignore_index=True)
    allg = gpd.GeoDataFrame(allg, geometry="geometry", crs=grid.crs)
    order = rng.permutation(len(allg))
    allg["queue_index"] = np.argsort(order)
    allg = allg.sort_values("queue_index").reset_index(drop=True)
    allg.insert(0, "sample_id", [f"M3-{i + 1:04d}" for i in range(len(allg))])

    out_dir = cfg.path("labels")
    out_dir.mkdir(parents=True, exist_ok=True)
    gj = out_dir / "m3_sample.geojson"
    allg.to_crs(4326).to_file(gj, driver="GeoJSON")
    cprov = read_provenance(cand_path) or {}
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "code_version": code_version(), "seed": lc.seed,
        "config": lc.model_dump(),
        "candidates": {"file": cand_path.name, "code_version": cprov.get("code_version"),
                       "params_hash": cprov.get("params_hash"), "n": len(cands),
                       "n_scored": len(scored)},
        "score_definition": "detection_score = polygon mean of min(dNBR/dnbr_short_min, "
                            "albedo_drop/albedo_rel_drop_min, dNBR2/dnbr2_min); rule-based "
                            "ranking, not a probability",
        "score_bin_edges": [round(float(v), 4) for v in q],
        "week_weights_flagged": fw, "week_weights_unflagged": uw,
        "n_flagged": len(flag), "n_unflagged": len(unfl),
        "unflagged_population_cells": len(cells),
        "strata": {"flagged": flag_info, "unflagged_cropland": unfl_info},
        "per_week": allg.groupby(["source", "sample_week"]).size().rename("n").reset_index()
                    .to_dict("records"),
        "per_bin": flag.groupby("score_bin").size().to_dict(),
        "per_burn_type": flag.groupby("burn_type").size().to_dict() if "burn_type" in flag
        else {},
        "sample_ids": allg[["sample_id", "field_id", "source", "stratum"]].to_dict("records"),
        "geojson": gj.name,
    }
    (out_dir / "m3_sample_manifest.json").write_text(json.dumps(manifest, indent=1,
                                                                default=str),
                                                     encoding="utf-8")
    log.info("sample: %d flagged + %d unflagged", len(flag), len(unfl))
    return manifest
