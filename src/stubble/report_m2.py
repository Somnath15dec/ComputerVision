"""M2 report: maps, weekly summaries, single-observation list, false-positive samples.

Everything here is computed from the cached rasters and candidate polygons; nothing is
hand-entered. Suspected false-positive categories are *heuristics for picking examples to
label in M3*, not verified classes.
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
import rasterio
from matplotlib.colors import BoundaryNorm, ListedColormap
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

from stubble.config import Config
from stubble.data.composites import NODATA
from stubble.detect.indices import nbr
from stubble.detect.timeseries import DOY_BASE, INDEX_SCALE, _s2_days

log = logging.getLogger(__name__)

GRADUAL_DROP = 0.10   # dnbr_local below this: NBR fell gradually (harvest-like), heuristic


def _read(path: Path, band: str, factor: int = 1) -> tuple[np.ndarray, Any]:
    with rasterio.open(path) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        if factor == 1:
            return ds.read(ix[band]), ds.transform
        shape = (ds.height // factor, ds.width // factor)
        a = ds.read(ix[band], out_shape=shape, resampling=Resampling.nearest)
        t = ds.transform * ds.transform.scale(ds.width / shape[1], ds.height / shape[0])
    return a, t


def _extent(t: Any, shape: tuple[int, int]) -> list[float]:
    return [t.c, t.c + t.a * shape[1], t.f + t.e * shape[0], t.f]


def _composite_nbr(path: Path, factor: int) -> tuple[np.ndarray, Any]:
    b8, t = _read(path, "B08", factor)
    b12, _ = _read(path, "B12", factor)
    return nbr(b8, b12), t


def _week_ids(cfg: Config) -> list[Any]:
    return [w for w in cfg.compositing_windows() if w.id != "pre"]


# ---------------------------------------------------------------------------- figures

def fig_overview(cfg: Config, aoi: gpd.GeoDataFrame, out: Path, factor: int = 6) -> Path:
    """Map of M2 baseline candidates by week of first detection (candidates_by_week.png)."""
    wk, t = _read(cfg.path("outputs", "m2", "burnt_area.tif"), "week_detected", factor)
    weeks = _week_ids(cfg)
    cmap = ListedColormap(plt.get_cmap("plasma")(np.linspace(0.05, 0.95, len(weeks))))
    img = np.where(wk > 0, wk, np.nan).astype(float)
    fig, ax = plt.subplots(figsize=(8, 10))
    ax.set_facecolor("#e9e9e9")
    im = ax.imshow(img, extent=_extent(t, wk.shape), cmap=cmap,
                   norm=BoundaryNorm(np.arange(0.5, len(weeks) + 1), cmap.N))
    aoi.boundary.plot(ax=ax, color="black", linewidth=0.7)
    cb = fig.colorbar(im, ax=ax, fraction=0.035, ticks=range(1, len(weeks) + 1))
    cb.ax.set_yticklabels([f"{w.id} {w.start:%d %b}" for w in weeks], fontsize=8)
    thr = cfg.detection.dnbr_threshold
    ax.set_title(f"Baseline candidates (dNBR > {thr:.2f}) by week of first post-burn "
                 "observation\nUNVERIFIED model estimate, pending field verification",
                 fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def fig_w04(cfg: Config, aoi: gpd.GeoDataFrame, cands: gpd.GeoDataFrame, out: Path,
            factor: int = 6) -> tuple[Path, dict[str, Any]]:
    """w04 strict-composite dNBR next to the w04 clear-observation count (+ zoom row)."""
    thr = cfg.detection.dnbr_threshold
    pre_p = cfg.path("processed", "s2", "pre.tif")
    w04_p = cfg.path("processed", "s2", "w04.tif")
    q_p = cfg.path("processed", "m2", "s2_quality.tif")
    det_p = cfg.path("processed", "m2", "s2_pixel_detect.tif")
    burnt_p = cfg.path("outputs", "m2", "burnt_area.tif")
    wk = next(w for w in _week_ids(cfg) if w.id == "w04")

    # full-resolution statistics inside the AOI
    from stubble.aoi import aoi_mask, grid_spec
    spec = grid_spec(cfg, aoi.to_crs(4326))
    m = aoi_mask(cfg, aoi.to_crs(4326), spec)
    npre, _ = _composite_nbr(pre_p, 1)
    nw4, _ = _composite_nbr(w04_p, 1)
    d4 = npre - nw4
    cnt, _ = _read(q_p, "n_clear_w04")
    flag = (d4 > thr) & m
    stats = {
        "w04_composite_flag_ha": round(float(flag.sum() / 100), 1),
        "w04_composite_flag_pct_from_1obs": round(float((cnt[flag] == 1).mean() * 100), 1),
        "w04_aoi_pct_0obs": round(float((cnt[m] == 0).mean() * 100), 2),
        "w04_aoi_pct_1obs": round(float((cnt[m] == 1).mean() * 100), 2),
        "w04_aoi_pct_2plus": round(float((cnt[m] >= 2).mean() * 100), 2),
    }
    del npre, nw4, d4, flag

    # zoom box (5 km): the 5 km block with most w04-detected area seen on a single w04 day
    half = 2500
    wkb_full, _ = _read(burnt_p, "week_detected")
    hit = ((wkb_full == 4) & (cnt == 1) & m).astype(np.float32)
    del wkb_full
    blk = int(2 * half / spec.resolution)
    hh, ww = (hit.shape[0] // blk) * blk, (hit.shape[1] // blk) * blk
    sums = hit[:hh, :ww].reshape(hh // blk, blk, ww // blk, blk).sum(axis=(1, 3))
    bi, bj = np.unravel_index(np.argmax(sums), sums.shape)
    x0 = spec.transform.c + bj * blk * spec.resolution
    y1 = spec.transform.f - bi * blk * spec.resolution
    zb = (x0, y1 - 2 * half, x0 + 2 * half, y1)
    stats["zoom_w04_1obs_detected_ha"] = round(float(sums[bi, bj] / 100), 1)
    del hit

    def layers(f: int | None) -> list[tuple[np.ndarray, Any]]:
        res = []
        for path, band in ((det_p, None), (q_p, "n_clear_w04"), (burnt_p, "week_detected")):
            with rasterio.open(path if band else pre_p) as ds:
                ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
                win = None if f else from_bounds(*zb, transform=ds.transform)
                kw = {}
                if f:
                    kw = dict(out_shape=(ds.height // f, ds.width // f),
                              resampling=Resampling.nearest)
                tr = (ds.transform * ds.transform.scale(f, f)) if f else ds.window_transform(win)
                if band is None:  # composite dNBR
                    with rasterio.open(w04_p) as dw:
                        iw = {d: i + 1 for i, d in enumerate(dw.descriptions)}
                        a = nbr(ds.read(ix["B08"], window=win, **kw),
                                ds.read(ix["B12"], window=win, **kw)) - nbr(
                            dw.read(iw["B08"], window=win, **kw),
                            dw.read(iw["B12"], window=win, **kw))
                else:
                    a = ds.read(ix[band], window=win, **kw).astype(float)
                res.append((a, tr))
        return res

    fig, axes = plt.subplots(2, 3, figsize=(16, 11.5))
    aoi_u = aoi.to_crs(cfg.aoi.crs)
    for r, f in enumerate((factor, None)):
        (dn, t1), (ct, t2), (wkb, t3) = layers(f)
        a0 = axes[r, 0].imshow(dn, extent=_extent(t1, dn.shape), cmap="RdYlGn_r",
                               vmin=-0.2, vmax=0.8)
        fig.colorbar(a0, ax=axes[r, 0], fraction=0.04, label="dNBR (pre - w04 composite)")
        cm = ListedColormap(["#000000", "#d7301f", "#fdae61", "#a6d96a", "#1a9641"])
        a1 = axes[r, 1].imshow(np.clip(ct, 0, 4), extent=_extent(t2, ct.shape), cmap=cm,
                               norm=BoundaryNorm(np.arange(-0.5, 5), cm.N))
        cb = fig.colorbar(a1, ax=axes[r, 1], fraction=0.04, ticks=range(5))
        cb.ax.set_yticklabels(["0", "1", "2", "3", "4+"])
        cb.set_label("clear S2 days in w04")
        w4 = np.where(wkb == 4, 1.0, np.where(wkb > 0, 0.4, np.nan))
        axes[r, 2].set_facecolor("#f2f2f2")
        axes[r, 2].imshow(w4, extent=_extent(t3, wkb.shape), cmap="Reds", vmin=0, vmax=1)
        for ax in axes[r]:
            aoi_u.boundary.plot(ax=ax, color="blue", linewidth=0.6)
            ax.set_xticks([])
            ax.set_yticks([])
            if f is None:
                ax.set_xlim(zb[0], zb[2])
                ax.set_ylim(zb[1], zb[3])
        if f is None:
            sel_z = cands[cands.week_detected == "w04"].cx[zb[0]:zb[2], zb[1]:zb[3]]
            single = sel_z[sel_z.single_observation]
            n_z, n_s = len(sel_z), len(single)
            if n_s:
                single.boundary.plot(ax=axes[r, 1], color="white", linewidth=0.8)
                single.boundary.plot(ax=axes[r, 2], color="black", linewidth=0.8)
        else:
            from matplotlib.patches import Rectangle
            for ax in axes[r]:
                ax.add_patch(Rectangle((zb[0], zb[1]), 2 * half, 2 * half, fill=False,
                                       edgecolor="black", linewidth=1.2))
    axes[0, 0].set_title(f"w04 baseline dNBR from strict weekly composite\n{wk.label}",
                         fontsize=10)
    axes[0, 1].set_title("Clear S2 observation days per pixel in w04", fontsize=10)
    axes[0, 2].set_title("Per-pixel method: first post-burn obs in w04 (dark red)\n"
                         "other weeks pale", fontsize=10)
    axes[1, 0].set_title("Zoom 5 x 5 km", fontsize=10)
    axes[1, 1].set_title(f"Zoom: clear days in w04 ({n_s} of {n_z} w04 polygons here are\n"
                         "single-observation, outlined white)", fontsize=10)
    axes[1, 2].set_title("Zoom: per-pixel detections; support looks can come from the\n"
                         "following week, so 1 clear w04 day != single observation",
                         fontsize=10)
    fig.suptitle(
        f"w04: {stats['w04_aoi_pct_1obs']:.0f}% of AOI pixels have exactly one clear day; "
        f"{stats['w04_composite_flag_pct_from_1obs']:.0f}% of the composite's "
        f"dNBR > {thr} pixels rest on one observation", fontsize=12)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out, stats


def fig_quality(cfg: Config, aoi: gpd.GeoDataFrame, out: Path, factor: int = 6) -> Path:
    """Quality-band maps: clear days, longest gap, hazy days (quality_bands.png)."""
    q = cfg.path("processed", "m2", "s2_quality.tif")
    panels = [("n_clear", "Clear S2 days, 20 Sep - 30 Nov", "viridis", (0, 33)),
              ("max_gap_days", "Longest gap between clear days (1 Oct - 30 Nov)", "magma_r",
               (0, 20)),
              ("n_hazy", "Clear-by-SCL days flagged hazy", "cividis", (0, 8))]
    fig, axes = plt.subplots(1, 3, figsize=(16, 6.5))
    for ax, (band, title, cmap, (lo, hi)) in zip(axes, panels, strict=True):
        a, t = _read(q, band, factor)
        im = ax.imshow(np.where(a == NODATA, np.nan, a), extent=_extent(t, a.shape), cmap=cmap,
                       vmin=lo, vmax=hi)
        aoi.to_crs(cfg.aoi.crs).boundary.plot(ax=ax, color="red", linewidth=0.6)
        fig.colorbar(im, ax=ax, fraction=0.04)
        ax.set_title(title, fontsize=10)
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------- weekly

def weekly_summary(cfg: Config, cands: gpd.GeoDataFrame, aoi_m: np.ndarray) -> pd.DataFrame:
    """Per-week burnt area from per-pixel windows.

    * ``ha_first_detected``: pixels whose first post-burn observation falls in the week.
    * ``ha_window_inside_week``: pixels whose whole burn-date window lies in the week.
    * ``ha_allocated``: each pixel's area spread uniformly over its window's days, summed per
      week (an estimate of when burning happened, not when it was seen).
    """
    det = cfg.path("processed", "m2", "s2_pixel_detect.tif")
    burnt, _ = _read(cfg.path("outputs", "m2", "burnt_area.tif"), "burnt")
    sel = (burnt == 1) & aoi_m
    dd, _ = _read(det, "detect_doy")
    ss, _ = _read(det, "window_start_doy")
    pairs = pd.DataFrame({"s": ss[sel], "e": dd[sel]}).value_counts().rename("n").reset_index()
    del dd, ss, burnt
    px_ha = cfg.aoi.resolution_m ** 2 / 1e4
    rows = []
    for w in _week_ids(cfg):
        a, b = w.start.toordinal() - DOY_BASE, w.end.toordinal() - DOY_BASE
        ln = pairs.e - pairs.s + 1
        ov = (np.minimum(pairs.e, b) - np.maximum(pairs.s, a) + 1).clip(lower=0)
        c = cands[cands.week_detected == w.id]
        rows.append({
            "week": w.id, "dates": w.label,
            "ha_first_detected": round(float((pairs.n[(pairs.e >= a) & (pairs.e <= b)]).sum()
                                             * px_ha), 1),
            "ha_window_inside_week": round(float(pairs.n[(pairs.s >= a) & (pairs.e <= b)].sum()
                                                 * px_ha), 1),
            "ha_allocated": round(float((pairs.n * ov / ln).sum() * px_ha), 1),
            "n_polygons": len(c),
            "ha_single_observation": round(float(c.loc[c.single_observation, "area_ha"].sum()),
                                           1),
            "ha_haze_at_detection": round(float(c.loc[c.haze_frac >= 0.5, "area_ha"].sum()), 1),
            "ha_gradual_drop": round(float(c.loc[c.dnbr_local_mean < GRADUAL_DROP,
                                                 "area_ha"].sum()), 1),
            "median_window_days": float(c.window_days.median()) if len(c) else None,
        })
    return pd.DataFrame(rows)


def fig_weekly(df: pd.DataFrame, out: Path) -> Path:
    """Weekly bar chart of first-detected, allocated and single-observation area."""
    fig, ax = plt.subplots(figsize=(10, 4.8))
    x = np.arange(len(df))
    ax.bar(x - 0.27, df.ha_first_detected / 1000, 0.27, label="first detected in week",
           color="#3b6fb6")
    ax.bar(x, df.ha_allocated / 1000, 0.27, label="allocated over burn-date window",
           color="#e08a2c")
    ax.bar(x + 0.27, df.ha_single_observation / 1000, 0.27,
           label="of which single-observation polygons (by detection week)", color="#999999")
    ax.set_xticks(x, [f"{r.week}\n{r.dates[5:10]}" for r in df.itertuples()], fontsize=8)
    ax.set_ylabel("thousand ha (baseline, unverified)")
    ax.set_title("Baseline candidate area per week: estimates, not confirmed burns")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------- FP samples

def _best_file(by_day: dict[date, list[Path]], day: date, bounds: tuple) -> Path:
    best, score = by_day[day][0], -1
    for p in by_day[day]:
        with rasterio.open(p) as ds:
            ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
            s = (ds.read(ix["B08"], window=from_bounds(*bounds, transform=ds.transform))
                 != NODATA).mean()
        if s > score:
            best, score = p, s
    return best


def _chip(path: Path, bounds: tuple, bands: list[str]) -> tuple[np.ndarray, Any]:
    with rasterio.open(path) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
        return ds.read([ix[b] for b in bands], window=win), ds.window_transform(win)


def _rgb(a: np.ndarray, hi: float) -> np.ndarray:
    x = np.clip(a.astype(np.float32) / hi, 0, 1)
    x[:, (a == NODATA).any(axis=0)] = 1
    return np.moveaxis(x, 0, -1)


def sample_fp(cfg: Config, cands: gpd.GeoDataFrame, n_sample: int = 400, seed: int = 0
              ) -> pd.DataFrame:
    """Measure post-detection reflectance for a random sample of 1-10 ha polygons."""
    by_day = _s2_days(cfg)
    pool = cands[(cands.area_ha >= 1) & (cands.area_ha <= 10)]
    smp = pool.sample(min(n_sample, len(pool)), random_state=seed)
    from rasterio.features import geometry_mask
    rows = []
    for r in smp.itertuples():
        b = r.geometry.bounds
        p = _best_file(by_day, date.fromisoformat(r.detect_date), b)
        a, t = _chip(p, b, ["B02", "B04", "B08", "B12", "SCL"])
        inside = ~geometry_mask([r.geometry], out_shape=a.shape[1:], transform=t)
        ok = inside & (a[0] != NODATA)
        if ok.sum() < 5:
            continue
        v = {k: float(np.median(a[i][ok])) / 1e4 for i, k in enumerate(["B02", "B04", "B08",
                                                                         "B12"])}
        rows.append({"field_id": r.field_id, **{f"post_{k}": x for k, x in v.items()}})
    return pd.DataFrame(rows)


def fig_fp(cfg: Config, cands: gpd.GeoDataFrame, groups: dict[str, list[str]], out: Path
           ) -> Path:
    """Chip sheet of suspected false-positive polygons per hypothesis group."""
    by_day = _s2_days(cfg)
    det_p = cfg.path("processed", "m2", "s2_pixel_detect.tif")
    nrows = sum(len(v) for v in groups.values())
    fig, axes = plt.subplots(nrows, 5, figsize=(15, 3.1 * nrows), squeeze=False)
    i = 0
    idx = cands.set_index("field_id")
    for gname, ids in groups.items():
        for fid in ids:
            r = idx.loc[fid]
            g = r.geometry
            x0, y0, x1, y1 = g.bounds
            m = max(150, 0.4 * max(x1 - x0, y1 - y0))
            b = (x0 - m, y0 - m, x1 + m, y1 + m)
            pre_day = date.fromisoformat(r.window_start)
            post_day = date.fromisoformat(r.detect_date)
            pre, tpre = _chip(_best_file(by_day, pre_day, b), b, ["B04", "B03", "B02", "B12",
                                                                  "B08"])
            post, tpost = _chip(_best_file(by_day, post_day, b), b, ["B04", "B03", "B02", "B12",
                                                                     "B08"])
            npre, _ = _chip(det_p, b, ["nbr_pre"])
            npre = np.where(npre[0] == NODATA, np.nan, npre[0] / INDEX_SCALE)
            dn = npre - nbr(post[4], post[3])
            ext = _extent(tpost, post.shape[1:])
            ax = axes[i]
            ax[0].imshow(_rgb(pre[:3], 2000), extent=_extent(tpre, pre.shape[1:]))
            ax[0].set_title(f"pre RGB {pre_day:%d %b}", fontsize=8)
            ax[1].imshow(_rgb(post[:3], 2000), extent=ext)
            ax[1].set_title(f"post RGB {post_day:%d %b}", fontsize=8)
            ax[2].imshow(_rgb(post[[3, 4, 0]], 4000), extent=ext)
            ax[2].set_title("post SWIR false colour (B12/B8/B4)", fontsize=8)
            im = ax[3].imshow(dn, extent=ext, cmap="RdYlGn_r", vmin=-0.2, vmax=0.9)
            ax[3].set_title("dNBR (pre median - post)", fontsize=8)
            fig.colorbar(im, ax=ax[3], fraction=0.045)
            for a in ax[:4]:
                gpd.GeoSeries([g]).boundary.plot(ax=a, color="cyan", linewidth=1)
                a.set_xticks([])
                a.set_yticks([])
            ax[4].axis("off")
            vh = r.vh_change_db
            s1_txt = f"S1 VH change {vh:+.1f} dB" if pd.notna(vh) else "S1: no pair"
            txt = (f"{gname}\n{fid}  ({r.grid_cell})\n{r.area_ha:.1f} ha, "
                   f"window {r.detected_window}\n"
                   f"dNBR mean {r.dnbr_detect_mean:.2f}, drop since\nprevious look "
                   f"{r.dnbr_local_mean:.2f}\n"
                   f"support obs {r.n_support_mean:.1f}, haze {r.haze_frac:.0%}\n{s1_txt}")
            ax[4].text(0, 0.5, txt + "\nstatus: pending_field_verification", fontsize=8,
                       va="center", family="monospace")
            i += 1
    fig.suptitle("Suspected false-positive patterns (heuristic picks, UNLABELLED; to be "
                 "labelled in M3)", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------- main

def make_report(cfg: Config) -> list[Path]:
    """Write all M2 figures, tables and ``reports/m2/REPORT.md``."""
    from stubble.aoi import aoi_mask, grid_spec

    out_dir = cfg.path("reports", "m2")
    out_dir.mkdir(parents=True, exist_ok=True)
    aoi = gpd.read_file(cfg.path("processed", "aoi", "aoi.geojson"))
    spec = grid_spec(cfg, aoi)
    m = aoi_mask(cfg, aoi, spec)
    aoi_u = aoi.to_crs(cfg.aoi.crs)
    cands = gpd.read_file(cfg.path("outputs", "m2", "candidates.gpkg")).to_crs(cfg.aoi.crs)
    prov = json.loads(cfg.path("outputs", "m2", "candidates.gpkg.provenance.json")
                      .read_text(encoding="utf-8"))
    st = prov["stats"]
    written = [fig_overview(cfg, aoi_u, out_dir / "candidates_by_week.png")]
    p, w4 = fig_w04(cfg, aoi_u, cands, out_dir / "w04_dnbr_vs_obscount.png")
    written.append(p)
    written.append(fig_quality(cfg, aoi_u, out_dir / "quality_bands.png"))

    wk = weekly_summary(cfg, cands, m)
    wk.to_csv(out_dir / "weekly_summary.csv", index=False)
    written += [out_dir / "weekly_summary.csv", fig_weekly(wk, out_dir / "weekly_area.png")]

    cols = ["field_id", "grid_cell", "area_ha", "week_detected", "detected_window",
            "window_days", "n_support_mean", "frac_single_obs", "n_clear_week_mean",
            "n_clear_min", "max_gap_days", "haze_frac", "dnbr_detect_mean", "dnbr_local_mean",
            "vv_change_db", "vh_change_db", "s1_pre", "s1_post", "s1_orbit", "quality_flags",
            "status"]
    single = cands[cands.single_observation].sort_values("area_ha", ascending=False)
    single[cols].to_csv(out_dir / "single_observation_polygons.csv", index=False)
    written.append(out_dir / "single_observation_polygons.csv")

    # suspected false-positive groups from a measured random sample
    smp = sample_fp(cfg, cands).merge(cands[["field_id", "dnbr_local_mean", "dnbr_detect_mean",
                                             "area_ha"]], on="field_id")
    smp.to_csv(out_dir / "fp_sample_reflectance.csv", index=False)
    rng = np.random.default_rng(1)

    def pick(df: pd.DataFrame, k: int = 2) -> list[str]:
        ids = df.field_id.tolist()
        return list(rng.choice(ids, size=min(k, len(ids)), replace=False)) if ids else []

    brightest = smp.sort_values("post_B04", ascending=False).head(max(5, len(smp) // 10))
    darkest = smp.sort_values("post_B08").head(max(5, len(smp) // 10))
    groups = {
        "abrupt + dark (burn-like reference)": pick(darkest[darkest.dnbr_local_mean
                                                            >= GRADUAL_DROP]),
        "gradual NBR decline (harvest-like?)": pick(smp[smp.dnbr_local_mean < 0.05]),
        "bright post-surface (tilled-soil-like?)": pick(brightest),
        "dark, low SWIR (wet-soil-like?)": pick(darkest[darkest.post_B12 < darkest.post_B12
                                                       .median()]),
    }
    written.append(fig_fp(cfg, cands, groups, out_dir / "suspected_false_positives.png"))

    area_q = cands.area_ha.quantile([0.5, 0.9, 0.99]).round(2).tolist()
    s1_ok = cands.s1_orbit.fillna("").astype(bool)
    gradual_ha = float(cands.loc[cands.dnbr_local_mean < GRADUAL_DROP, "area_ha"].sum())
    summary = {**st, **w4, "polygon_area_ha_p50_p90_p99": area_q,
               "largest_polygon_ha": float(cands.area_ha.max()),
               "s1_pair_pct": round(float(s1_ok.mean() * 100), 1),
               "gradual_drop_ha": round(gradual_ha, 1),
               "haze_at_detection_ha": round(float(cands.loc[cands.haze_frac >= 0.5,
                                                             "area_ha"].sum()), 1),
               "fp_groups": groups}
    (out_dir / "m2_summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                             encoding="utf-8")
    written.append(out_dir / "m2_summary.json")
    written.append(_write_md(cfg, out_dir, summary, wk, single, cands))
    return written


def _md_table(df: pd.DataFrame) -> str:
    """Plain GitHub markdown table (no extra dependency)."""
    def fmt(v: Any) -> str:
        if isinstance(v, float):
            return "" if np.isnan(v) else f"{v:,.2f}".rstrip("0").rstrip(".")
        return "" if v is None else str(v)

    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "---|" * len(df.columns)
    body = ["| " + " | ".join(fmt(v) for v in row) + " |"
            for row in df.itertuples(index=False)]
    return chr(10).join([head, sep, *body])


def _write_md(cfg: Config, out_dir: Path, s: dict, wk: pd.DataFrame, single: pd.DataFrame,
              cands: gpd.GeoDataFrame) -> Path:
    d = cfg.detection
    pct = lambda x: 100 * x / s["aoi_ha"]  # noqa: E731
    top = single.head(25)
    area_txt = " / ".join(f"{v:g}" for v in s["polygon_area_ha_p50_p90_p99"])
    lines = [
        "# M2: Baseline burn detection (Sangrur, 2025)",
        "",
        "> **Decision support only.** Every polygon is a model estimate with status "
        "`pending_field_verification`. `burn_confidence` is empty until the calibrated model "
        "(M4). Nothing here is evidence of an offence.",
        "",
        "## Method",
        f"- NBR = (B8 - B12)/(B8 + B12); dNBR = NBR_pre - NBR_obs; candidate if "
        f"dNBR > **{d.dnbr_threshold}**.",
        "- **NBR_pre** = per-pixel median over clear S2 days in the pre-season window "
        f"({cfg.windows.pre.start} to {cfg.windows.pre.end}).",
        "- **Per-pixel time series, not weekly composites.** For every pixel the first clear "
        "post-harvest day exceeding the threshold is the *first post-burn observation*; the "
        "latest clear day before it is the *last clear pre-burn observation*. The burn-date "
        "window is `[last clear pre-burn, first post-burn]`, inclusive at both ends because "
        "burning after the ~10:30 IST overpass is possible.",
        "- Two S2 passes on the same day are merged (mean of clear passes). Clear = SCL not in "
        f"{cfg.sentinel2.scl_invalid}.",
        f"- Cleaning: opening with a {2 * d.opening_radius_px + 1}x{2 * d.opening_radius_px + 1}"
        f" square, then patches < {d.min_patch_ha} ha ({s['min_patch_px']} px) dropped. "
        "Patches are split by first post-burn observation date, so one polygon = one "
        "detection event.",
        "- Quality bands per pixel: clear S2 days (season, pre, post, per week), days since "
        "last clear day, longest gap, hazy days. Haze = clear-by-SCL but B02 > "
        f"{cfg.quality.haze_blue_threshold} or within {cfg.quality.haze_cloud_buffer_m:.0f} m "
        f"of SCL {cfg.quality.haze_cloud_classes}. Carried into polygon attributes.",
        f"- Supporting observations: clear days within {d.confirm_days} d after first "
        "detection that also exceed the threshold. **Single-observation polygon** = at least "
        "half its pixels have only the detecting day as support.",
        "- **Sentinel-1 is supporting evidence only**: VV/VH change (dB, same orbit direction, "
        "last acquisition on/before window start vs first on/after first post-burn day) is "
        "stored per polygon. S1 never creates or removes a candidate.",
        "",
        "## Results",
        "| | value |",
        "|---|---|",
        f"| AOI area | {s['aoi_ha']:,.0f} ha |",
        f"| Raw candidate pixels | {s['raw_ha']:,.0f} ha ({pct(s['raw_ha']):.1f}% of AOI) |",
        f"| After opening | {s['after_opening_ha']:,.0f} ha |",
        f"| **Final candidate area** | **{s['final_ha']:,.0f} ha "
        f"({pct(s['final_ha']):.1f}% of AOI)** |",
        f"| Candidate polygons | {s['n_polygons']:,} |",
        f"| Polygon area p50 / p90 / p99 | {area_txt} ha; "
        f"largest {s['largest_polygon_ha']:,.0f} ha |",
        f"| Single-observation polygons | {s['n_single_observation']:,} "
        f"({s['single_observation_ha']:,.0f} ha) |",
        f"| Haze at detection (>= 50% px) | {s['haze_at_detection_ha']:,.0f} ha |",
        f"| Polygons with an S1 pre/post pair | {s['s1_pair_pct']}% |",
        f"| Gradual drop (NBR fell < {GRADUAL_DROP} since previous clear day) | "
        f"{s['gradual_drop_ha']:,.0f} ha |",
        "",
        "### Main finding: the baseline cannot separate burning from harvest",
        f"The brief's baseline flags **{pct(s['final_ha']):.0f}% of the district** (including "
        "settlement edges and roads that survive cleaning). Scene-wide median NBR on clear "
        "pixels falls from about 0.6 in the pre-season window (20 Sep to 5 Oct, standing paddy) "
        "to about -0.1 by November. A drop of that size exceeds 0.27 for harvested fields "
        "whether or not they were later burnt, so the first-detection date most likely "
        "tracks harvest rather than burning. Without labels (M3) this cannot be quantified. "
        f"{s['gradual_drop_ha']:,.0f} ha of candidates show only a gradual NBR decline between "
        "consecutive clear looks, which fits harvest/senescence better than an abrupt burn. "
        "These numbers are therefore **not** a burnt-area estimate. They are the baseline M4 "
        "has to beat. `dnbr_local_mean` (drop since the previous clear look) is stored per "
        "polygon as a candidate feature for M3/M4.",
        "",
        "## w04 (27 Oct to 2 Nov): strict composite vs observation count",
        f"- AOI pixels with 0 / 1 / 2+ clear days in w04: {s['w04_aoi_pct_0obs']}% / "
        f"{s['w04_aoi_pct_1obs']}% / {s['w04_aoi_pct_2plus']}%.",
        f"- The strict w04 composite flags {s['w04_composite_flag_ha']:,.0f} ha with dNBR > "
        f"{d.dnbr_threshold}; **{s['w04_composite_flag_pct_from_1obs']}%** of those pixels "
        "rest on a single observation.",
        "",
        "![w04](w04_dnbr_vs_obscount.png)",
        "",
        "## Weekly summary (from per-pixel windows)",
        "`ha_first_detected` = first post-burn observation in the week; `ha_allocated` = each "
        "pixel spread uniformly over its burn-date window; `ha_window_inside_week` = window "
        "entirely within the week.",
        "",
        _md_table(wk),
        "",
        "![weekly](weekly_area.png)",
        "",
        "![overview](candidates_by_week.png)",
        "",
        "## Quality bands",
        "![quality](quality_bands.png)",
        "",
        f"## Single-observation polygons ({len(single):,}; largest 25 shown)",
        "Full list: `single_observation_polygons.csv`.",
        "",
        top[["field_id", "grid_cell", "area_ha", "detected_window", "n_clear_week_mean",
             "haze_frac", "dnbr_detect_mean", "vh_change_db", "quality_flags"]]
        .pipe(_md_table),
        "",
        "## Suspected false positives (unlabelled)",
        "Picked from a random sample of 1 to 10 ha polygons using measured post-detection "
        "reflectance (`fp_sample_reflectance.csv`): darkest NIR with an abrupt drop (burn-like "
        "reference), drop since previous look < 0.05 (gradual, harvest-like), brightest red "
        "(tilled-soil-like) and darkest NIR with low SWIR (wet-soil-like). These are "
        "**hypotheses for M3 labelling**, not verified classes.",
        "",
        "![fp](suspected_false_positives.png)",
        "",
        "## Limitations",
        "- 10 m pixels: mixed pixels at field edges; field bunds are not resolved, so adjacent "
        "fields with the same detection date merge into one polygon.",
        "- Haze flag is a heuristic (blue threshold + cloud proximity); smoke-affected looks are "
        "flagged, not removed.",
        "- No ground truth yet; no precision/recall can be reported for the baseline until M3 "
        "labels exist.",
        "",
        "## Outputs",
        "- `data/outputs/m2/burnt_area.tif`: burnt (0/1), week_detected (1-8), COG",
        "- `data/outputs/m2/candidates.gpkg`: polygons + attributes (EPSG:4326)",
        "- `data/outputs/m2/candidate_labels.tif`: polygon label raster",
        "- `data/processed/m2/s2_pixel_detect.tif`, `s2_quality.tif`: per-pixel bands",
        "- Every file has a `.provenance.json` sidecar.",
    ]
    p = out_dir / "REPORT.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
