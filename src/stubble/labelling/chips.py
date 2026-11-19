"""Time strip for one labelling item (read on demand from the cached raw S2 views).

Blind display: every item, flagged polygon or unflagged 100 m cell, is drawn the same way,
and the geometry's shape never appears. The *anchor* is the 10 m grid pixel that contains
``geometry.representative_point()`` (always inside the geometry). Each tile is a
``chip_window_px`` x ``chip_window_px`` window (odd, so the anchor pixel is exactly in the
centre), upscaled ``tile_upscale`` x with nearest neighbour, with a small crosshair at the
anchor pixel centre and a neutral ``crosshair_box_m`` square around it. The label means "the
field under the crosshair"; it is stored against the sample item, whose original geometry is
used later (M4).

A date is *usable* when it is not scene-excluded (``radiometry.json`` /
``haze_mask.exclude_days``), the neutral box is at least ``strip_min_coverage`` covered by data
and at least ``strip_min_clear_frac`` of it is SCL-valid and not haze-masked (the detector's
blue, cloud-buffer and AOT tests). The test uses the box, not the geometry, so the choice of
dates does not depend on the item's shape either. When a date has several files, the
clearest one is shown.

The strip is laid out in rows of ``strip_cols`` dates. Each row has, top to bottom:

* a line chart at the crosshair (mean of the clear pixels in a ``series_window_px`` square):
  NBR ``(B08-B12)/(B08+B12)``, visible+NIR albedo (mean of B02, B03, B04, B08, as in the M2.1
  detector; drawn x4 to share the axis) and NDVI. Each date's marker sits exactly above its
  tiles; the previous row's last date enters from the left edge so a drop across a row break
  stays visible. The y-axis is the same on every row and every item;
* true colour (B04, B03, B02);
* SWIR false colour (B12, B8A, B04);
* ΔNBR to the previous usable date, ``NBR_prev - NBR_cur`` (positive = NBR drop, burn-like),
  on a fixed diverging scale ``+-dnbr_display_max`` for every item. The first date has no
  previous view. Pixels that are cloudy / hazy in either view are grey.

Contrast: one fixed stretch per item, computed from the clear pixels of all its dates
(``stretch_percentiles``; one range shared by the three
true-colour bands, per band for SWIR), so brightness is comparable along the strip.
Reflectances are the raw L2A values (no PIF offsets). Nothing from the detector is drawn.

Rendering uses the object-oriented Matplotlib API (no pyplot), so strips can be prefetched on
a background thread. Finished strips are cached on disk under ``data/cache/m3_strips``.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import matplotlib
import numpy as np
import rasterio
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry

from stubble.config import Config
from stubble.data.ancillary import aot_path
from stubble.data.composites import NODATA, scl_valid
from stubble.detect.harvest import albedo
from stubble.detect.indices import nbr
from stubble.detect.radiometry import haze_components, radiometry_path
from stubble.detect.timeseries import _s2_days

STRIP_VERSION = 4
BANDS = ["B02", "B03", "B04", "B08", "B8A", "B12", "SCL"]
REFL = ["B02", "B03", "B04", "B08", "B8A", "B12"]


@dataclass
class ChipWindow:
    bounds: tuple[float, float, float, float]
    anchor: Point        # centre of the anchor pixel (grid CRS)
    box: BaseGeometry    # neutral crosshair_box_m square around the anchor


@dataclass
class View:
    day: date
    file: str
    clear_frac: float
    bands: dict[str, np.ndarray]  # (h, w) int16 per band in ``REFL``
    clear: np.ndarray             # (h, w) bool: SCL-valid, not hazy, all bands present
    transform: object

    @property
    def tc(self) -> np.ndarray:
        """(3, h, w) int16: B04, B03, B02."""
        return np.stack([self.bands[b] for b in ("B04", "B03", "B02")])

    @property
    def swir(self) -> np.ndarray:
        """(3, h, w) int16: B12, B8A, B04."""
        return np.stack([self.bands[b] for b in ("B12", "B8A", "B04")])

    @property
    def nbr(self) -> np.ndarray:
        """NBR per pixel, NaN where not clear."""
        out = nbr(self.bands["B08"], self.bands["B12"])
        out[~self.clear] = np.nan
        return out

    @property
    def ndvi(self) -> np.ndarray:
        """NDVI per pixel, NaN where not clear."""
        out = nbr(self.bands["B08"], self.bands["B04"])
        out[~self.clear] = np.nan
        return out

    def albedo(self, scale: int) -> np.ndarray:
        """Visible+NIR albedo per pixel (detector definition), NaN where not clear."""
        out = albedo(*(self.bands[b] for b in ("B02", "B03", "B04", "B08")), scale=scale)
        out[~self.clear] = np.nan
        return out


def anchor_point(cfg: Config, geom: BaseGeometry) -> Point:
    """Centre of the grid pixel containing ``geom.representative_point()`` (grid CRS).

    Assumes the grid origin is a multiple of the resolution (true for the analysis grid).
    """
    r = cfg.aoi.resolution_m
    p = geom.representative_point()
    return Point((math.floor(p.x / r) + 0.5) * r, (math.floor(p.y / r) + 0.5) * r)


def chip_window(cfg: Config, geom: BaseGeometry) -> ChipWindow:
    """Fixed-size window centred on the anchor pixel; identical layout for every item."""
    lc, r = cfg.labelling, cfg.aoi.resolution_m
    a = anchor_point(cfg, geom)
    half = lc.chip_window_px / 2 * r
    hb = lc.crosshair_box_m / 2
    return ChipWindow((a.x - half, a.y - half, a.x + half, a.y + half), a,
                      box(a.x - hb, a.y - hb, a.x + hb, a.y + hb))


@lru_cache(maxsize=4)
def _excluded(radiometry_json: str, manual: tuple[date, ...]) -> frozenset:
    out = set(manual)
    p = Path(radiometry_json) if radiometry_json else None
    if p is not None and p.exists():
        out |= {date.fromisoformat(d) for d in json.loads(p.read_text("utf-8"))["excluded_days"]}
    return frozenset(out)


def excluded_days(cfg: Config) -> frozenset:
    """Scene-excluded dates, exactly as the M2.1 detector uses them."""
    rj = str(radiometry_path(cfg)) if cfg.radiometry is not None else ""
    return _excluded(rj, tuple(cfg.haze_mask.exclude_days))


def _read_halo(path: Path, bounds: tuple, halo_px: int, aot: Path | None
               ) -> tuple[dict[str, np.ndarray], object]:
    """Read ``BANDS`` (+AOT) for ``bounds`` plus ``halo_px`` on every side (boundless)."""
    with rasterio.open(path) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
        big = rasterio.windows.Window(win.col_off - halo_px, win.row_off - halo_px,
                                      win.width + 2 * halo_px, win.height + 2 * halo_px)
        arr = ds.read([ix[b] for b in BANDS], window=big, boundless=True, fill_value=NODATA)
        t = ds.window_transform(win)
    out = dict(zip(BANDS, arr, strict=True))
    if aot is not None and aot.exists():
        with rasterio.open(aot) as ds:
            w2 = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
            big2 = rasterio.windows.Window(w2.col_off - halo_px, w2.row_off - halo_px,
                                           w2.width + 2 * halo_px, w2.height + 2 * halo_px)
            out["AOT"] = ds.read(1, window=big2, boundless=True, fill_value=NODATA)
    return out, t


def polygon_mask(geom: BaseGeometry, shape: tuple[int, int], t) -> np.ndarray:
    inside = ~geometry_mask([geom], out_shape=shape, transform=t)
    if inside.sum() < 3:  # very small polygon: count every touched pixel
        inside = ~geometry_mask([geom], out_shape=shape, transform=t, all_touched=True)
    return inside


def pixel_clear(cfg: Config, a: dict[str, np.ndarray], halo_px: int) -> np.ndarray:
    """Per-pixel clear mask of the core window: SCL-valid, not hazy, all bands present."""
    hc = haze_components(a["B02"], a["SCL"], a.get("AOT"), cfg, halo_px)
    hz = hc["blue"] | hc["buffer"] | hc["aot"]
    core = (slice(halo_px, a["B02"].shape[0] - halo_px),
            slice(halo_px, a["B02"].shape[1] - halo_px))
    ok = scl_valid(a["SCL"][core], cfg.sentinel2.scl_invalid) & ~hz[core]
    for b in REFL:
        ok &= a[b][core] != NODATA
    return ok


def view_clear_frac(cfg: Config, a: dict[str, np.ndarray], inside: np.ndarray,
                    halo_px: int) -> tuple[float, float]:
    """(coverage, clear share) of the polygon in one view; ``a`` includes the halo."""
    hc = haze_components(a["B02"], a["SCL"], a.get("AOT"), cfg, halo_px)
    hz = hc["blue"] | hc["buffer"] | hc["aot"]
    core = (slice(halo_px, a["B02"].shape[0] - halo_px),
            slice(halo_px, a["B02"].shape[1] - halo_px))
    has = inside & (a["B04"][core] != NODATA) & (a["B12"][core] != NODATA)
    n_in = max(int(inside.sum()), 1)
    clear = has & scl_valid(a["SCL"][core], cfg.sentinel2.scl_invalid) & ~hz[core]
    return has.sum() / n_in, clear.sum() / n_in


def usable_views(cfg: Config, geom: BaseGeometry) -> list[View]:
    """Clearest usable view per date for the item's neutral box, in date order."""
    lc = cfg.labelling
    cw = chip_window(cfg, geom)
    halo = round(cfg.quality.haze_cloud_buffer_m / cfg.aoi.resolution_m)
    excl = excluded_days(cfg)
    out: list[View] = []
    for day, paths in _s2_days(cfg).items():
        if day in excl:
            continue
        best: View | None = None
        for p in paths:
            a, t = _read_halo(p, cw.bounds, halo, aot_path(cfg, p))
            core = (slice(halo, a["B02"].shape[0] - halo), slice(halo, a["B02"].shape[1] - halo))
            inside = polygon_mask(cw.box, a["B02"][core].shape, t)
            cov, clear = view_clear_frac(cfg, a, inside, halo)
            if cov < lc.strip_min_coverage or clear < lc.strip_min_clear_frac:
                continue
            if best is None or clear > best.clear_frac:
                best = View(day, p.name, float(clear), {b: a[b][core] for b in REFL},
                            pixel_clear(cfg, a, halo), t)
        if best is not None:
            out.append(best)
    return out


TC_BANDS = ("B04", "B03", "B02")
SWIR_BANDS = ("B12", "B8A", "B04")


def _pool(views: list[View], bands: tuple[str, ...]) -> np.ndarray:
    vals = np.concatenate([v.bands[b][v.clear].ravel() for v in views for b in bands]
                          or [np.zeros(0, np.int16)]).astype(np.float32)
    if vals.size < 10:  # almost nothing clear: fall back to every valid pixel
        vals = np.concatenate([v.bands[b][v.bands[b] != NODATA].ravel() for v in views
                               for b in bands] or [np.zeros(0, np.int16)]).astype(np.float32)
    return vals


def item_stretch(views: list[View], q: tuple[float, float]
                 ) -> dict[str, dict[str, tuple[float, float]]]:
    """One fixed stretch per item from the clear pixels of all its dates.

    True colour: one (lo, hi) shared by B04/B03/B02, so colours stay natural (char dark grey,
    soil brown). SWIR false colour: per band, since B8A is much brighter than B12.
    Returns ``{"tc": {band: (lo, hi)}, "swir": {band: (lo, hi)}}``.
    """
    def lim(vals: np.ndarray) -> tuple[float, float]:
        lo, hi = np.percentile(vals, q) if vals.size else (0.0, 3000.0)
        return float(lo), float(max(hi, lo + 1))

    tc = lim(_pool(views, TC_BANDS))
    return {"tc": dict.fromkeys(TC_BANDS, tc),
            "swir": {b: lim(_pool(views, (b,))) for b in SWIR_BANDS}}


def _stretch(v: View, bands: tuple[str, str, str], st: dict[str, tuple[float, float]]
             ) -> np.ndarray:
    x = np.stack([np.clip((v.bands[b].astype(np.float32) - st[b][0]) / (st[b][1] - st[b][0]),
                          0, 1) for b in bands])
    x[:, np.stack([v.bands[b] == NODATA for b in bands]).any(axis=0)] = 1
    return np.moveaxis(x, 0, -1)


def upscale(img: np.ndarray, k: int) -> np.ndarray:
    """Nearest-neighbour upscale of an (h, w[, c]) array by an integer factor ``k``."""
    return np.repeat(np.repeat(img, k, axis=0), k, axis=1)


def series_at_anchor(cfg: Config, geom: BaseGeometry, views: list[View]
                     ) -> dict[str, np.ndarray]:
    """Per date: mean NBR, albedo and NDVI of the clear pixels in a ``series_window_px``
    square centred on the anchor pixel (NaN when none is clear)."""
    k = cfg.labelling.series_window_px // 2
    a = chip_window(cfg, geom).anchor
    out: dict[str, list[float]] = {"nbr": [], "albedo": [], "ndvi": []}
    for v in views:
        c, r = (~v.transform) @ (a.x, a.y)
        r, c = math.floor(r), math.floor(c)
        sl = (slice(max(r - k, 0), r + k + 1), slice(max(c - k, 0), c + k + 1))
        for name, arr in (("nbr", v.nbr), ("albedo", v.albedo(cfg.sentinel2.reflectance_scale)),
                          ("ndvi", v.ndvi)):
            w = arr[sl]
            out[name].append(float(np.nanmean(w)) if np.isfinite(w).any() else np.nan)
    return {name: np.asarray(x, dtype=np.float64) for name, x in out.items()}


def delta_nbr(views: list[View]) -> list[np.ndarray | None]:
    """``NBR_prev - NBR_cur`` per pixel for each date (None for the first date)."""
    out: list[np.ndarray | None] = [None]
    for prev, cur in itertools.pairwise(views):
        out.append(prev.nbr - cur.nbr)
    return out


def _marker(ax, cw: ChipWindow, t) -> None:
    """Neutral box + small crosshair at the anchor pixel centre (pixel coordinates)."""
    inv = ~t
    xs, ys = zip(*[inv @ xy for xy in cw.box.exterior.coords], strict=True)
    ax.plot(xs, ys, color="cyan", lw=0.8)
    cx, cy = inv @ (cw.anchor.x, cw.anchor.y)
    arm, gap = 2.2, 0.8  # pixels; open centre so the pixel itself stays visible
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ax.plot([cx + dx * gap, cx + dx * arm], [cy + dy * gap, cy + dy * arm],
                color="yellow", lw=1.0)


SERIES_STYLE = {"nbr": ("NBR", "#c0392b"), "albedo": ("albedo x4", "#2c3e50"),
                "ndvi": ("NDVI", "#27ae60")}
ALBEDO_PLOT_SCALE = 4.0  # albedo (~0.05-0.25) drawn x4 so it shares the index axis
ROW_LABELS = ("true colour", "SWIR", "ΔNBR prev-cur")


def render_strip(cfg: Config, geom: BaseGeometry, views: list[View]) -> bytes:
    """PNG of the strip: per row of dates a chart, true colour, SWIR and ΔNBR (see module)."""
    lc = cfg.labelling
    cw = chip_window(cfg, geom)
    up, n_px = lc.tile_upscale, lc.chip_window_px
    tile_in = n_px * up / 100  # at dpi 100: one output pixel per upscaled pixel
    cols = max(1, min(lc.strip_cols, len(views) or 1))
    rows = max(1, math.ceil(len(views) / cols))
    chart_in, tile_h = 1.6, tile_in + 0.24
    fig = Figure(figsize=(0.7 + cols * (tile_in + 0.08), rows * (chart_in + 3 * tile_h + 0.45)))
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    if not views:
        ax = fig.add_subplot(1, 1, 1)
        ax.text(0.5, 0.5, "no usable clear view", ha="center", va="center")
        ax.set_axis_off()
        fig.savefig(buf, format="png", dpi=100)
        return buf.getvalue()
    st = item_stretch(views, tuple(lc.stretch_percentiles))
    ser = series_at_anchor(cfg, geom, views)
    dn = delta_nbr(views)
    cmap = matplotlib.colormaps["RdBu_r"].with_extremes(bad="0.6")
    ext = (-0.5, n_px - 0.5, n_px - 0.5, -0.5)  # keep original pixel coordinates
    gs = fig.add_gridspec(rows * 4, cols, height_ratios=[chart_in, tile_h, tile_h, tile_h] * rows,
                          hspace=0.22, wspace=0.08)
    for r in range(rows):
        idx = list(range(r * cols, min((r + 1) * cols, len(views))))
        axc = fig.add_subplot(gs[4 * r, :])
        lead = [idx[0] - 1] if idx[0] > 0 else []
        xs = np.array([i - r * cols for i in lead + idx], dtype=float)
        for name, (lab, col) in SERIES_STYLE.items():
            y = ser[name][lead + idx] * (ALBEDO_PLOT_SCALE if name == "albedo" else 1.0)
            axc.plot(xs, y, "-o", color=col, ms=4, lw=1.4, label=lab)
        axc.set_xlim(-0.5, cols - 0.5)
        axc.set_ylim(-0.4, 1.0)
        axc.set_xticks(range(len(idx)))
        axc.set_xticklabels([])  # the date is the tile title just below each tick
        axc.grid(axis="x", color="0.85", lw=0.8)
        axc.axhline(0, color="0.7", lw=0.6)
        axc.tick_params(axis="y", labelsize=7)
        if r == 0:
            axc.legend(fontsize=7, ncol=3, loc="upper right", frameon=False)
            axc.set_title(f"at crosshair ({lc.series_window_px}x{lc.series_window_px} mean of "
                          "clear pixels)", fontsize=8, loc="left", pad=2)
        for k in idx:
            c, v = k - r * cols, views[k]
            tiles = [upscale(_stretch(v, TC_BANDS, st["tc"]), up),
                     upscale(_stretch(v, SWIR_BANDS, st["swir"]), up),
                     None if dn[k] is None else upscale(dn[k], up)]
            for j, img in enumerate(tiles):
                ax = fig.add_subplot(gs[4 * r + 1 + j, c])
                ax.set_xticks([])
                ax.set_yticks([])
                if img is None:
                    ax.text(0.5, 0.5, "no previous\ndate", ha="center", va="center",
                            fontsize=7, color="0.4", transform=ax.transAxes)
                    ax.set_xlim(ext[0], ext[1])
                    ax.set_ylim(ext[2], ext[3])
                    ax.set_aspect("equal")
                elif j == 2:
                    ax.imshow(np.ma.masked_invalid(img), cmap=cmap, vmin=-lc.dnbr_display_max,
                              vmax=lc.dnbr_display_max, interpolation="nearest", extent=ext)
                else:
                    ax.imshow(img, interpolation="nearest", extent=ext)
                if img is not None:
                    _marker(ax, cw, v.transform)
                if j == 0:
                    ax.set_title(f"{v.day:%d %b}", fontsize=8, pad=2)
                if c == 0:
                    ax.set_ylabel(ROW_LABELS[j], fontsize=7)
    fig.subplots_adjust(left=0.5 / fig.get_figwidth(), right=0.995,
                        top=1 - 0.15 / fig.get_figheight(), bottom=0.05 / fig.get_figheight())
    fig.savefig(buf, format="png", dpi=100)
    return buf.getvalue()


def _cache_key(cfg: Config, sample_id: str, geom: BaseGeometry) -> str:
    lc = cfg.labelling
    files = sorted(p.name for ps in _s2_days(cfg).values() for p in ps)
    blob = json.dumps({"v": STRIP_VERSION, "id": sample_id, "files": files,
                       "excl": sorted(d.isoformat() for d in excluded_days(cfg)),
                       "p": [lc.chip_window_px, lc.crosshair_box_m, lc.tile_upscale,
                             lc.stretch_percentiles, lc.dnbr_display_max, lc.series_window_px,
                             lc.strip_min_clear_frac, lc.strip_min_coverage, lc.strip_cols,
                             cfg.quality.model_dump(), cfg.haze_mask.model_dump(mode="json")]},
                      sort_keys=True, default=str).encode() + anchor_point(cfg, geom).wkb
    return hashlib.sha1(blob).hexdigest()[:16]


def strip(cfg: Config, sample_id: str, geom: BaseGeometry) -> tuple[list[date], bytes]:
    """(usable dates, PNG) for one item, from the disk cache when possible."""
    d = cfg.path("cache", "m3_strips")
    key = _cache_key(cfg, sample_id, geom)
    png, meta = d / f"{sample_id}_{key}.png", d / f"{sample_id}_{key}.json"
    if png.exists() and meta.exists():
        days = [date.fromisoformat(x) for x in json.loads(meta.read_text("utf-8"))["days"]]
        return days, png.read_bytes()
    views = usable_views(cfg, geom)
    data = render_strip(cfg, geom, views)
    d.mkdir(parents=True, exist_ok=True)
    tmp = png.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(png)
    meta.write_text(json.dumps({"days": [v.day.isoformat() for v in views],
                                "files": [v.file for v in views],
                                "clear_frac": [round(v.clear_frac, 3) for v in views]}),
                    encoding="utf-8")
    return [v.day for v in views], data
