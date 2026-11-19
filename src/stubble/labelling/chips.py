"""Time strip for one labelling item (read on demand from the cached raw S2 views).

Blind display: every item, flagged polygon or unflagged 100 m cell, is drawn the same way,
and the geometry's shape never appears. The *anchor* is the 10 m grid pixel that contains
``geometry.representative_point()`` (always inside the geometry). Each tile is a
``chip_window_px`` x ``chip_window_px`` window (odd, so the anchor pixel is exactly in the
centre), with a small crosshair at the anchor pixel centre and a neutral
``crosshair_box_m`` square around it. The label means "the field under the crosshair"; it is
stored against the sample item, whose original geometry is used later (M4).

For every *usable clear* date the strip shows a true-colour tile (B04, B03, B02) above a SWIR
false-colour tile (B12, B8A, B04). A date is usable when it is not scene-excluded
(``radiometry.json`` / ``haze_mask.exclude_days``), the neutral box is at least
``strip_min_coverage`` covered by data and at least ``strip_min_clear_frac`` of it is SCL-valid
and not haze-masked (the detector's blue, cloud-buffer and AOT tests). The test uses the box,
not the geometry, so the choice of dates does not depend on the item's shape either. When a
date has several files, the clearest one is shown.

The display stretch is fixed per composite for every date (``rgb_max_reflectance``,
``swir_max_reflectance``), so brightness is comparable along the strip. Reflectances are the
raw L2A values (no PIF offsets). Nothing from the detector is drawn.

Rendering uses the object-oriented Matplotlib API (no pyplot), so strips can be prefetched on
a background thread. Finished strips are cached on disk under ``data/cache/m3_strips``.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

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
from stubble.detect.radiometry import haze_components, radiometry_path
from stubble.detect.timeseries import _s2_days

STRIP_VERSION = 2
BANDS = ["B02", "B03", "B04", "B8A", "B12", "SCL"]


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
    tc: np.ndarray      # (3, h, w) int16: B04, B03, B02
    swir: np.ndarray    # (3, h, w) int16: B12, B8A, B04
    transform: object


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
                best = View(day, p.name, float(clear),
                            np.stack([a["B04"][core], a["B03"][core], a["B02"][core]]),
                            np.stack([a["B12"][core], a["B8A"][core], a["B04"][core]]), t)
        if best is not None:
            out.append(best)
    return out


def _stretch(a: np.ndarray, hi: float) -> np.ndarray:
    x = np.clip(a.astype(np.float32) / hi, 0, 1)
    x[:, (a == NODATA).any(axis=0)] = 1
    return np.moveaxis(x, 0, -1)


def _marker(ax, cw: ChipWindow, t) -> None:
    """Neutral box + small crosshair at the anchor pixel centre (pixel coordinates)."""
    inv = ~t
    xs, ys = zip(*[inv * xy for xy in cw.box.exterior.coords], strict=True)
    ax.plot(xs, ys, color="cyan", lw=0.8)
    cx, cy = inv * (cw.anchor.x, cw.anchor.y)
    arm, gap = 2.2, 0.8  # pixels; open centre so the pixel itself stays visible
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ax.plot([cx + dx * gap, cx + dx * arm], [cy + dy * gap, cy + dy * arm],
                color="yellow", lw=1.0)


def render_strip(cfg: Config, geom: BaseGeometry, views: list[View]) -> bytes:
    """PNG of the strip: per date a true-colour tile above a SWIR tile."""
    lc, scale = cfg.labelling, cfg.sentinel2.reflectance_scale
    cw = chip_window(cfg, geom)
    hi_tc, hi_sw = lc.rgb_max_reflectance * scale, lc.swir_max_reflectance * scale
    cols = max(1, min(lc.strip_cols, len(views) or 1))
    rows = max(1, math.ceil(len(views) / cols))
    fig = Figure(figsize=(cols * 1.75, rows * 3.7))
    FigureCanvasAgg(fig)
    if not views:
        ax = fig.add_subplot(1, 1, 1)
        ax.text(0.5, 0.5, "no usable clear view", ha="center", va="center")
        ax.set_axis_off()
    gs = fig.add_gridspec(rows * 2, cols, hspace=0.12, wspace=0.05)
    for k, v in enumerate(views):
        r, c = divmod(k, cols)
        for j, (arr, hi) in enumerate(((v.tc, hi_tc), (v.swir, hi_sw))):
            ax = fig.add_subplot(gs[2 * r + j, c])
            ax.imshow(_stretch(arr, hi), interpolation="nearest")
            _marker(ax, cw, v.transform)
            ax.set_xticks([])
            ax.set_yticks([])
            if j == 0:
                ax.set_title(f"{v.day:%d %b}", fontsize=8, pad=2)
            if c == 0:
                ax.set_ylabel("true colour" if j == 0 else "SWIR", fontsize=7)
    fig.subplots_adjust(left=0.03, right=0.99, top=0.96, bottom=0.01)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    return buf.getvalue()


def _cache_key(cfg: Config, sample_id: str, geom: BaseGeometry) -> str:
    lc = cfg.labelling
    files = sorted(p.name for ps in _s2_days(cfg).values() for p in ps)
    blob = json.dumps({"v": STRIP_VERSION, "id": sample_id, "files": files,
                       "excl": sorted(d.isoformat() for d in excluded_days(cfg)),
                       "p": [lc.chip_window_px, lc.crosshair_box_m, lc.rgb_max_reflectance,
                             lc.swir_max_reflectance,
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
