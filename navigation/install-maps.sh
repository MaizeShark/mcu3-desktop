#!/bin/bash
# Install tiles built by build-tiles.sh into a rootfs (the mounted image, or sandbox/rootfs).
#
#   sudo navigation/install-maps.sh <rootfs> [region]      region: 2 letters, default US
#
# Puts the tiles where QtCar and /etc/sv/valhalla/run look for them (/opt/navigon/tm/<dir>/valhalla,
# <dir> = NA for the US region), writes the version files QtCar and QtCarTMServer read, and sets
# up the car's settings.conf:
#   nav/forcePremiumNav=true   QtCar only checks for installed maps with "premium navigation" or a
#                              valid map region from the car's config over CAN, which we don't have
#   nav/forceMapRegion=<reg>   only for non-US regions; US is the default (region 0) and "US" isn't
#                              a name QtCar accepts (it becomes "none" and disables navigation)
# It also clears a stored FEATURE_navigationEnabled=false from QtCar's settings database (left
# behind by a wrong region), which QtCar would otherwise keep.
set -euo pipefail
ROOT=$(realpath "${1:?usage: $0 <rootfs> [region]}")   # before cd, so relative paths work
cd "$(dirname "$(realpath "$0")")"

REGION=${2:-US}
TILES=$PWD/work/tiles-$REGION
[ -d "$TILES/valhalla" ] || { echo "no tiles in $TILES/valhalla, run build-tiles.sh first"; exit 1; }
[ -f "$ROOT/usr/tesla/UI/bin/QtCar" ] || { echo "$ROOT doesn't look like the MCU rootfs"; exit 1; }

NAV=$ROOT/opt/navigon
case $REGION in
    US) MAPDIR=NA ;;            # QtCar: "Map directory for region does not exist: /opt/navigon/tm/NA"
    *)  MAPDIR=$REGION ;;
esac
DEST=$NAV/tm/$MAPDIR
# only one region at a time (valhalla takes the first two-letter folder)
for d in "$NAV"/tm/??; do if [ -d "$d" ] && [ "$d" != "$DEST" ]; then rm -rf "$d"; fi; done
VERSION="OSM-$REGION-$(date -r "$TILES/valhalla" +%Y%m%d)"
echo "Installing $VERSION to $DEST"
mkdir -p "$DEST"
rm -rf "$DEST/valhalla"
cp -r "$TILES/valhalla" "$DEST/valhalla"
[ -f "$TILES/admin.sqlite" ] && cp "$TILES/admin.sqlite" "$DEST/"
echo "$VERSION" > "$NAV/VERSION"
echo "$VERSION" > "$NAV/FILESYNC.VERSION"
chmod -R a+rX "$NAV"

# region override, keeping everything else in settings.conf
CONF=$ROOT/home/tesla/.Tesla/car/settings.conf
mkdir -p "$(dirname "$CONF")"
touch "$CONF"
python3 - "$CONF" "$REGION" "$ROOT/home/tesla/.Tesla/data/QtCarSettings.db" <<'EOF'
import configparser, os, sqlite3, sys
path, region, db = sys.argv[1:]
c = configparser.ConfigParser(interpolation=None)
c.optionxform = str  # keep key case
c.read(path)
if not c.has_section('nav'):
    c.add_section('nav')
c['nav']['forcePremiumNav'] = 'true'
if region == 'US':
    c.remove_option('nav', 'forceMapRegion')
else:
    c['nav']['forceMapRegion'] = region
with open(path, 'w') as f:
    c.write(f, space_around_delimiters=False)
if os.path.exists(db):
    con = sqlite3.connect(db)
    if con.execute("select 1 from sqlite_master where name='data'").fetchone():
        n = con.execute("delete from data where key='DataValues/FEATURE_navigationEnabled'").rowcount
        con.commit()
        if n:
            print("Reset stored FEATURE_navigationEnabled")
EOF
[ "$(id -u)" = 0 ] && chown 1111:1111 "$CONF"
echo "Done. ./start_all.sh now starts valhalla + qtcar-tmserver automatically."
