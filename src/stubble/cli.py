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


@app.command("download-ancillary")
def download_ancillary(config: ConfigOpt = None, verbose: bool = False) -> None:
    """M2.1: fetch the S2 AOT band for cached acquisitions + ESA WorldCover (cached)."""
    from stubble.aoi import build_aoi, grid_spec
    from stubble.data.ancillary import download_aot, download_worldcover

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    prov = _provider(cfg)
    bbox = tuple(gdf.to_crs(4326).total_bounds)
    p, new = download_worldcover(cfg, prov, spec, bbox)
    console.print(f"worldcover: {p.name} {'downloaded' if new else 'cached'}")
    for key, new in download_aot(cfg, prov, spec):
        console.print(f"  aot {key} {'downloaded' if new else 'cached'}")


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


@app.command()
def detect(config: ConfigOpt = None, force: bool = False, verbose: bool = False) -> None:
    """M2 step 1: per-pixel S2 time-series scan -> burn-date windows + quality bands."""
    from stubble.aoi import aoi_mask, build_aoi, grid_spec
    from stubble.detect.timeseries import run_pixel_scan

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    stats = run_pixel_scan(cfg, spec, aoi_mask(cfg, gdf, spec), force=force)
    console.print(f"pixel scan: {stats.get('n_days')} S2 days, raw burn-like area in AOI "
                  f"{stats.get('raw_burnt_ha_in_aoi')} ha")


@app.command()
def polygons(config: ConfigOpt = None, verbose: bool = False) -> None:
    """M2 step 2: clean the mask, vectorise candidates, attach quality + S1 support."""
    from stubble.aoi import aoi_mask, build_analysis_grid, build_aoi, grid_spec
    from stubble.detect.polygons import build_candidates

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    res = build_candidates(cfg, spec, aoi_mask(cfg, gdf, spec), build_analysis_grid(cfg, gdf))
    console.print(res)


@app.command("report-m2")
def report_m2(config: ConfigOpt = None, verbose: bool = False) -> None:
    """M2 step 3: figures, weekly summaries and reports/m2/REPORT.md."""
    from stubble.report_m2 import make_report

    cfg = _setup(config, verbose)
    for p in make_report(cfg):
        console.print(f"wrote {p}")


@app.command()
def m2(config: ConfigOpt = None, verbose: bool = False) -> None:
    """Run the whole M2 baseline: detect -> polygons -> report-m2."""
    detect(config=config, verbose=verbose)
    polygons(config=config, verbose=verbose)
    report_m2(config=config, verbose=verbose)


@app.command()
def radiometry(config: ConfigOpt = None, force: bool = False, verbose: bool = False) -> None:
    """M2.1 step 0: per-scene haze loss, PIF offsets and scene exclusions."""
    from stubble.aoi import aoi_mask, build_aoi, grid_spec
    from stubble.detect.radiometry import run_radiometry

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    res = run_radiometry(cfg, spec, aoi_mask(cfg, gdf, spec), force=force)
    t = Table(title=f"Haze loss per scene (AOT > {cfg.haze_mask.haze_aot_threshold})")
    for c in ("file", "SCL-clear % AOI", "blue %", "buffer %", "AOT %", "AOT-only %",
              "usable %", "AOT p50", "excluded"):
        t.add_column(c)
    for s in res["scenes"]:
        t.add_row(s["file"], str(s["scl_clear_pct_aoi"]), str(s["blue_pct_of_clear"]),
                  str(s["buffer_pct_of_clear"]), str(s["aot_pct_of_clear"]),
                  str(s["aot_only_pct_of_clear"]), str(s["usable_pct_of_clear"]),
                  str(s.get("aot_p50", "")), "yes" if s["excluded"] else "")
    console.print(t)
    console.print(f"PIFs: {res['n_pif']} of {res['n_pif_candidates_sampled']} sampled; "
                  f"excluded days: {res['excluded_days'] or 'none'}")


@app.command("detect-harvest")
def detect_harvest(config: ConfigOpt = None, force: bool = False, verbose: bool = False) -> None:
    """M2.1 step 1: harvest-aware per-pixel scan (harvest date, burn window, labels)."""
    from stubble.aoi import aoi_mask, build_aoi, grid_spec
    from stubble.detect.harvest import run_harvest_scan

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    console.print(run_harvest_scan(cfg, spec, aoi_mask(cfg, gdf, spec), force=force))


@app.command("polygons-harvest")
def polygons_harvest(config: ConfigOpt = None, verbose: bool = False) -> None:
    """M2.1 step 2: clean + vectorise harvest-aware candidates."""
    from stubble.aoi import aoi_mask, build_analysis_grid, build_aoi, grid_spec
    from stubble.detect.polygons import build_candidates, harvest_product

    cfg = _setup(config, verbose)
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    console.print(build_candidates(cfg, spec, aoi_mask(cfg, gdf, spec),
                                   build_analysis_grid(cfg, gdf), harvest_product(cfg)))


@app.command("report-m21")
def report_m21(config: ConfigOpt = None, verbose: bool = False) -> None:
    """M2.1 step 3: old-vs-new comparison report -> reports/m21/REPORT.md."""
    from stubble.report_m21 import make_report

    cfg = _setup(config, verbose)
    for p in make_report(cfg):
        console.print(f"wrote {p}")


@app.command("sample-m3")
def sample_m3(config: ConfigOpt = None, force: bool = False, verbose: bool = False) -> None:
    """M3: draw the seeded stratified labelling sample (refuses to overwrite one in use)."""
    from stubble.aoi import aoi_mask, build_aoi, grid_spec
    from stubble.labelling.sample import build_sample
    from stubble.labelling.store import db_path, latest_labels

    cfg = _setup(config, verbose)
    man = cfg.path("labels", "m3_sample_manifest.json")
    if man.exists() and not force:
        raise typer.BadParameter(f"{man} exists; pass --force to redraw")
    if db_path(cfg).exists() and len(latest_labels(cfg)) and force:
        raise typer.BadParameter("labels already exist for the current sample; not redrawing")
    gdf = build_aoi(cfg)
    spec = grid_spec(cfg, gdf)
    m = build_sample(cfg, spec, aoi_mask(cfg, gdf, spec))
    console.print(f"seed {m['seed']}: {m['n_flagged']} flagged + {m['n_unflagged']} unflagged; "
                  f"score bin edges {m['score_bin_edges']}")
    t = Table(title="Sample per week")
    for c in ("source", "week", "n"):
        t.add_column(c)
    for r in m["per_week"]:
        t.add_row(r["source"], r["sample_week"], str(r["n"]))
    console.print(t)


@app.command()
def label(config: ConfigOpt = None, port: int = 8501) -> None:
    """M3: open the labelling tool (Streamlit) in the browser."""
    import subprocess
    import sys
    from pathlib import Path

    app_py = Path(__file__).parent / "labelling" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_py),
                    "--server.port", str(port)], check=False)


@app.command("export-labels")
def export_labels(config: ConfigOpt = None) -> None:
    """M3: write data/labels/labels.geojson from the SQLite store."""
    from stubble.labelling.store import export_geojson

    cfg = _setup(config, False)
    console.print(f"wrote {export_geojson(cfg)}")


@app.command()
def m21(config: ConfigOpt = None, verbose: bool = False) -> None:
    """Run M2.1: download-ancillary -> radiometry -> detect-harvest -> polygons-harvest ->
    report-m21 (needs M1 raw data and M2 outputs)."""
    download_ancillary(config=config, verbose=verbose)
    radiometry(config=config, verbose=verbose)
    detect_harvest(config=config, verbose=verbose)
    polygons_harvest(config=config, verbose=verbose)
    report_m21(config=config, verbose=verbose)


if __name__ == "__main__":
    app()
