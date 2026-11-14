# Stubble Burn-Area Audit: Sangrur, Punjab (2025 season)

> **Decision support only.** Outputs are model estimates with confidence scores, not proof of
> any offence. Every flag starts as `pending_field_verification`. No personal data is used.

*Work in progress. The full README (method, metrics, limitations, ethics) arrives in M8.*

## Quick start (M1 data pipeline)

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"    # or: pip install -r requirements.txt && pip install -e .
cp .env.example .env                          # FIRMS key needed from M5 onward
stubble aoi          # district boundary + 5 km analysis grid
stubble download     # cache every S2 / S1 acquisition on the AOI grid (data/raw, never re-fetched)
stubble composite    # pre-season + weekly median composites -> data/processed/{s2,s1}/*.tif (COG)
stubble quicklook    # QA figures -> reports/m1/
pytest && ruff check src tests
```

## M2 baseline burn detection

```bash
stubble detect       # per-pixel S2 time-series scan: burn-date windows + quality bands
stubble polygons     # clean mask, vectorise candidates, quality + Sentinel-1 support attributes
stubble report-m2    # figures, weekly summaries, single-observation list -> reports/m2/REPORT.md
# or: stubble m2
```

Detection uses each pixel's full series of clear S2 observations, not weekly median
composites. The burn-date window runs from the last clear pre-burn look to the first
post-burn look. Sentinel-1 VV/VH change is stored per polygon as supporting evidence only.
It never creates a flag. See `reports/m2/REPORT.md`.

## M2.1 harvest-aware detection

The M2 baseline compares against pre-season green paddy, so harvest alone crosses its
threshold (81 % of the district flagged). M2.1 finds each pixel's harvest date (NDVI) and
tests for burns only between consecutive clear views after harvest. A burn needs a
short-interval NBR drop, a visible+NIR albedo drop and an NBR2 drop. Pixels with no clear
view after harvest are `unobserved`, never `not_burnt`. All thresholds are in `config.yaml`
(`harvest_detection`, `burn_detection`).

```bash
stubble detect-harvest     # per-pixel labels, harvest date, burn-date window
stubble polygons-harvest   # candidates -> data/outputs/m21/candidates.gpkg (IDs SGR-nnnn)
stubble report-m21         # old vs new comparison -> reports/m21/REPORT.md
```

## Data sources (M1)
| Data | Source | Licence |
|---|---|---|
| Sangrur boundary (post-2021, excludes Malerkotla) | OpenStreetMap relation 9513553 via Nominatim | ODbL 1.0, (c) OpenStreetMap contributors |
| Sentinel-2 L2A | Microsoft Planetary Computer `sentinel-2-l2a` | Copernicus open licence |
| Sentinel-1 RTC (VV, VH) | Microsoft Planetary Computer `sentinel-1-rtc` | Copernicus open licence, RTC processing by Catalyst/PC |

Every file under `data/raw` and `data/processed` has a `*.provenance.json` sidecar listing scene
IDs, dates, parameters and code version.
