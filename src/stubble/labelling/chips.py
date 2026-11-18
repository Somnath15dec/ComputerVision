"""Evidence chips for one labelling item (read on demand from the cached rasters).

Panels: true-colour RGB before / after, false colour (B12, B08, B04) after, dNBR between the
two views, Sentinel-1 VH change, and the polygon's median NDVI / NBR / albedo time series
over every S2 view (unusable views drawn hollow). Display stretch is fixed for every date
(``labelling.rgb_max_reflectance``) so brightness is comparable. Reflectances shown are the
raw L2A values (no PIF offsets), exactly as delivered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from rasterio.features import geometry_mask
from shapely.geometry.base import BaseGeometry

from stubble.config import Config
from stubble.data.composites import NODATA, scl_valid
from stubble.detect.polygons import _s1_acqs
from stubble.detect.timeseries import _s2_days
from stubble.report_m2 import _best_file, _chip, _rgb


@dataclass
class ChipWindow:
    bounds: tuple[float, float, float, float]
    geom: BaseGeometry


def chip_window(cfg: Config, geom: BaseGeometry) -> ChipWindow:
    """Square window around ``geom`` (grid CRS) with ``chip_pad_m`` context."""
    x0, y0, x1, y1 = geom.bounds
    pad = cfg.labelling.chip_pad_m
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0, y1 - y0) / 2 + pad
    r = cfg.aoi.resolution_m
    snap = lambda v: round(v / r) * r  # noqa: E731
    return ChipWindow((snap(cx - half), snap(cy - half), snap(cx + half), snap(cy + half)),
                      geom)


def snap_day(days: list[date], d: date, side: str) -> date:
    """Latest day <= d (``side='before'``) or earliest day >= d (``'after'``)."""
    if side == "before":
        c = [x for x in days if x <= d]
        return c[-1] if c else days[0]
    c = [x for x in days if x >= d]
    return c[0] if c else days[-1]


def _outline(ax, geom: BaseGeometry, t) -> None:
    inv = ~t
    polys = getattr(geom, "geoms", [geom])
    for p in polys:
        xs, ys = zip(*[inv * xy for xy in p.exterior.coords], strict=True)
        ax.plot(xs, ys, color="cyan", lw=1.2)


def _nbr(a: np.ndarray, i8: int, i12: int) -> np.ndarray:
    b8, b12 = a[i8].astype(np.float32), a[i12].astype(np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (b8 - b12) / (b8 + b12)
    out[(a[i8] == NODATA) | (a[i12] == NODATA)] = np.nan
    return out


def time_series(cfg: Config, cw: ChipWindow) -> list[dict]:
    """Polygon-median NDVI, NBR and albedo per S2 view (clear = SCL-valid share >= 0.5)."""
    rows = []
    for day, paths in _s2_days(cfg).items():
        for p in paths:
            a, t = _chip(p, cw.bounds, ["B02", "B03", "B04", "B08", "B12", "SCL"])
            inside = ~geometry_mask([cw.geom], out_shape=a.shape[1:], transform=t)
            has = inside & (a[0] != NODATA)
            if has.sum() < 3:
                continue
            ok = has & scl_valid(a[5], cfg.sentinel2.scl_invalid)
            f = a[:5].astype(np.float32) / cfg.sentinel2.reflectance_scale
            use = ok if ok.sum() >= 3 else has
            with np.errstate(invalid="ignore", divide="ignore"):
                ndvi = (f[3] - f[2]) / (f[3] + f[2])
                nbr = (f[3] - f[4]) / (f[3] + f[4])
            rows.append({"day": day, "file": p.name, "clear_frac": float(ok.sum() / has.sum()),
                         "ndvi": float(np.nanmedian(ndvi[use])),
                         "nbr": float(np.nanmedian(nbr[use])),
                         "albedo": float(np.nanmedian(f[:4].mean(axis=0)[use]))})
    return rows


def _s1_pair(cfg: Config, before: date, after: date, orbit: str | None = None
             ) -> tuple[Path, Path, date, date] | None:
    acqs = _s1_acqs(cfg)
    orbits = [orbit] if orbit else sorted({o for _, o, _ in acqs})
    best = None
    for o in orbits:
        pre = [(d, p) for d, oo, p in acqs if oo == o and d <= before]
        post = [(d, p) for d, oo, p in acqs if oo == o and d >= after]
        if pre and post:
            gap = (post[0][0] - pre[-1][0]).days
            if best is None or gap < best[0]:
                best = (gap, pre[-1][1], post[0][1], pre[-1][0], post[0][0])
    return None if best is None else best[1:]


def render(cfg: Config, geom: BaseGeometry, before: date, after: date,
           s1_orbit: str | None = None, ts: list[dict] | None = None):
    """Matplotlib figure with all evidence panels for one item."""
    cw = chip_window(cfg, geom)
    by_day = _s2_days(cfg)
    days = list(by_day)
    b_day, a_day = snap_day(days, before, "before"), snap_day(days, after, "after")
    hi = cfg.labelling.rgb_max_reflectance * cfg.sentinel2.reflectance_scale
    bands = ["B04", "B03", "B02", "B08", "B12", "SCL"]
    pb, pa = _best_file(by_day, b_day, cw.bounds), _best_file(by_day, a_day, cw.bounds)
    ab, t = _chip(pb, cw.bounds, bands)
    aa, _ = _chip(pa, cw.bounds, bands)

    fig = plt.figure(figsize=(13, 8.2))
    gs = fig.add_gridspec(2, 3, height_ratios=[1, 1])
    ax = [fig.add_subplot(gs[0, i]) for i in range(3)] + [fig.add_subplot(gs[1, i])
                                                          for i in range(2)]
    ax_ts = fig.add_subplot(gs[1, 2])
    ax[0].imshow(_rgb(ab[:3], hi))
    ax[0].set_title(f"before {b_day:%d %b} (RGB)", fontsize=9)
    ax[1].imshow(_rgb(aa[:3], hi))
    ax[1].set_title(f"after {a_day:%d %b} (RGB)", fontsize=9)
    ax[2].imshow(_rgb(aa[[4, 3, 0]], hi * 1.6))
    ax[2].set_title(f"after {a_day:%d %b} (B12/B08/B04)", fontsize=9)
    dn = _nbr(ab, 3, 4) - _nbr(aa, 3, 4)
    im = ax[3].imshow(dn, cmap="RdYlGn_r", vmin=-0.5, vmax=0.5)
    fig.colorbar(im, ax=ax[3], fraction=0.046)
    ax[3].set_title("dNBR before - after", fontsize=9)

    pair = _s1_pair(cfg, b_day, a_day, s1_orbit)
    if pair:
        p0, p1, d0, d1 = pair
        v0, _ = _chip(p0, cw.bounds, ["VH_db"])
        v1, _ = _chip(p1, cw.bounds, ["VH_db"])
        sc = cfg.sentinel1.db_scale
        ch = np.where((v0[0] == NODATA) | (v1[0] == NODATA), np.nan,
                      (v1[0].astype(np.float32) - v0[0]) / sc)
        im = ax[4].imshow(ch, cmap="PuOr", vmin=-6, vmax=6)
        fig.colorbar(im, ax=ax[4], fraction=0.046)
        ax[4].set_title(f"S1 VH change dB {d0:%d %b} -> {d1:%d %b} ({p0.stem.split('_')[-1]})",
                        fontsize=9)
    else:
        ax[4].text(0.5, 0.5, "no S1 pair", ha="center", va="center")
    for a in ax:
        _outline(a, geom, t)
        a.set_xticks([])
        a.set_yticks([])

    ts = ts if ts is not None else time_series(cfg, cw)
    if ts:
        x = [r["day"] for r in ts]
        for key, col in (("ndvi", "tab:green"), ("nbr", "tab:red"), ("albedo", "tab:blue")):
            y = np.array([r[key] for r in ts])
            clear = np.array([r["clear_frac"] >= 0.5 for r in ts])
            ax_ts.plot(x, y, color=col, lw=0.8, alpha=0.6)
            ax_ts.scatter(np.array(x)[clear], y[clear], color=col, s=14, label=key)
            ax_ts.scatter(np.array(x)[~clear], y[~clear], facecolors="none", edgecolors=col,
                          s=14)
        for d in (b_day, a_day):
            ax_ts.axvline(d, color="k", lw=0.7, ls="--")
        ax_ts.legend(fontsize=7, loc="upper right")
        ax_ts.tick_params(labelsize=7)
        ax_ts.set_title("polygon median (hollow = mostly not clear)", fontsize=9)
        fig.autofmt_xdate()
    fig.tight_layout()
    return fig
