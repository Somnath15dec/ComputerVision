# Stubble Burn-Area Audit: Sangrur, Punjab (2025 season)

> **Research prototype, decision support only.** Burnt areas are unvalidated satellite
> estimates. Every polygon has status `pending_field_verification`. No contact with anyone, no
> fines, no automated enforcement, no personal data.

**Live dashboard:** LIVE-URL-PLACEHOLDER (Streamlit Community Cloud; the free app sleeps after
inactivity: click the wake-up button and wait for it to restart) ·
**Code:** https://github.com/Somnath15dec/ComputerVision ·
**Report and slides:** `reports/final/`

## Overview
Official stubble-burning counts come from active-fire hotspots (MODIS / VIIRS) seen at fixed
overpass times, and burning has reportedly shifted to the late afternoon. A burn scar stays
visible for days. This project maps **candidate burnt area** for Sangrur district, 1 Oct - 30
Nov 2025, from Sentinel-2 (Sentinel-1 as supporting evidence), compares it with NASA FIRMS VIIRS
hotspots per 5 km cell and week, checks it against a blind labelled sample, and publishes the
result in a dashboard.

Pipeline: **M1** data and composites -> **M2** dNBR baseline (kept as a reference; it measured
harvest) -> **M2.1** harvest-aware detection with burn-date windows and quality bands -> **M3**
blind labelling and review rounds -> **M5-lite** FIRMS comparison -> dashboard. M4 (U-Net) is
future work.

## Results summary (all estimates)
| | value | source |
|---|---|---|
| M2 baseline flagged | 236,566 ha, 80.6 % of the district (tracks harvest) | `reports/m2/REPORT.md` |
| M2.1 candidate burnt area | 135,979 ha, 46.3 % of the district, 97,137 polygons | `reports/m21/REPORT.md` |
| In season (1 Oct - 30 Nov) | 135,835 ha | `reports/m5lite/FINDINGS.md` |
| VIIRS detections (S-NPP + NOAA-20) | 857; 852 between 12:00 and 14:59 IST | same |
| Agreement per 5 km cell | Spearman 0.55 (162 cells); hotspots peak w04, area w05 | same |
| West vs east | 69,321 / 66,514 ha; 21.2 / 30.1 clear S2 views per polygon | same |
| Labels (200, blind, one labeller) | burnt 134 / not burnt 57 / unsure 9 | `data/labels/m3_review_r2_report.json` |
| Design-weighted burnt share | M2.1 polygons 76 % (count), 90 % (area); unflagged cropland 38 % | `reports/m5lite/FINDINGS.md` |
| Labeller self-agreement | 55 %, kappa 0.17 (20 pairs); chart on/off McNemar p = 0.289 | r2 report |

The labels are not reliable enough to correct the area, so no precision or recall is claimed.

## Folder structure
```
app/                    Streamlit dashboard (reads only deploy/data/) + its requirements.txt
config.yaml             single source of configuration (paths, windows, thresholds, labelling)
deploy/data/            small precomputed files for the dashboard (~13 MB, committed)
reports/                m1/ m2/ m21/ m3/ m5lite/: generated reports and figures
reports/final/          IEEE report (report.tex, report.pdf, figures/) and slide deck
scripts/regen_all.sh    rebuild every pipeline output in order
scripts/make_slides.py  build the slide deck from reports/final/figures (needs python-pptx)
src/stubble/            package: cli.py (typer CLI `stubble`), aoi, config, provenance,
                        data/ (providers, downloads, composites), detect/ (indices, M2 time
                        series, M2.1 harvest scan, radiometry, polygons), labelling/ (sample,
                        blind queue, strips, SQLite store, review), firms.py, m5lite.py,
                        deploy_data.py, report_m2.py, report_m21.py
tests/                  pytest suite (synthetic data, no downloads)
data/                   raw/ cache/ processed/ outputs/ labels/ (not committed, several GB)
```

## Setup (Python 3.11)
```bash
uv venv --python 3.11 .venv                  # or: python3.11 -m venv .venv
uv pip install --python .venv -e ".[dev]"    # or: pip install -r requirements.txt && pip install -e .
cp .env.example .env                         # add FIRMS_MAP_KEY (free) for firms-fetch; never commit .env
pytest && ruff check src app scripts tests
```
The pipeline needs internet access (Planetary Computer, OpenStreetMap, FIRMS) and several GB of
disk for `data/`. Downloads are cached and never repeated; each step skips outputs whose
provenance sidecar matches.

## Rebuild everything: `scripts/regen_all.sh`
```bash
bash scripts/regen_all.sh                                   # Linux / macOS
STUBBLE=.venv/Scripts/stubble bash scripts/regen_all.sh     # Windows (Git Bash)
CONFIG=other.yaml bash scripts/regen_all.sh                 # another config file
```
It runs, in order: `aoi`, `download`, `composite`, `quicklook` (M1); `detect`, `polygons`,
`report-m2` (M2); `download-ancillary`, `radiometry`, `detect-harvest`, `polygons-harvest`,
`report-m21` (M2.1); `firms-fetch`, `m5lite` (M5-lite); and `deploy-data` if the M3 label
exports exist. `download`, `download-ancillary` and `radiometry` are prerequisites of the steps
after them. The M3 labelling steps need a person and are not in the script.

## M1 data pipeline
```bash
stubble aoi          # district boundary + 5 km analysis grid
stubble download     # cache every S2 / S1 acquisition on the AOI grid (data/raw, never re-fetched)
stubble composite    # pre-season + weekly median composites -> data/processed/{s2,s1}/*.tif (COG)
stubble quicklook    # QA figures -> reports/m1/
# or: stubble m1
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

M2.1 is frozen as the candidate generator. Its final inputs are corrected before any index:
hazy views are skipped (blue B02 test, cloud buffer, Sen2Cor AOT; `haze_mask`), whole dates are
dropped by a rule fixed in advance (too little survives the haze mask, or unstable
pseudo-invariant pixels), and each acquisition gets a per-band offset from ESA WorldCover
built-up/bare pixels that stay stable all season (`radiometry`). A field that goes straight
from green to a strong char signature is `harvest_and_burn_same_window`.

```bash
stubble download-ancillary # Sen2Cor AOT for the cached scenes + ESA WorldCover (cached)
stubble radiometry         # haze loss per scene, PIF offsets, excluded dates
stubble detect-harvest     # per-pixel labels, harvest date, burn-date window
stubble polygons-harvest   # candidates -> data/outputs/m21/candidates.gpkg (IDs SGR-nnnn)
stubble report-m21         # old vs new comparison -> reports/m21/REPORT.md
```

## M3 labelling

```bash
stubble sample-m3          # seeded stratified sample of 400 -> data/labels/m3_sample*.json|geojson
stubble label-prerender    # optional: render the 200 queued items' strips into data/cache
stubble label              # blind Streamlit labelling tool (http://localhost:8501)
stubble label-export       # data/labels/m3_labels.geojson + m3_repeat_agreement.json
stubble label-agreement    # repeat agreement + chart-on vs chart-off comparison
stubble review-select      # freeze the current round's review queue (labelling.review_round)
stubble label --review     # label the review queue (new rows appended; originals kept)
stubble review-report      # changes per group, final counts, burnt rates, repeat agreement
```

80 % of the sample comes from M2.1 candidates (5 equal-count bins of the rule-based
`detection_score`) and 20 % from unflagged cropland (100 m cells). Both are spread across
weeks, with w04 weighted double. The manifest records the seed, the stratum populations and
the inclusion weights. No personal data is stored.

The labelling tool is blind. It does not show the sample id, detection score, score bin,
stratum or flagged/unflagged source. The 400 items are put in a seeded shuffled order
(`labelling.queue_seed`) and only the **first 200** of that order are labelled
(`labelling.n_label`), a seeded random half of the stratified sample (all 49 strata are
present; 159 flagged / 41 unflagged). 20 of them come back a second time, unmarked, at least
40 items later: 220 queue entries. The repeats are shown with **imagery only** (true colour +
SWIR, no chart, no ΔNBR row), so `stubble label-agreement` reports chart-on vs chart-off
agreement: how far the chart pulls labels toward the M2.1 rule features (an upper bound,
since it also contains the labeller's own noise). Each label records what was shown
(`display`: `aids` or `imagery`). Labels made in the earlier tool, which had no chart, count as
unlabelled until labelled again. On export,
`subset_inclusion_weight` (stratum population / items of that stratum in the 200) replaces the
full-sample weight for estimates.

Labels (keys `1`-`3`): `burnt`, `not_burnt`, `unsure`. Labels from the earlier 5-class tool
(`tilled_not_burnt`, `harvested_not_burnt`, `wet`) are still accepted and readable; exports keep
the raw `label` and add `label_3`, which folds them into `not_burnt`.

Each item shows every usable clear date in rows of 8. Per row, top to bottom:
- a chart at the crosshair (mean of the clear pixels in a 3x3 square): NBR, visible+NIR albedo
  (mean of B02, B03, B04, B08, drawn x4) and NDVI, with each date's marker directly above its
  tiles. A burn after harvest shows as a sudden NBR + albedo drop;
- true colour (B4, B3, B2) and SWIR false colour (B12, B8A, B4), upscaled x4 with nearest
  neighbour, with one fixed contrast stretch per item computed from all its dates (2-98 %;
  one range for the three true-colour bands, per band for SWIR);
- ΔNBR = NBR(previous date) - NBR(this date) on a fixed ±0.4 scale for every item (red = drop),
  so a burn is a hot spot on one date. Pixels cloudy or hazy in either view are grey.

Every item is shown the same way: a 310 m window centred on the pixel that holds the
geometry's `representative_point()`, a crosshair there and a neutral 100 m box. No outline is
drawn. The label means "the field under the crosshair". It is stored against the sample item,
so M4 still uses the original polygon or cell. "Usable" means the same scene exclusions and
haze tests as the M2.1 detector, plus at least 50 % of the 100 m box clear. Left/right arrows
go back and forward, and `N` jumps to the next unlabelled entry. A burnt label needs a burn
date (`labelling.require_burn_date`): click the first date the field turns dark, then press
`1`; the burnt key stays disabled until a date is picked. Every change is saved to SQLite
straight away.

**Speed bump.** The label keys are disabled for 4 s after an item appears, with a countdown
(`labelling.min_view_seconds`); an earlier save is refused. Every row records `view_seconds`.

**Review round r1.** The first pass had 97 of 220 labels saved 2 s or less after the previous
save. `stubble review-select` freezes a queue of those 97 plus 20 random slower labels as a
control (seed 20251120), mixed in one seeded order, in `data/labels/m3_review_r1.json`. The
review tool shows nothing about the group or the earlier label, and uses each entry's original
display. Review labels are appended with `review_round = r1` and become the current label; the
originals stay in the store as history. `stubble review-report` gives the share of labels
changed in each group (Wilson 95 % CI), final counts, burnt rate by source and by week, and
the repeat agreement recomputed on the final labels. Result: fast 57 / 96 changed, control
10 / 20 (mostly not_burnt -> burnt), so the slower labels were not reliable either.

**Review round r2** (rule `remaining`, seed 20251121, `data/labels/m3_review_r2.json`): the
103 current labels not in r1, plus the one r1 entry that was never reviewed (carryover), blind
and mixed; then a burn-date pass over the 35 r1 burnt labels that have no date, shown with a
note that the item was labelled burnt, so the date can be added (or the label changed). The
r2 report counts each group separately; earlier rounds are read from their own rows, so a
later round does not change them.

## M5-lite: FIRMS comparison
```bash
stubble firms-fetch  # VIIRS S-NPP + NOAA-20 (standard archive) for the AOI, 5-day API requests
stubble m5lite       # burnt ha vs hotspots per 5 km cell and week -> data/outputs/m5lite/,
                     # figures + FINDINGS.md in reports/m5lite/
stubble deploy-data  # dashboard package -> deploy/data/ (fails if over 50 MB)
```
Weeks always come from each polygon's `detect_date` (w00 = 1-5 Oct, then 7-day weeks from 6 Oct).
The stored `week_detected` attribute puts pre-6 Oct detections into w08 and is not used.

## Data sources
| Data | Source | Licence |
|---|---|---|
| Sangrur boundary (post-2021, excludes Malerkotla) | OpenStreetMap relation 9513553 via Nominatim | ODbL 1.0, (c) OpenStreetMap contributors |
| Sentinel-2 L2A | Microsoft Planetary Computer `sentinel-2-l2a` | Copernicus open licence |
| Sentinel-1 RTC (VV, VH) | Microsoft Planetary Computer `sentinel-1-rtc` | Copernicus open licence, RTC processing by Catalyst/PC |
| Sentinel-2 L2A AOT (Sen2Cor aerosol optical thickness) | Microsoft Planetary Computer `sentinel-2-l2a`, asset `AOT` | Copernicus open licence |
| ESA WorldCover 10 m 2021 v200 | Microsoft Planetary Computer `esa-worldcover` | CC BY 4.0, (c) ESA WorldCover project 2021 |
| VIIRS 375 m active fire (S-NPP, NOAA-20, standard processing) | NASA FIRMS area API (free MAP_KEY) | NASA open data |

Every file under `data/raw` and `data/processed` has a `*.provenance.json` sidecar listing scene
IDs, dates, parameters and code version.

## Dashboard (deployment)
`app/dashboard.py` is a Streamlit app that reads only `deploy/data/` (about 13 MB, rebuilt with
`stubble deploy-data` after `stubble m5lite`). Pages: **Map** (week slider; layers for the
largest candidate polygons coloured by week, all candidates as points, VIIRS hotspots and the
labelled sample), **Weekly comparison** (area vs hotspots per week, hotspots by IST hour, grid
maps, west/east table) and **Method & limitations** (full disclaimer, method, validation
numbers, ethics). Every chart and map labels the area "M2.1 candidate burnt area (unvalidated
estimate)". Dependencies are pinned in `requirements-app.txt` (identical copy in
`app/requirements.txt`, which Streamlit Community Cloud reads next to the entry point).

```bash
pip install -r requirements-app.txt      # Python 3.11; no geospatial or deep-learning stack
streamlit run app/dashboard.py
```
Streamlit Community Cloud: repository `Somnath15dec/ComputerVision`, branch `main`, main file
`app/dashboard.py`, Python 3.11 (Advanced settings).

## Limitations
- **Label reliability:** one labeller, no field visits; self-agreement 55 % (kappa 0.17); labels
  moved strongly toward burnt across review rounds. The labelling chart shows the rule's own
  indices and may pull labels toward it (McNemar p = 0.289 at n = 20, not significant).
- **10-20 m resolution:** small fields and mixed pixels at field edges; adjacent fields with the
  same detection date merge.
- **Haze and smoke:** 28 Oct was 1.4 % clear and 30 Oct was excluded, so late-October burns are
  first seen in November and windows widen.
- **Tilled or wet soil** can look like a burn scar.
- **Observation gaps in the west:** about 21 clear views per polygon vs 30 in the east.
- **Threshold dependence:** the area moves by tens of percent under plausible threshold changes.
- **Hotspots are not area:** S-NPP / NOAA-20 duplicates are not merged; MODIS and NOAA-21 not used.

## Ethics
Decision support only: no contact, no fines, no automated enforcement. No personal data (no
owner names, phone numbers or land records); fields have synthetic IDs derived from geometry,
and the public dashboard shows labelled items as points without field outlines. Grid cells with
mismatches are prompts for field checks, not findings about villages. Secrets live in `.env`
(never committed).

## Future work
M4 U-Net fine-tuned from SSL4EO-S12 Sentinel-2 weights with spatially split tiles and calibrated
confidence; a label re-test (r3) and a second labeller; photo assessment after M4; evidence packs
per field; merged VIIRS duplicates, MODIS and NOAA-21.
