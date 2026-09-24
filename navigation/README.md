# navigation

Offline routing for QtCar, built from OpenStreetMap data.

How routing works in the car: QtCar asks `QtCarTMServer` (runit service `qtcar-tmserver`), which
asks `valhalla_server` over HTTP on `localhost:8002`. That's a Tesla-modified Valhalla 2.4.3
(service `valhalla`), which routes on tiles in `/opt/navigon/tm/<region>/valhalla`. Tesla ships
those tiles as a separate download, so the firmware dump has an empty `/opt/navigon`.
Address search and geocoding go to Google online and work without any of this.

Here the tiles are built with upstream Valhalla 2.4.3 from an OSM extract, and Tesla's modified
server routes on them. Tested 2026-09-23 with a Northern California extract: Tesla's
`valhalla_run_route` gives a full turn-by-turn route Palo Alto → San Francisco (37.9 mi, 46 min),
and in the sandbox QtCarTMServer finds the region (US), talks to valhalla_server and reports the
map version from `/opt/navigon/VERSION`.

```sh
# once: build the Valhalla 2.4.3 tools image (~30 min)
podman build -t valhalla:2.4.3 -f navigation/Containerfile navigation
# tiles from an extract (e.g. from download.geofabrik.de), region = 2 letters (US, EU, ...)
navigation/build-tiles.sh ~/Downloads/norcal-260922.osm.pbf US
# into the image: ./tesla build-image installs the newest tiles (--maps US picks a region;
# by hand, into the mounted image: sudo navigation/install-maps.sh chroot US)
./tesla build-image
./tesla start    # starts valhalla + qtcar-tmserver automatically once maps are installed
```

## What QtCar needs (found 2026-09-23)

- **Map folder**: `/opt/navigon/tm/NA/valhalla` for the US (QtCar: "Map directory for region does
  not exist: /opt/navigon/tm/NA"), plus `/opt/navigon/VERSION`.
- **`[nav] forcePremiumNav=true`** in `/home/tesla/.Tesla/car/settings.conf`. Without it QtCar
  only looks for maps once the car reports a valid map region over CAN (`VAPI_navigationMapRegion`),
  which never happens here, so it stays at "Navigation enabled, maps missing".
- **No `forceMapRegion` for the US.** Region values: 0 = US (the default), 1 = EU, 2 = none,
  3 = CN, 4 = AU, … `forceMapRegion=EU/CN/AU` work; "US" isn't a known name and becomes "none",
  which switches `FEATURE_navigationEnabled` off. QtCar stores that flag in
  `/home/tesla/.Tesla/data/QtCarSettings.db` and keeps it even after the setting is removed.
- **Services on local broadcast**: QtCar hears other services on 127.255.255.255. Started with
  `--gw`/`--ip` (as in the car) they use multicast 224.0.0.26 instead, and QtCar never learns that
  TMServer is up (`TM_startupTime`). `qtcar-service` strips those options.

`install-maps.sh` takes care of all of this. Result in the sandbox: "Navigation enabled, maps
found", "Nav map server availability changed from false to true", `isNavigationAvailable(): true`.
