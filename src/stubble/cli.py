"""Command-line entry point: ``stubble <command>``."""

from __future__ import annotations

import json
import logging
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from stubble.config import Config, load_config

app = typer.Typer(add_completion=False, help="Stubble burn-area audit pipeline.")
console = Console()


def _setup(config: str | None, verbose: bool) -> Config:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False)])
    for noisy in ("rasterio", "urllib3", "botocore", "azure"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return load_config(config)


def _provider(cfg: Config):
    if cfg.provider == "planetary":
        from stubble.data.planetary import PlanetaryComputerProvider
        return PlanetaryComputerProvider(cfg)
    if cfg.provider == "gee":
        from stubble.data.gee import EarthEngineProvider
        return EarthEngineProvider(cfg)
    raise typer.BadParameter(f"unknown provider {cfg.provider}")


ConfigOpt = Annotated[str | None, typer.Option("--config", "-c", help="Path to config.yaml")]
SensorOpt = Annotated[str, typer.Option("--sensor", "-s", help="s2, s1 or all")]


@app.command()
def aoi(config: ConfigOpt = None, force: bool = False, verbose: bool = False) -> None:
    """Build the AOI boundary and the analysis grid."""
    from stubble.aoi import build_analysis_grid, build_aoi, grid_spec

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg, force=force)
    grid = build_analysis_grid(cfg, gdf, force=force)
    spec = grid_spec(cfg, gdf)
    console.print(f"AOI source: [bold]{gdf.source.iloc[0]}[/] | {gdf.note.iloc[0]}")
    console.print(f"AOI area: {gdf.area_km2.iloc[0]:.1f} km2 | analysis cells: {len(grid)}")
    console.print(f"Raster grid: {spec.width} x {spec.height} px @ {spec.resolution} m, "
                  f"{spec.crs}, bounds {tuple(round(b) for b in spec.bounds)}")


@app.command()
def download(sensor: SensorOpt = "all", config: ConfigOpt = None, verbose: bool = False) -> None:
    """Search the catalogue and cache every acquisition overlapping the AOI and season."""
    from stubble.aoi import build_aoi, grid_spec
    from stubble.data.composites import download_acquisition, group_acquisitions

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    prov = _provider(cfg)
    wins = cfg.compositing_windows()
    start, end = wins[0].start, wins[-1].end
    bbox = tuple(gdf.total_bounds) if not cfg.aoi.subset_bbox else tuple(cfg.aoi.subset_bbox)
    sensors = ["s2", "s1"] if sensor == "all" else [sensor]
    for sen in sensors:
        scenes = prov.search(sen, start, end, bbox)
        acqs = group_acquisitions(sen, scenes)
        # cache the search result itself
        cache = cfg.path("cache", f"search_{sen}.json")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([s.to_record() for s in scenes], indent=1), encoding="utf-8")
        console.print(f"[bold]{sen}[/]: {len(scenes)} scenes -> {len(acqs)} acquisitions")
        new = 0
        for i, acq in enumerate(acqs, 1):
            _path, fetched = download_acquisition(cfg, prov, acq, spec)
            new += fetched
            console.print(f"  [{i}/{len(acqs)}] {acq.key} ({len(acq.scenes)} scenes) "
                          f"{'downloaded' if fetched else 'cached'}")
        console.print(f"{sen}: {new} downloaded, {len(acqs) - new} already cached")


@app.command()
def composite(sensor: SensorOpt = "all", config: ConfigOpt = None, force: bool = False,
              verbose: bool = False) -> None:
    """Build per-window median composites (COGs) from cached acquisitions."""
    from stubble.aoi import aoi_mask, build_aoi, grid_spec
    from stubble.data.composites import composite_window

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    mask = aoi_mask(cfg, gdf, spec)
    sensors = ["s2", "s1"] if sensor == "all" else [sensor]
    rows = []
    for sen in sensors:
        for w in cfg.compositing_windows():
            st = composite_window(cfg, sen, w, spec, mask, force=force)
            rows.append(st)
            console.print(f"  {sen} {w.id} done")
    summary = cfg.path("reports", "m1", "composite_summary.json")
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    t = Table(title="Composite coverage inside AOI")
    for c in ("sensor", "window", "dates", "acq", "% px >=1 obs", "% px >=2 obs", "mean obs"):
        t.add_column(c)
    for r in rows:
        t.add_row(r.get("sensor", ""), r.get("window", ""), r.get("label", ""),
                  str(r.get("n_acq", 0)), f"{r.get('aoi_valid_pct', 0):.1f}",
                  f"{r.get('aoi_valid2_pct', 0):.1f}", f"{r.get('mean_obs', 0):.2f}")
    console.print(t)


@app.command()
def quicklook(config: ConfigOpt = None, factor: int = 8, verbose: bool = False) -> None:
    """Render QA figures under reports/m1/."""
    from stubble.quicklook import make_quicklooks

    cfg = _setup(config, verbose)
    for p in make_quicklooks(cfg, factor=factor):
        console.print(f"wrote {p}")


@app.command()
def m1(config: ConfigOpt = None, verbose: bool = False) -> None:
    """Run the whole M1 data pipeline: aoi -> download -> composite -> quicklook."""
    aoi(config=config, verbose=verbose)
    download(sensor="all", config=config, verbose=verbose)
    composite(sensor="all", config=config, verbose=verbose)
    quicklook(config=config, verbose=verbose)


if __name__ == "__main__":
    app()
