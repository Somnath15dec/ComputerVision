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

## Data sources (M1)
| Data | Source | Licence |
|---|---|---|
| Sangrur boundary (post-2021, excludes Malerkotla) | OpenStreetMap relation 9513553 via Nominatim | ODbL 1.0, (c) OpenStreetMap contributors |
| Sentinel-2 L2A | Microsoft Planetary Computer `sentinel-2-l2a` | Copernicus open licence |
| Sentinel-1 RTC (VV, VH) | Microsoft Planetary Computer `sentinel-1-rtc` | Copernicus open licence, RTC processing by Catalyst/PC |
| Sentinel-2 L2A AOT (Sen2Cor aerosol optical thickness) | Microsoft Planetary Computer `sentinel-2-l2a`, asset `AOT` | Copernicus open licence |
| ESA WorldCover 10 m 2021 v200 | Microsoft Planetary Computer `esa-worldcover` | CC BY 4.0, (c) ESA WorldCover project 2021 |

Every file under `data/raw` and `data/processed` has a `*.provenance.json` sidecar listing scene
IDs, dates, parameters and code version.

## Dashboard (deployment)
`app/dashboard.py` is a Streamlit app that reads only `deploy/data/` (about 13 MB, rebuilt with
`stubble deploy-data` after `stubble m5lite`). It shows the M2.1 candidate burnt area
(unvalidated estimate) coloured by the week of its first clear burnt view, VIIRS hotspots, the
labelled sample, weekly and per-cell comparisons, and a Method & limitations page, under a
research-prototype disclaimer. Dependencies are pinned in `requirements-app.txt` (copied to
`app/requirements.txt`, which Streamlit Community Cloud picks up next to the entry point).

```bash
pip install -r requirements-app.txt
streamlit run app/dashboard.py
```
