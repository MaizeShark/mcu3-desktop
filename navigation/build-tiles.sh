#!/bin/bash
# Build Valhalla 2.4.3 routing tiles from an OpenStreetMap extract, for Tesla's valhalla_server.
#
#   navigation/build-tiles.sh <extract.osm.pbf> [region]      region: 2 letters, default US
#
# Needs the valhalla:2.4.3 image: podman build -t valhalla:2.4.3 -f navigation/Containerfile navigation
# Result: navigation/work/tiles-<region>/valhalla/ (tiles) + admin.sqlite. Install into the rootfs
# with navigation/install-maps.sh.
set -euo pipefail
PBF=$(realpath "${1:?usage: $0 <extract.osm.pbf> [region]}")   # before cd, so relative paths work
cd "$(dirname "$(realpath "$0")")"

REGION=${2:-US}
OUT=$PWD/work/tiles-$REGION
mkdir -p "$OUT"

podman run --rm -v "$OUT":/out -v "$PBF":/input.osm.pbf:ro valhalla:2.4.3 bash -c '
    set -e
    valhalla_build_config --mjolnir-tile-dir /out/valhalla \
        --mjolnir-admin /out/admin.sqlite --mjolnir-timezone /out/tz_world.sqlite > /out/build.json
    echo "== admins (country/state borders, driving side)"
    valhalla_build_admins -c /out/build.json /input.osm.pbf
    echo "== tiles"
    valhalla_build_tiles -c /out/build.json /input.osm.pbf
' 2>&1 | tee "$OUT/build.log"

echo "Tiles: $OUT/valhalla ($(du -sh "$OUT/valhalla" | cut -f1))"
