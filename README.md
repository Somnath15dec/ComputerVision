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

## Data sources (M1)
| Data | Source | Licence |
|---|---|---|
| Sangrur boundary (post-2021, excludes Malerkotla) | OpenStreetMap relation 9513553 via Nominatim | ODbL 1.0, (c) OpenStreetMap contributors |
| Sentinel-2 L2A | Microsoft Planetary Computer `sentinel-2-l2a` | Copernicus open licence |
| Sentinel-1 RTC (VV, VH) | Microsoft Planetary Computer `sentinel-1-rtc` | Copernicus open licence, RTC processing by Catalyst/PC |

Every file under `data/raw` and `data/processed` has a `*.provenance.json` sidecar listing scene
IDs, dates, parameters and code version.
