"""M2.1 report: harvest-aware detection vs the M2 baseline.

All numbers are computed from cached rasters and polygons. Nothing is hand-entered.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap

from stubble.config import Config
from stubble.data.composites import NODATA
from stubble.detect.harvest import BURNT, LABELS, NO_HARVEST, NOT_BURNT, UNOBSERVED
from stubble.detect.timeseries import DOY_BASE, INDEX_SCALE, _s2_days
from stubble.report_m2 import _best_file, _chip, _extent, _md_table, _read, _rgb

log = logging.getLogger(__name__)


def _weeks(cfg: Config) -> list[Any]:
    return [w for w in cfg.compositing_windows() if w.id != "pre"]


def _doy(d: date) -> int:
    return d.toordinal() - DOY_BASE


# ---------------------------------------------------------------------------- figures

def fig_labels_harvest(cfg: Config, aoi: gpd.GeoDataFrame, out: Path, factor: int = 6) -> Path:
    src = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    lab, t = _read(src, "label", factor)
    hd, _ = _read(src, "harvest_doy", factor)
    fig, axes = plt.subplots(1, 2, figsize=(15, 9))
    cm = ListedColormap(["#ffffff", "#bdbdbd", "#7b3294", "#a6d96a", "#d7191c"])
    axes[0].imshow(lab, extent=_extent(t, lab.shape), cmap=cm,
                   norm=BoundaryNorm(np.arange(-0.5, 5), cm.N), interpolation="nearest")
    handles = [plt.Rectangle((0, 0), 1, 1, color=cm(i)) for i in (1, 2, 3, 4)]
    axes[0].legend(handles, [LABELS[i] for i in (1, 2, 3, 4)], loc="lower left", fontsize=8)
    axes[0].set_title("Per-pixel label (raw, before cleaning)\nburnt = model estimate, "
                      "pending field verification", fontsize=10)
    wins = _weeks(cfg)
    lo, hi = _doy(cfg.windows.pre.start), _doy(cfg.season.end)
    img = np.where(hd > 0, hd, np.nan).astype(float)
    im = axes[1].imshow(img, extent=_extent(t, hd.shape), cmap="viridis", vmin=lo, vmax=hi,
                        interpolation="nearest")
    cb = fig.colorbar(im, ax=axes[1], fraction=0.035)
    ticks = [_doy(w.start) for w in wins]
    cb.set_ticks(ticks)
    cb.set_ticklabels([f"{w.start:%d %b}" for w in wins])
    axes[1].set_facecolor("#eeeeee")
    axes[1].set_title(f"Harvest date (first clear view NDVI < {cfg.harvest_detection.ndvi_harvest}"
                      f" after NDVI > {cfg.harvest_detection.ndvi_green})", fontsize=10)
    for ax in axes:
        aoi.boundary.plot(ax=ax, color="black", linewidth=0.7)
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def fig_w04_windows(cfg: Config, aoi: gpd.GeoDataFrame, aoi_m: np.ndarray, out: Path,
                    factor: int = 6) -> tuple[Path, pd.DataFrame]:
    """Burn-date windows overlapping w04, as a map of window length and a window table."""
    src = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    burnt_p = cfg.path("outputs", "m21", "burnt_area.tif")
    w4 = next(w for w in _weeks(cfg) if w.id == "w04")
    a, b = _doy(w4.start), _doy(w4.end)
    s, _ = _read(src, "burn_prev_doy")
    e, _ = _read(src, "burn_doy")
    bu, _ = _read(burnt_p, "burnt")
    sel = (bu == 1) & aoi_m & (e >= a) & (s <= b)
    tab = (pd.DataFrame({"start": s[sel], "end": e[sel]}).value_counts().rename("px")
           .reset_index().sort_values("px", ascending=False))
    tab["window"] = [f"{date.fromordinal(int(x) + DOY_BASE):%d %b} - "
                     f"{date.fromordinal(int(y) + DOY_BASE):%d %b}"
                     for x, y in zip(tab.start, tab.end, strict=True)]
    tab["days"] = tab.end - tab.start
    tab["ha"] = (tab.px / 100).round(1)
    tab["inside_w04"] = (tab.start >= a) & (tab.end <= b)
    tab = tab[["window", "days", "ha", "inside_w04"]]
    del s, e, bu, sel

    sd, t = _read(src, "burn_prev_doy", factor)
    ed, _ = _read(src, "burn_doy", factor)
    bd, _ = _read(burnt_p, "burnt", factor)
    m = (bd == 1) & (ed >= a) & (sd <= b)
    img = np.where(m, ed - sd, np.nan).astype(float)
    fig, axes = plt.subplots(1, 2, figsize=(15, 8.5), gridspec_kw={"width_ratios": [1.2, 1]})
    axes[0].set_facecolor("#eeeeee")
    im = axes[0].imshow(img, extent=_extent(t, img.shape), cmap="magma_r", vmin=0, vmax=15,
                        interpolation="nearest")
    fig.colorbar(im, ax=axes[0], fraction=0.035, label="window length (days)")
    aoi.boundary.plot(ax=axes[0], color="black", linewidth=0.7)
    axes[0].set_xticks([])
    axes[0].set_yticks([])
    axes[0].set_title(f"Burn-date windows overlapping w04 ({w4.label})\n"
                      "[last clear unburnt view, first clear burnt view]", fontsize=10)
    top = tab.head(15).iloc[::-1]
    axes[1].barh(top.window, top.ha / 1000,
                 color=["#d7191c" if x else "#fdae61" for x in top.inside_w04])
    axes[1].set_xlabel("thousand ha (cleaned candidates, unverified)")
    axes[1].set_title("15 most common windows overlapping w04\n(red = window entirely inside "
                      "w04)", fontsize=10)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out, tab


def fig_random_chips(cfg: Config, cands: gpd.GeoDataFrame, out: Path, n: int = 20,
                     seed: int = 0) -> tuple[Path, list[str]]:
    """Pre (last unburnt view) / post (first burnt view) RGB chips for random polygons."""
    by_day = _s2_days(cfg)
    smp = cands.sample(min(n, len(cands)), random_state=seed).sort_values("field_id")
    ncol_pairs = 2
    nrow = int(np.ceil(len(smp) / ncol_pairs))
    fig, axes = plt.subplots(nrow, 2 * ncol_pairs, figsize=(12, 3.1 * nrow), squeeze=False)
    for ax in axes.ravel():
        ax.axis("off")
    for k, r in enumerate(smp.itertuples()):
        row, col = divmod(k, ncol_pairs)
        x0, y0, x1, y1 = r.geometry.bounds
        mg = max(200, 0.5 * max(x1 - x0, y1 - y0))
        bb = (x0 - mg, y0 - mg, x1 + mg, y1 + mg)
        for j, (d, tag) in enumerate(((r.window_start, "pre"), (r.detect_date, "post"))):
            day = date.fromisoformat(d)
            a, tr = _chip(_best_file(by_day, day, bb), bb, ["B04", "B03", "B02"])
            ax = axes[row, 2 * col + j]
            ax.imshow(_rgb(a, 2000), extent=_extent(tr, a.shape[1:]))
            gpd.GeoSeries([r.geometry]).boundary.plot(ax=ax, color="cyan", linewidth=1)
            ax.set_title(f"{r.field_id} {tag} {day:%d %b}" + (f"\n{r.area_ha:.2f} ha, harvest "
                         f"{r.harvest_date[5:]}" if tag == "pre" else f"\nVH {r.vh_change_db:+.1f}"
                         " dB" if pd.notna(r.vh_change_db) else "\nno S1 pair"), fontsize=8)
            ax.axis("on")
            ax.set_xticks([])
            ax.set_yticks([])
    fig.suptitle(f"{len(smp)} random M2.1 candidates (seed {seed}): last clear unburnt view vs "
                 "first clear burnt view. UNVERIFIED.", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out, smp.field_id.tolist()


def fig_albedo(cfg: Config, aoi_m: np.ndarray, out: Path) -> tuple[Path, dict[str, float]]:
    """Albedo at the harvest view (all harvested) vs at the first burnt view (burnt)."""
    src = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    lab, _ = _read(src, "label")
    ah, _ = _read(src, "albedo_harvest")
    ab, _ = _read(src, "albedo_burn")
    nh, _ = _read(src, "nbr_harvest")
    sc = lambda a, m: a[m & (a != NODATA)] / INDEX_SCALE  # noqa: E731
    harvested = aoi_m & (lab >= UNOBSERVED)
    nb = aoi_m & (lab == NOT_BURNT)
    bu = aoi_m & (lab == BURNT)
    rng = np.random.default_rng(0)
    sub = lambda v: rng.choice(v, size=min(len(v), 300_000), replace=False)  # noqa: E731
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    bins = np.linspace(0, 0.3, 61)
    axes[0].hist(sub(sc(ah, nb)), bins=bins, alpha=0.6, density=True,
                 label="harvest view, pixels labelled not_burnt")
    axes[0].hist(sub(sc(ah, bu)), bins=bins, alpha=0.6, density=True,
                 label="harvest view, pixels labelled burnt")
    axes[0].hist(sub(sc(ab, bu)), bins=bins, alpha=0.6, density=True,
                 label="first burnt view, pixels labelled burnt", histtype="step", linewidth=2,
                 color="black")
    axes[0].set_xlabel("albedo = mean(B02, B03, B04, B08)")
    axes[0].legend(fontsize=8)
    axes[0].set_title("Albedo at harvest view vs at detected burn", fontsize=10)
    axes[1].hist(sub(sc(nh, nb)), bins=np.linspace(-0.5, 0.6, 56), alpha=0.6, density=True,
                 label="not_burnt")
    axes[1].hist(sub(sc(nh, bu)), bins=np.linspace(-0.5, 0.6, 56), alpha=0.6, density=True,
                 label="burnt")
    axes[1].set_xlabel("NBR at harvest view")
    axes[1].legend(fontsize=8)
    axes[1].set_title("NBR at the harvest view", fontsize=10)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    ab_b = sc(ab, bu)
    ah_nb = sc(ah, nb)
    stats = {"albedo_burn_p50": round(float(np.median(ab_b)), 4),
             "albedo_harvest_notburnt_p50": round(float(np.median(ah_nb)), 4),
             "pct_notburnt_harvest_view_darker_than_burn_p50": round(
                 float((ah_nb < np.median(ab_b)).mean() * 100), 1),
             "harvested_px": int(harvested.sum())}
    return out, stats


# ---------------------------------------------------------------------------- tables

def pair_table(cfg: Config, aoi_m: np.ndarray, top: int = 15, factor: int = 10
               ) -> pd.DataFrame:
    """Burnt area per (last unburnt, first burnt) date pair vs the scene-wide change.

    Scene-wide medians are over harvested pixels (decimated by ``factor``) that are clear
    by SCL and below the blue haze threshold on both dates (cloud-proximity haze test not
    applied at this scale). A large scene-wide drop means the atmosphere or illumination,
    not only the field, changed between the two views.
    """
    from rasterio.enums import Resampling

    src = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    bu, _ = _read(cfg.path("outputs", "m21", "burnt_area.tif"), "burnt")
    s, _ = _read(src, "burn_prev_doy")
    e, _ = _read(src, "burn_doy")
    sel = (bu == 1) & aoi_m
    t = (pd.DataFrame({"s": s[sel], "e": e[sel]}).value_counts().rename("px").reset_index()
         .sort_values("px", ascending=False).head(top))
    del bu, s, e, sel
    lab, _ = _read(src, "label", factor)
    harvested = lab >= UNOBSERVED
    s2, b = cfg.sentinel2, cfg.burn_detection
    by_day = _s2_days(cfg)
    cache: dict[int, np.ndarray] = {}

    def day_stack(doy: int) -> np.ndarray:
        if doy in cache:
            return cache[doy]
        acc = None
        for p in by_day[date.fromordinal(int(doy) + DOY_BASE)]:
            import rasterio
            with rasterio.open(p) as ds:
                ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
                shp = lab.shape
                r = {n: ds.read(ix[n], out_shape=shp, resampling=Resampling.nearest)
                     .astype(np.float64) for n in ("B02", "B03", "B04", "B08", "B11", "B12",
                                                   "SCL")}
            ok = (~np.isin(r["SCL"], s2.scl_invalid) & (r["B02"] != NODATA)
                  & (r["B02"] <= cfg.quality.haze_blue_threshold * s2.reflectance_scale))
            with np.errstate(invalid="ignore", divide="ignore"):
                cur = np.stack([(r["B02"] + r["B03"] + r["B04"] + r["B08"]) / 4,
                                (r["B08"] - r["B12"]) / (r["B08"] + r["B12"]),
                                (r["B11"] - r["B12"]) / (r["B11"] + r["B12"])])
            cur = np.where(ok[None], cur, np.nan)
            acc = cur if acc is None else np.where(np.isnan(acc), cur, acc)
        cache[doy] = acc
        return acc

    rows = []
    for r in t.itertuples():
        a, c = day_stack(r.s), day_stack(r.e)
        ok = harvested & np.isfinite(a).all(0) & np.isfinite(c).all(0)
        ra = ((a[0] - c[0]) / a[0])[ok]
        dn = (a[1] - c[1])[ok]
        d2 = (a[2] - c[2])[ok]
        rows.append({
            "pair": f"{date.fromordinal(int(r.s) + DOY_BASE):%d %b} -> "
                    f"{date.fromordinal(int(r.e) + DOY_BASE):%d %b}",
            "burnt_ha": round(r.px / 100, 1),
            "scene_albedo_drop_rel": round(float(np.median(ra)), 3),
            "scene_dnbr": round(float(np.median(dn)), 3),
            "scene_dnbr2": round(float(np.median(d2)), 3),
            "scene_pct_pass_all3": round(float(((ra > b.albedo_rel_drop_min)
                                                & (dn > b.dnbr_short_min)
                                                & (d2 > b.dnbr2_min)).mean() * 100), 1),
        })
    return pd.DataFrame(rows)



def weekly(cfg: Config, cands: gpd.GeoDataFrame, aoi_m: np.ndarray) -> pd.DataFrame:
    src = cfg.path("processed", "m21", "s2_harvest_burn.tif")
    bu, _ = _read(cfg.path("outputs", "m21", "burnt_area.tif"), "burnt")
    lab, _ = _read(src, "label")
    hd, _ = _read(src, "harvest_doy")
    sel = (bu == 1) & aoi_m
    s, _ = _read(src, "burn_prev_doy")
    e, _ = _read(src, "burn_doy")
    pairs = pd.DataFrame({"s": s[sel], "e": e[sel]}).value_counts().rename("n").reset_index()
    harv = hd[aoi_m & (lab >= UNOBSERVED)]
    rows = []
    for w in _weeks(cfg):
        a, b = _doy(w.start), _doy(w.end)
        ln = pairs.e - pairs.s + 1
        ov = (np.minimum(pairs.e, b) - np.maximum(pairs.s, a) + 1).clip(lower=0)
        c = cands[cands.week_detected == w.id]
        rows.append({
            "week": w.id, "dates": w.label,
            "ha_harvested": round(float(((harv >= a) & (harv <= b)).sum() / 100), 1),
            "ha_first_detected": round(float(pairs.n[(pairs.e >= a) & (pairs.e <= b)].sum()
                                             / 100), 1),
            "ha_window_inside_week": round(float(pairs.n[(pairs.s >= a) & (pairs.e <= b)].sum()
                                                 / 100), 1),
            "ha_allocated": round(float((pairs.n * ov / ln).sum() / 100), 1),
            "n_polygons": len(c),
            "ha_single_observation": round(float(c.loc[c.single_observation, "area_ha"].sum()),
                                           1),
            "median_window_days": float(c.window_days.median()) if len(c) else None,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------- main

def make_report(cfg: Config) -> list[Path]:
    """Write all M2.1 figures, tables and ``reports/m21/REPORT.md``."""
    from stubble.aoi import aoi_mask, grid_spec
    from stubble.detect.harvest import sensitivity

    out_dir = cfg.path("reports", "m21")
    out_dir.mkdir(parents=True, exist_ok=True)
    aoi = gpd.read_file(cfg.path("processed", "aoi", "aoi.geojson"))
    spec = grid_spec(cfg, aoi)
    m = aoi_mask(cfg, aoi, spec)
    aoi_u = aoi.to_crs(cfg.aoi.crs)
    px_ha = spec.resolution ** 2 / 1e4

    def load(name: str) -> tuple[gpd.GeoDataFrame, dict]:
        c = gpd.read_file(cfg.path("outputs", name, "candidates.gpkg")).to_crs(cfg.aoi.crs)
        st = json.loads(cfg.path("outputs", name, "candidates.gpkg.provenance.json")
                        .read_text(encoding="utf-8"))
        return c, st

    _, old_prov = load("m2")
    new, new_prov = load("m21")
    so, sn = old_prov["stats"], new_prov["stats"]

    lab, _ = _read(cfg.path("processed", "m21", "s2_harvest_burn.tif"), "label")
    lab_in = lab[m]
    counts = {LABELS[k]: int((lab_in == k).sum()) for k in (NO_HARVEST, UNOBSERVED, NOT_BURNT,
                                                            BURNT)}
    harvested_ha = (counts["unobserved"] + counts["not_burnt"] + counts["burnt"]) * px_ha
    del lab, lab_in

    written = [fig_labels_harvest(cfg, aoi_u, out_dir / "labels_and_harvest_dates.png")]
    p, w4tab = fig_w04_windows(cfg, aoi_u, m, out_dir / "w04_burn_windows.png")
    written.append(p)
    w4tab.to_csv(out_dir / "w04_burn_windows.csv", index=False)
    written.append(out_dir / "w04_burn_windows.csv")
    p, chip_ids = fig_random_chips(cfg, new, out_dir / "random_candidates_rgb.png")
    written.append(p)
    p, alb = fig_albedo(cfg, m, out_dir / "albedo_harvest_vs_burn.png")
    written.append(p)
    wk = weekly(cfg, new, m)
    wk.to_csv(out_dir / "weekly_summary.csv", index=False)
    written.append(out_dir / "weekly_summary.csv")
    sens = pd.DataFrame(sensitivity(cfg, spec, m))
    sens.to_csv(out_dir / "threshold_sensitivity.csv", index=False)
    written.append(out_dir / "threshold_sensitivity.csv")
    pairs = pair_table(cfg, m)
    pairs.to_csv(out_dir / "date_pair_scene_change.csv", index=False)
    written.append(out_dir / "date_pair_scene_change.csv")

    cmp = pd.DataFrame([
        {"method": f"M2 baseline (pre-season median dNBR > {cfg.detection.dnbr_threshold:.2f})",
         "raw_ha": so["raw_ha"], "final_ha": so["final_ha"],
         "pct_aoi": round(100 * so["final_ha"] / so["aoi_ha"], 1),
         "pct_harvested": round(100 * so["final_ha"] / harvested_ha, 1),
         "n_polygons": so["n_polygons"]},
        {"method": "M2.1 harvest-aware",
         "raw_ha": sn["raw_ha"], "final_ha": sn["final_ha"],
         "pct_aoi": round(100 * sn["final_ha"] / sn["aoi_ha"], 1),
         "pct_harvested": round(100 * sn["final_ha"] / harvested_ha, 1),
         "n_polygons": sn["n_polygons"]},
    ])
    cmp.to_csv(out_dir / "old_vs_new.csv", index=False)
    written.append(out_dir / "old_vs_new.csv")

    summary = {"old": so, "new": sn, "label_px": counts,
               "label_ha": {k: round(v * px_ha, 1) for k, v in counts.items()},
               "harvested_ha": round(harvested_ha, 1), "albedo": alb,
               "random_chip_ids": chip_ids,
               "new_polygon_area_ha_p50_p90_p99": new.area_ha.quantile([.5, .9, .99]).round(2)
               .tolist(), "new_largest_polygon_ha": float(new.area_ha.max()),
               "code_version": new_prov.get("code_version")}
    (out_dir / "m21_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    written.append(out_dir / "m21_summary.json")
    written.append(_write_md(cfg, out_dir, summary, cmp, wk, w4tab, sens, pairs))
    return written


def _write_md(cfg: Config, out_dir: Path, s: dict, cmp: pd.DataFrame, wk: pd.DataFrame,
              w4: pd.DataFrame, sens: pd.DataFrame, pairs: pd.DataFrame) -> Path:
    h, b = cfg.harvest_detection, cfg.burn_detection
    lh = s["label_ha"]
    n = s["new"]
    a = s["albedo"]
    w4_in = w4[w4.inside_w04].ha.sum()
    lines = [
        "# M2.1: Harvest-aware burn detection (Sangrur, 2025)",
        "",
        "> **Decision support only.** Every polygon is a model estimate with status "
        "`pending_field_verification` and an empty `burn_confidence` (calibration in M4). "
        "Nothing here is evidence of an offence.",
        "",
        f"Code version for all M1/M2/M2.1 outputs: `{s['code_version']}`.",
        "",
        "## Method (thresholds from `config.yaml`)",
        f"- **Harvest** per pixel: first usable clear view with NDVI < {h.ndvi_harvest} after "
        f">= {h.min_green_views} view(s) with NDVI > {h.ndvi_green}.",
        "- **Burn** tested only between *consecutive* usable views after the harvest view; all "
        f"three must hold: NBR drop > {b.dnbr_short_min}, relative albedo drop > "
        f"{b.albedo_rel_drop_min} (albedo = mean of B02, B03, B04, B08), NBR2 drop > "
        f"{b.dnbr2_min} (NBR2 = (B11-B12)/(B11+B12)).",
        "- **Burn-date window** = [last clear unburnt view, first clear burnt view].",
        f"- Hazy views are {'used' if b.use_hazy_views else 'skipped'} (haze flag from M2); "
        "skipped views widen windows but never trigger a burn.",
        "- **Labels**: `no_harvest`, `unobserved` (harvest seen, no usable view after it; never "
        "reported as not burnt), `not_burnt`, `burnt`.",
        f"- Cleaning as M2: opening r={b.opening_radius_px}, patches < {b.min_patch_ha} ha "
        "dropped, polygons split by first burnt-view date. Sentinel-1 change stored as "
        "supporting evidence only.",
        "- M2 baseline polygons are kept for comparison and now use IDs `SGR-B-nnnn`; M2.1 "
        "polygons use `SGR-nnnn`.",
        "",
        "## Old vs new",
        _md_table(cmp),
        "",
        f"Harvested area (any harvest label) = {s['harvested_ha']:,.0f} ha.",
        "",
        "## Pixel labels inside the AOI (raw, before cleaning)",
        "| label | pixels | ha |",
        "|---|---|---|",
        *[f"| {k} | {s['label_px'][k]:,} | {lh[k]:,.0f} |" for k in s["label_px"]],
        "",
        f"**Unobserved: {s['label_px']['unobserved']:,} pixels ({lh['unobserved']:,.0f} ha).**",
        "",
        "![labels](labels_and_harvest_dates.png)",
        "",
        "## Burn windows for w04",
        f"{w4.ha.sum():,.0f} ha of cleaned candidates have a window overlapping w04; "
        f"{w4_in:,.0f} ha have the whole window inside w04. Full table: "
        "`w04_burn_windows.csv`.",
        "",
        _md_table(w4.head(12)),
        "",
        "![w04](w04_burn_windows.png)",
        "",
        "## Weekly summary",
        _md_table(wk),
        "",
        "## 20 random candidates (pre = last clear unburnt view, post = first clear burnt view)",
        "![chips](random_candidates_rgb.png)",
        "",
        "## Threshold sensitivity (raw pixel shares, 6 sample row blocks)",
        "Burn thresholds scaled x0.5 / x2, NDVI thresholds shifted +-0.05. Without labels the "
        "thresholds cannot be tuned; this shows how much the answer depends on them.",
        "",
        _md_table(sens),
        "",
        "## Scene-wide change per date pair (atmosphere / illumination check)",
        "For the date pairs that produce the most burnt area: the scene-wide median change "
        "between the two views over harvested pixels (decimated x10, clear and below the blue "
        "haze threshold on both dates), and the share of those pixels passing all three tests. "
        "A large scene-wide albedo drop means the two images differ as a whole (haze, smoke, "
        "illumination), so part of the per-pixel drop is not the field. In peak burning weeks "
        "real burns also move the scene median, so this table cannot separate the two on "
        "its own.",
        "",
        _md_table(pairs),
        "",
        "## Diagnostics and caveats",
        "- Haze leak: the blue threshold "
        f"({cfg.quality.haze_blue_threshold}) only removes part of the smoky 4 Nov scene; "
        "several random chips use visibly hazy 4 Nov views as the unburnt reference. A hazy -> "
        "clear transition lowers albedo, NBR and NBR2 together.",
        f"- Median albedo at the first burnt view: {a['albedo_burn_p50']}; at the harvest view of "
        f"`not_burnt` pixels: {a['albedo_harvest_notburnt_p50']}. "
        f"{a['pct_notburnt_harvest_view_darker_than_burn_p50']}% of `not_burnt` pixels already "
        "had a harvest view darker than the median burnt view. Some of these may be burns that "
        "happened between the last green view and the harvest view, which this method cannot "
        "see (the harvest view is the first post-harvest reference).",
        "- Tillage that incorporates straw can lower NBR, albedo and NBR2 together, like a burn. "
        "The three-way test reduces but does not remove that confusion; M3 labels "
        "(`tilled_not_burnt`) are needed to measure it.",
        "- `no_harvest` includes settlements, roads, water, orchards and fields not green in the "
        "pre-season window.",
        f"- Polygon area p50 / p90 / p99: "
        f"{' / '.join(str(v) for v in s['new_polygon_area_ha_p50_p90_p99'])} ha; largest "
        f"{s['new_largest_polygon_ha']:,.0f} ha.",
        f"- {n['n_single_observation']:,} single-observation polygons "
        f"({n['single_observation_ha']:,.0f} ha).",
        "",
        "![albedo](albedo_harvest_vs_burn.png)",
        "",
        "## Outputs",
        "- `data/processed/m21/s2_harvest_burn.tif`: per-pixel label, harvest and burn dates, "
        "index drops (x10000)",
        "- `data/outputs/m21/burnt_area.tif`, `candidate_labels.tif`, `candidates.gpkg`",
        "- Every file has a `.provenance.json` sidecar.",
    ]
    p = out_dir / "REPORT.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
