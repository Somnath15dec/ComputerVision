# Project: Stubble Burn-Area Audit (Punjab)

You are helping me build a project end to end. Read this whole brief, then **start in plan mode**: propose the repo structure and a milestone plan, wait for my approval, and only then write code. Work one milestone at a time. After each one, run it, show me the output, and stop for my review.

## Problem
Official stubble-burning counts in Punjab/Haryana come mostly from polar-orbiting satellites (MODIS/VIIRS, ~13:30 IST overpass). Studies show farmers have shifted burning to late afternoon, so fire-hotspot counts undercount real burning. A burnt field stays visible for days, so **burnt-area mapping** can't be dodged by timing.

**Goal:** for one district (Sangrur, Punjab) and one season (Oct 1 – Nov 30, 2025), estimate burnt area from Sentinel-2 + Sentinel-1, compare it with NASA FIRMS hotspot counts, and present the gap on a dashboard. Also produce per-field evidence packs for human verification and "no burn detected" certificates for incentive eligibility.

## Hard constraints (non-negotiable)
- **No contacting anyone, no fines, no automated enforcement.** The system is decision support only. Every flag has status `pending_field_verification` until a human changes it.
- **No personal data.** No owner names, phone numbers or land-record scraping. Fields use synthetic IDs (e.g. `SGR-0001`) derived from geometry.
- Every prediction carries a confidence score. Never present a model output as a confirmed fact in the UI.
- Free/open data only. Secrets (API keys, GEE credentials) go in `.env`, never committed. Provide `.env.example`.

## Stack
- **Python 3.11** for data and ML. Use `uv` or `pip` with a `requirements.txt`.
- **Data access:** Microsoft Planetary Computer STAC (`pystac-client`, `planetary-computer`, `stackstac`/`odc-stac`), so no account approval is needed. Make the data layer swappable so Google Earth Engine can be added later.
  - Sentinel-2 L2A: bands B4, B8, B8A, B11, B12, SCL (cloud mask)
  - Sentinel-1 RTC (VV, VH)
  - NASA FIRMS VIIRS active fire (CSV via FIRMS API; key in `.env`)
- **Geo:** `rasterio`, `xarray`, `geopandas`, `shapely`
- **ML:** PyTorch + `segmentation-models-pytorch` (U-Net, ResNet34 encoder)
- **Backend:** FastAPI
- **Frontend:** React + Vite + Leaflet (or MapLibre)
- **Deployment:** Docker + docker-compose (backend, frontend, optional PostGIS)

## Milestones

### M1: Data pipeline
- AOI: Sangrur district boundary (from a public GeoJSON source; if none can be downloaded, use a bounding box around 30.24N, 75.84E with a 25 km buffer and note that).
- Download/cache pre-season composite (Sep 20 – Oct 5) and weekly post-harvest composites (Oct 6 – Nov 30) for Sentinel-2, cloud-masked using SCL.
- Matching Sentinel-1 VV/VH composites.
- Save as Cloud-Optimized GeoTIFFs under `data/processed/`. Cache aggressively; never redownload existing tiles.

### M2: Baseline burn detection
- NBR = (B8 − B12) / (B8 + B12). dNBR = NBR_pre − NBR_post.
- Baseline mask: dNBR > 0.27 (configurable), cleaned with morphological opening and a minimum patch size (≥ 0.1 ha).
- Output: burnt-area raster + vectorized polygons with area (ha) and the week detected.
- Write a short notebook/report with sample maps showing where the baseline produces false positives (tilled or wet soil).

### M3: Labelling tool
- A minimal labelling utility (Streamlit or a small React page) that shows pre/post RGB, dNBR and SAR chips for a candidate polygon and lets me label: `burnt`, `tilled_not_burnt`, `harvested_not_burnt`, `wet`, `unsure`.
- Store labels in GeoJSON/SQLite. Target: ~300–500 labelled polygons.

### M4: Segmentation model
- Input stack: B4, B8, B11, B12, dNBR, VV, VH (and VV/VH change). Normalize per band.
- U-Net trained on chips derived from the labels. **Spatially split** train/val/test by tile (no random pixel splits, since that leaks).
- Report precision, recall, F1 and IoU on the test split, plus a confusion matrix against the baseline from M2. Be honest if the model doesn't beat the baseline.
- Calibrate confidence (temperature scaling or similar).
- **Observation-density breakdown** (added 2026-11-19, fixed before labelling; reporting
  layout revised 2026-11-19). Splits:
  - **strip length**: short (<= 16 usable dates in the labelling strip) vs long (> 16);
  - **region**: west vs east of easting 584 602 m (EPSG:32643; the midpoint of the AOI
    bounding box).
  Every short strip is in the west, so the two splits are nested: west/short, west/long, east.
  - **Headline: west vs east** (west = west/short + west/long). For each region: label
    distribution, repeat agreement, and every model metric (baseline and U-Net: precision,
    recall, F1, IoU) with confidence intervals, plus the all-items margin.
  - **Supplement: full per-cell table** (west/short, west/long, east, margins) with the same
    quantities and confidence intervals. In the 200 labelled items the cells hold 58 / 30 /
    112 items, of which unflagged 25 / 3 / 13 (full 400-item sample: 106 / 72 / 222, unflagged
    41 / 9 / 30). West/long has only 3 unflagged items: show its CIs, do not over-read it.
  - Use `subset_inclusion_weight` from the label export (stratum population / items of that
    stratum in the 200) for every estimate.
- **Label-aid bias check, next to the baseline metrics** (added 2026-11-19). The labelling
  tool shows the crosshair NBR/albedo/NDVI chart and the ΔNBR row on first showings, but the
  20 hidden repeats get imagery only (true colour + SWIR). The chart shows the same features
  the M2.1 rule uses, so it may pull labels toward the rule and flatter the baseline. Report
  from `stubble label-agreement`, beside the baseline metrics table: chart-on vs chart-off
  agreement (raw, kappa), burnt and unsure rates with and without the chart, the discordant
  burnt pairs in each direction (exact McNemar p), and how often the label matches the M2.1
  flag with and without the chart. With no chart-off/chart-off control, repeat noise and
  the chart effect cannot be separated, so read the gap as an upper bound. With 20 pairs it is
  a rough check: say so.

### M5: FIRMS comparison (the headline result)
- Pull VIIRS hotspots for the AOI and season.
- Per village/grid cell (e.g. 5 km) and per week: burnt hectares (our estimate) vs hotspot count.
- Produce a chart and a table of where the gap is biggest. Phrase findings as estimates with uncertainty, not accusations.
- **Caveat: low-observation areas** (added 2026-11-19). The west of the AOI has far fewer
  usable Sentinel-2 views (about 14-16 vs about 27 per item in M3). Burn detection needs clear
  views around the burn date, so burnt area there may be undercounted, and a large
  FIRMS-minus-estimate gap may reflect missing observations rather than real disagreement.
  State this next to the results, and use the M4 west/short metrics to size the effect.
- Show an **observation-count map** (usable clear S2 views per pixel or grid cell over the
  season) next to the burnt-area map, at the same extent and grid.
- **External reference: ISRO NRSC burnt-area figures** (added 2026-11-19). If ISRO's National
  Remote Sensing Centre has published MIRBI-based (Mid-Infrared Burn Index) burnt-area figures
  for Sangrur for the 2025 season, put them next to our estimate and the FIRMS counts as a
  third reference that does not depend on my labels. Record the source document, date,
  method, spatial unit and date window. Compare like with like (same boundary and dates, or
  say how they differ). If no figure for Sangrur 2025 can be found, or only state-level totals,
  say so and stop to ask; never estimate or back-fill an NRSC number.

### M6: Evidence packs and certificates
- For each flagged polygon, an evidence pack:
```json
{
  "field_id": "SGR-0001",
  "grid_cell": "SGR-G-012",
  "burn_confidence": 0.91,
  "detected_window": "2025-11-06/2025-11-11",
  "firms_hotspot_nearby": true,
  "evidence": ["pre_rgb.png", "post_rgb.png", "dnbr.png", "sar_change.png"],
  "status": "pending_field_verification"
}
```
- "No burn detected" certificate (JSON + simple PDF) for fields with good cloud-free coverage across the whole season and low burn probability. If coverage is insufficient, say `insufficient_data` instead of certifying.

### M7: API + dashboard
- FastAPI endpoints: `/fields`, `/fields/{id}`, `/fields/{id}/evidence`, `/fields/{id}/status` (PATCH, for human verification), `/summary/weekly`, `/compare/firms`, `/certificates/{id}`.
- Dashboard: map with burnt polygons colored by confidence, FIRMS points layer, week slider, "official count vs estimated burnt area" chart, verification queue sorted by confidence.
- A clear disclaimer in the UI: estimates for decision support, not proof of an offence.

### M8: Packaging
- `docker-compose up` brings up everything with sample cached data.
- README: problem, data sources, method, metrics, limitations (resolution, cloud/smoke, tilled-soil confusion, leased land, mixed pixels at field edges), and ethics section.
- Tests: pytest for index math, masking, the API, and a smoke test for the pipeline on one small tile.

## Engineering rules
- Config in one `config.yaml` (dates, AOI, thresholds, paths).
- Type hints, docstrings on public functions, `ruff` for linting.
- Log data provenance (scene IDs, dates) for every output file so results are reproducible.
- If something is ambiguous or a data source is unavailable, stop and ask me instead of inventing data. Never fabricate metrics or sample results.

Start now: give me the repo tree and milestone plan for approval.
