"""Low-resolution quicklook figures for visual QA of composites."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.enums import Resampling

from stubble.config import Config
from stubble.data.composites import NODATA


def _read(path: Path, bands: list[str], factor: int) -> tuple[np.ndarray, rasterio.Affine]:
    with rasterio.open(path) as ds:
        ix = {d: i + 1 for i, d in enumerate(ds.descriptions)}
        shape = (len(bands), ds.height // factor, ds.width // factor)
        arr = ds.read([ix[b] for b in bands], out_shape=shape, resampling=Resampling.nearest)
        t = ds.transform * ds.transform.scale(ds.width / shape[2], ds.height / shape[1])
    return arr, t


def _stretch(rgb: np.ndarray, lo: float, hi: float) -> np.ndarray:
    x = np.clip((rgb.astype(np.float32) - lo) / (hi - lo), 0, 1)
    x[:, (rgb == NODATA).any(axis=0)] = 1.0  # no data -> white
    return np.moveaxis(x, 0, -1)


def _extent(t: rasterio.Affine, shape: tuple[int, int]) -> list[float]:
    return [t.c, t.c + t.a * shape[1], t.f + t.e * shape[0], t.f]


def make_quicklooks(cfg: Config, factor: int = 8) -> list[Path]:
    """Write SWIR false-colour, true-colour, valid-obs and S1 VH panels under reports/m1/."""
    out_dir = cfg.path("reports", "m1")
    out_dir.mkdir(parents=True, exist_ok=True)
    aoi = gpd.read_file(cfg.path("processed", "aoi", "aoi.geojson")).to_crs(cfg.aoi.crs)
    wins = cfg.compositing_windows()
    written = []

    panels = [
        ("s2_swir_falsecolor.png", "s2", ["B12", "B08", "B04"],
         "S2 SWIR false colour (B12/B8/B4): burn scars appear dark brown/maroon",
         lambda a: _stretch(a, 0, 4000), None),
        ("s2_truecolor.png", "s2", ["B04", "B03", "B02"], "S2 true colour (B4/B3/B2)",
         lambda a: _stretch(a, 0, 2500), None),
        ("s2_valid_obs.png", "s2", ["valid_obs"],
         "S2 cloud-free observations per pixel (SCL-masked)", lambda a: a[0], "viridis"),
        ("s1_vh_db.png", "s1", ["VH_db"], "S1 VH backscatter (dB)",
         lambda a: np.where(a[0] == NODATA, np.nan, a[0] / cfg.sentinel1.db_scale), "gray"),
    ]
    for fname, sensor, bands, title, fn, cmap in panels:
        files = [(w, cfg.path("processed", sensor, f"{w.id}.tif")) for w in wins]
        files = [(w, p) for w, p in files if p.exists()]
        if not files:
            continue
        ncol = 3
        nrow = int(np.ceil(len(files) / ncol))
        fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 4.8 * nrow), squeeze=False)
        for ax in axes.ravel():
            ax.axis("off")
        for ax, (w, p) in zip(axes.ravel(), files, strict=False):
            arr, t = _read(p, bands, factor)
            img = fn(arr)
            kw = {}
            if cmap == "viridis":
                kw = dict(cmap=cmap, vmin=0, vmax=max(3, int(np.nanmax(img))))
            elif cmap == "gray":
                kw = dict(cmap=cmap, vmin=-25, vmax=-8)
            im = ax.imshow(img, extent=_extent(t, arr.shape[1:]), **kw)
            aoi.boundary.plot(ax=ax, color="cyan" if cmap is None else "red", linewidth=0.6)
            ax.set_title(f"{w.id}: {w.label}", fontsize=9)
            if cmap:
                fig.colorbar(im, ax=ax, fraction=0.04)
        fig.suptitle(title)
        fig.tight_layout()
        path = out_dir / fname
        fig.savefig(path, dpi=110)
        plt.close(fig)
        written.append(path)
    return written
