#!/usr/bin/env bash
# Rebuilds frontend/maps/area.pmtiles and streets.pmtiles: the service area (200 km around LPU)
# cut from the latest daily Protomaps build of OpenStreetMap. Needs the pmtiles tool
# (https://github.com/protomaps/go-pmtiles/releases) on PATH or in $PMTILES.
#
# Two files because GitHub refuses files over 100 MB: area holds data zooms 0-13, streets
# holds zoom 14 (frontend/campus-map.js shows each at the right map zoom).
set -euo pipefail
cd "$(dirname "$0")/.."
PMTILES="${PMTILES:-pmtiles}"

BUILD=$(curl -fsS https://build-metadata.protomaps.dev/builds.json \
  | python -c "import json,sys; print(json.load(sys.stdin)[-1]['key'])")
SOURCE="https://build.protomaps.com/$BUILD"
echo "Using $SOURCE"

REGION=$(mktemp --suffix=.geojson)
python - "$REGION" <<'EOF'
import json, math, sys
sys.path.insert(0, "backend")
from pricing_and_queue import LPU_CENTER, MAX_SERVICE_RADIUS_KM
lat0, lng0 = LPU_CENTER
r = MAX_SERVICE_RADIUS_KM + 3  # a little margin so the edge of the area is fully drawn
ring = [[round(lng0 + r / (111.0 * math.cos(math.radians(lat0))) * math.sin(math.radians(a)), 5),
         round(lat0 + r / 111.0 * math.cos(math.radians(a)), 5)] for a in range(0, 360, 5)]
json.dump({"type": "Polygon", "coordinates": [ring + [ring[0]]]}, open(sys.argv[1], "w"))
EOF

mkdir -p frontend/maps
"$PMTILES" extract "$SOURCE" frontend/maps/area.pmtiles.tmp --region="$REGION" --maxzoom=13
"$PMTILES" extract "$SOURCE" frontend/maps/streets.pmtiles.tmp --region="$REGION" --minzoom=14 --maxzoom=14
mv frontend/maps/area.pmtiles.tmp frontend/maps/area.pmtiles
mv frontend/maps/streets.pmtiles.tmp frontend/maps/streets.pmtiles
rm -f "$REGION"
ls -l frontend/maps
