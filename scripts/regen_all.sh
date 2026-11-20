#!/usr/bin/env bash
# Regenerate every pipeline output from scratch (or from cache), in dependency order.
#
#   bash scripts/regen_all.sh                 # uses ./config.yaml
#   STUBBLE=".venv/Scripts/stubble" bash scripts/regen_all.sh   # Windows venv (Git Bash)
#
# Needs: the project installed in a Python 3.11 venv (pip install -e .), internet access for
# Planetary Computer / OSM / FIRMS, and FIRMS_MAP_KEY in .env for firms-fetch. Downloads are
# cached under data/raw/ and never repeated; each step skips outputs whose provenance matches.
#
# Not included (they need a person): the M3 labelling sample and tool (sample-m3, label,
# review-select, review-report, label-export). Their outputs in data/labels/ are read by
# deploy-data, which runs at the end if those files exist.
set -euo pipefail

STUBBLE="${STUBBLE:-stubble}"
CONFIG="${CONFIG:-config.yaml}"
run() { echo; echo "==> stubble $*"; "$STUBBLE" "$@" --config "$CONFIG"; }

cd "$(dirname "$0")/.."

# M1: data pipeline
run aoi
run download                # prerequisite of composite: caches every S2/S1 acquisition
run composite
run quicklook

# M2: baseline (dNBR > 0.27), kept as the reference that measured harvest
run detect
run polygons
run report-m2

# M2.1: harvest-aware detection
run download-ancillary      # prerequisite: S2 AOT band + ESA WorldCover
run radiometry              # prerequisite: haze loss, PIF offsets, scene exclusions
run detect-harvest
run polygons-harvest
run report-m21

# M5-lite: FIRMS VIIRS comparison
run firms-fetch
run m5lite

# Dashboard data package (deploy/data/, <= 50 MB); needs the M3 label exports
if [ -f data/labels/m3_labels.geojson ] && [ -f data/labels/m3_review_r2_report.json ]; then
  run deploy-data
else
  echo "skipping deploy-data: M3 label exports not found in data/labels/"
fi
echo; echo "done"
