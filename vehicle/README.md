# vehicle

Fake vehicle inputs for QtCar in the chroot, so the UI sees a car instead of "no car attached".
Everything here runs on the host: the chroot shares the host's network, and the firmware services
talk over UDP on loopback.

| File | What |
|---|---|
| `tesla-gps.py` | fake GPS: NMEA over UDP to `QtCarGpsManager` (fixed position, or driving along a route) |
| `tesla-can.py` | fake CAN signals to `QtCarVehicle`, by name (gear, speed, odometer, doors, ...) |
| `extract-can-db.py` | builds the signal database `tesla-can.py` needs, from the firmware itself |
| `can-sniff.py` | shows (and decodes) the CAN-over-UDP traffic |
| `qtcar-settings.py` | read/write the data values QtCar persists in `QtCarSettings.db` |

```sh
./tesla start --vehicle                                    # parked car: config, battery, P, odometer
./tesla start --vehicle --gps "--pos 37.4419,-122.1430 --to 37.4275,-122.1697 --speedup 2"   # drive
```

`--vehicle` (`VEHICLE=1` in tesla.conf) starts `QtCarSimService` (Tesla's vehicle simulator, `qtcar-service qtcar-sim`),
`qtcar-vehicle` and `tesla-can.py --preset parked`. **http://localhost:8099/** is a web page to
open doors, plug in the charger, switch lights, change temperatures and tire pressures, apply
presets and set any signal while it runs, next to the values QtCar actually sees; it also shows
the run's services (up/down, restarts) and links their logs (`tesla-can.py --log-dir`). It listens
on localhost only unless `--remote` (`tesla-can.py --http-bind`). With `--gps` as well,
`tesla-gps.py --can` puts the car in D, sets the speed and counts up the odometer while it
drives, and P at the end. Values it set are marked **GPS** on the panel: a change there holds
only until the GPS car starts or stops moving (tesla-can.py remembers the last setter, `source`
in the control message).
Logs: `logs/<time>/{qtcar-sim,qtcar-vehicle,can,gps}.log`. The image needs the current patch kit
(`qtcar-service` with the `qtcar-sim` job, kit group `vehicle`).

## How vehicle data reaches QtCar

On the car the gateway forwards CAN frames to the MCU as UDP: **one datagram per frame, 2 bytes
CAN id (big endian) + 8 data bytes**, broadcast to port 1234 (`--udp :1234`; `--udphp :31415`
exists too, nothing needed it so far). `QtCarVehicle` (service `qtcar-vehicle`) decodes them into
data values (`VAPI_*`, `BMS_*`, `DOOR_*`, ...) and publishes those to QtCar. It checks neither
counters nor checksums of the frames tried so far; `tesla-can.py` fills both in anyway
(checksum = low byte of id + high byte of id + all other data bytes, `VehicleUtils::calculateChecksum`).

Two ways to produce frames, compared (2026-09-23):

| | `QtCarSimService` (Tesla's simulator) | `tesla-can.py` (own sender) |
|---|---|---|
| what | acts as the gateway, sends a whole simulated Model 3 (~40 CAN ids; ~70 with the drive rail on) | sends exactly the signals you set, by name, from the firmware's own signal database |
| control | `SIM_*` data values (486), HTTP on port 4190 | command line / control port |
| covers | car config (`GTW_carConfig`, map region USCanada), battery/range, HVAC, doors, lights, rails | anything in the database (379 messages, 8759 signals) |
| missing | drive inverter: no gear, speed, odometer with the rail off; with the rail on its gear/speed don't follow `SIM_shiftState`/`SIM_vehicleSpeed` (speed −40 km/h) | everything you don't set is 0 |

So both are used: the sim for the base car, `tesla-can.py` for the drive inverter (gear, speed,
odometer). The kit's `vehicle` group switches off the sim's drive-inverter module
(`SimDriveInverter::send2Hz/send10Hz` → `ret`), so with the rail on the two don't fight over the
same ids.

**Merge mode** (what `./tesla start` does): the sim is started with `--udp :1235` (it takes the
same `--udp` option as the other QtCar services, `RuntimeEnv`), so its frames go to
`tesla-can.py --sim-port 1235` instead of QtCarVehicle. `tesla-can.py` forwards them to :1234
with the signals it has set written into them (checksum redone, the sim's counter kept). So any
signal can be set, also ones in the sim's own messages, e.g. `BMS_packCurrent` in 0x132 (the
sim's dynamics model keeps its own current at 0 when parked). Without `--sim-port` it listens on
:1234 and pauses any id that someone else is sending.

On the car nothing starts the simulator (no runit job). Its options: `--no-gps` (it pushes GPS
timestamps to GpsManager otherwise; harmless so far), `--no-carconfig`, `--van`.

### Signals seen working in QtCar (sandbox)

| Set | QtCar data value | Notes |
|---|---|---|
| `DI_odometer` (km, 0x3B6) | `VAPI_odometer` (miles) | ends "waiting for odometer" / "No valid odometer value" |
| `DI_gear` P/R/N/D (0x118) | `VAPI_shiftState` | |
| `DI_uiSpeedHighSpeed` (0x257) | `VAPI_vehicleSpeed`, `VAPI_displaySpeed` | `DI_uiSpeed` alone isn't used |
| `DI_vehicleSpeed` (km/h) | `VAPI_signedVehicleSpeed` | offset −40: 0 raw = −40 km/h |
| `SIM_driveRailOn=true` | `VAPI_driveRailOn` (from `VCFRONT_LVPowerState`, 0x221) | UI shows speed + PRND instead of only the gear |
| sim defaults | `VAPI_carType` Model3, `VAPI_navigationMapRegion` USCanada, `VAPI_batteryLevel` 100, range 325 mi, 72 °F | |

### What drives what (tested in the sandbox, 2026-09-23)

| Part of the car | Set | QtCar data values | Shows in the UI |
|---|---|---|---|
| doors | `SIM_driverDoor`, `SIM_passFrontDoor`, `SIM_driverRearDoor`, `SIM_passRearDoor` (bool) | `VAPI_doorState` (flags), `VAPI_driverDoor`, `DOOR_*Latch` | open doors on the car picture |
| frunk / trunk | `SIM_frontTrunk`, `SIM_rearTrunk` | `DOOR_frontTrunkLatch`, `DOOR_rearTrunkLatch`, `VAPI_doorState` | "OPENED" over frunk / trunk |
| lock | `SIM_vehicleLockState` (bool) | `VAPI_isLocked`, `VAPI_lockStatus` | lock icon in the status bar |
| charge port | `SIM_chargePortDoor`, `SIM_chargePortLatch` (SNA, Disengaged, Engaged, Blocking) | `VAPI_chargePortDoor`, `VAPI_chargePortLatch` | port on the car picture |
| charging | `SIM_chargeState` (Disconnected, NoPower, Starting, Charging, Complete, Stopped), `SIM_chargerProximity`, `SIM_isPilotGood` | `VAPI_isCharging`, `VAPI_pilotCurrent`, `VAPI_chargeCableType`, `VAPI_chargerPower` | charging screen, green bolt |
| time to full | `SIM_chargeTimeToFull` (h) | `VAPI_chargeTimeToFull` | "Time Remaining" |
| charge rate | `BMS_packCurrent` (A, **positive** = charging; needs merge mode) | `VAPI_batteryCurrent`, `VAPI_chargeAveragePower` -> `GUI_chargeAveragePower` | "51 mi/hr" (power / Wh per mile) |
| battery | `SIM_batteryLevel` (%) | `VAPI_batteryLevel`, `VAPI_ratedRange` | range, battery bar |
| lights | `SIM_headLights`, `SIM_parkingLights`, `SIM_highbeamSwitch`, `SIM_frontFogLights` | `VAPI_headLights`, `VAPI_highBeamLights`, `VAPI_frontFogLights`, `LIGHT_*` | headlight icon in the status bar |
| outside temp | `SIM_hvacOutsideTemp` (°C) | `HVAC_outsideTemp` | temperature in the status bar |
| tire pressures | `SIM_tpmsPressureFL/FR/RL/RR` (bar) | `TPMS_pressureFL/...` | |
| BRAKE / ESP telltales (rail on) | `IBST_iBoosterStatus` (0x39D), `DI_tcTelltaleOn` (0x2B6), both in `--preset parked` | `VAPI_telltaleBrakes`, `VAPI_telltaleESP` | amber BRAKE / ESP when missing |

**Arcade** (tested 2026-09-25 on the laptop with Beach Buggy Racing 2 and Asteroids): while a
game runs (`GUI_vehicleGameMode`), QtCarVehicle (`SteeringInputForwarder` in libQtCarVAPI) makes
the uinput devices `game-steering` and `game-scroll-left`/`-right` and feeds them:

| Set | QtCar data value | Game device |
|---|---|---|
| `SCCM_steeringAngle` (0x129, degrees, **right = positive**) | `VAPI_steeringAngle` | `game-steering` ABS_WHEEL = angle x 100; BBR2: full lock at 30° (`SteeringWheelExtent`), QtCar's top bar says "TOO MUCH RIGHT/LEFT" beyond |
| `VCLEFT_brakePressed` (0x3C2) | `VAPI_brakePedal` (source `ETH_VCLEFT_brakePressed`) | `game-steering` brake key |
| `VCLEFT_swcLeftTiltLeft` / `Pressed` / `TiltRight` (ON/OFF) | `STW_leftTop` / `Middle` / `Bottom` | `game-scroll-left` keys |
| `VCLEFT_swcLeftScrollTicks` (signed, ticks per frame: they add up while set) | `STW_leftScroll` | `game-scroll-left` wheel |
| the same for `swcRight*` | `STW_right*` | `game-scroll-right` |

Not `DI_brakePedalState`, `IBST_driverBrakeApply` or `ESP_driverBrakeApply` (tried). Both
messages are in the simulator's frames (merge mode writes the values in). In the MAME games
QtCar turns the `STW_*` values into keys (XTest) itself. The panel's "Arcade controls" set these
with `{"values": {...}, "quiet": true}` (no log line, no state in the reply).

**Buttons in the UI** (frunk/trunk "OPEN", lock icon, charge port, light switch, fog lights):
QtCarVehicle sends the requests as CAN frames to the gateway, UDP broadcast to **:4321** (header
`bus << 12 | CAN id`, e.g. `0x4273` = bus 4, `UI_vehicleControl` 0x273). The sim ignores them, so
`tesla-can.py --respond` (`./tesla start` sets it) answers like the car's ECUs would (`RESPONSES`):
`UI_frunkRequest` -> frunk opens, `UI_trunkRequest` -> trunk opens/closes, `UI_lockRequest` ->
lock, `UI_lightSwitch` -> head/parking lights, `UI_frontFogSwitch`, `UI_open/closeChargePortDoorRequest`,
the wipers, and the climate: `UI_hvacRequest` (0x2f3: power, fan speed, air distribution,
recirculation, A/C, second row) -> `VCRIGHT_hvacFeedback` (0x243), which the climate panel shows
(fan speed "3" when AUTO; before, the simulator's fixed "2" and "on" regardless of the UI).
The temperature setpoints and the seat heaters need no answer: the UI keeps them itself
(`GUI_hvacLeftTempRequest`, `GUI_seatHeaterRequest*`) and only sends requests.
More candidates in `UI_vehicleControl` (mirrors), `UI_vehicleControl2` (glovebox),
`UI_chargeRequest` (0x333):
`vehicle/can-sniff.py --ports 4321` shows them (the port-4321 header also has the bus: `0x4273`).

The BRAKE/ESP telltales (`ESPStatusMessage::processMessage`, 0x145, only with the drive rail on):
ESP is On while `DI_tcTelltaleOn` is on or invalid, or `ESP_espFaultLamp` is set; Flash with
`ESP_espLampFlash` or `DI_vdcTelltaleFlash`/`DI_tcTelltaleFlash`. BRAKE is Red with
`ESP_ebdFaultLamp`, low/invalid brake fluid (`VCFRONT_brakeFluidLevel`) or an EPB brake-type
mismatch, else Yellow unless `IBST_iBoosterStatus` is READY or ACTUATION. With the rail off all
three are Off. Since the sim's drive inverter is patched out, nobody sent 0x2B6 or 0x39D.

Not working yet: TPMS warnings. The soft/hard warning bits in `TPMS_StatusC` (0x36F) reach
`TPMSStatusMessage::processContiMessage` → `filterStatus`, but `TPMS_softWarnings` stays None
(it waits `TPMSDriveRailOnFilterTime` after the rail comes on and filters faults; not followed up).

### Paint, wheels, performance

Part of the car config the sim sends (`GTW_carConfig`): `SIM_exteriorColor` (RedMulticoat,
SolidBlack, SilverMetallic, MidnightSilver, DeepBlue, PearlWhite), `SIM_wheelType` (Pinwheel18,
Stiletto19, Stiletto20, Stiletto20Staggered, Gemini19Square, Gemini19Staggered),
`SIM_performancePackage` (Base, Performance, Ludicrous, BasePlus). QtCar keeps its own copy
(`VAPI_*` in its settings DB) and draws the car with it from the next start on. `./tesla start`
sets both: `./tesla start --color DeepBlue --wheels Gemini19Square` (`VEHICLE_COLOR`, ... in tesla.conf).
The sim stores its values, so they stay. The firmware only has Model 3 assets (chassis S/X
isn't something this UI draws).

## tesla-can.py

```sh
vehicle/tesla-can.py --find door                        # search signals: id, message, unit, enum values
vehicle/tesla-can.py --preset parked DI_odometer=50000  # run (Ctrl+C to stop)
vehicle/tesla-can.py --set DI_gear=D DI_uiSpeedHighSpeed=30 DI_vehicleSpeed=48   # change it while running
vehicle/tesla-can.py --set SIM_driveRailOn=true         # SIM_* names go to the simulator
vehicle/tesla-can.py --status
vehicle/tesla-can.py --set --preset charging --preset locked   # presets at runtime, combinable
vehicle/tesla-can.py --list-presets
vehicle/can-sniff.py --decode --id 0x118,0x257          # what's on the wire
```

Presets: `parked` (the base: drive inverter, iBooster, chassis control), `drive`, `stop`,
`rail-off`, `charging`, `charge-complete`, `unplugged`, `doors-open`, `doors-closed`, `locked`,
`unlocked`, `lights-on`, `lights-off`, `tires-ok`, `tire-low`, `winter`, `summer`.
`--http PORT` serves the web page (`vehicle/panel.html`, JSON API at `/api/state`: GET for the
state, POST `{"values": {...}}`, `{"preset": NAME}` or `{"unset": [...]}`).
The page's "CAN signals" section browses the whole database: `/api/db` (every message with its
signals, range, unit, enum, multiplexer branch `m`), `/api/can?ids=0x118,0x3b6` (every CAN id
seen: `[age s, frames/s, sender]`, sender `set` = this tool, `sim` = forwarded from the
simulator, `other` = another sender on the port, `qtcar` = QtCar's requests on :4321; plus for
the given ids the last frame per multiplexer branch and all decoded signals). With `--http` it
listens on :4321 even without `--respond` (only to show QtCar's frames).

Values are in the signal's unit or an enum label (`--find` lists both). A `*` after the message
name in `--find` means the simulator sends that message itself.

### The signal database (extract-can-db.py)

The firmware has no DBC file, but `GUICanCracker::crackMessage()` in `libQtCarVAPI.so` decodes
every message for the diagnostics CAN viewer. `extract-can-db.py` runs that function in an
emulator (Unicorn) on test frames (all zeros, then each bit alone, then each value of small
signals for multiplexed messages) and derives bit positions, scale, offset and sign from how the
decoded values change. Names, units, message ids, periods and enum labels come from the tables
in `libQtCarCANData.so`. Result: `vehicle/work/can-db.json` (untracked, it's derived from the
firmware), 379 messages / 8759 signals, all with a layout. Takes ~20 s:

```sh
pip install --user unicorn pyelftools
vehicle/extract-can-db.py chroot        # or sandbox/rootfs; ./tesla start --vehicle does it once if missing
```

## GPS

On the car the GPS receiver's NMEA stream reaches `QtCarGpsManager` (runit service
`qtcar-gpsmanager`) as UDP on port 63277 (`--udpgps :63277`, `LBGPS` in `/etc/RunQtCar.vars`).
It accepts any sender, so `tesla-gps.py` just sends NMEA there. GpsManager publishes the fix to
QtCar (UDP broadcast, port 4161).

```sh
./tesla start --gps 37.4419,-122.1430                                   # stand in Palo Alto
./tesla start --gps "--pos 37.4419,-122.1430 --heading 90"
./tesla start --gps "--pos 37.4419,-122.1430 --to 37.7793,-122.4193 --speedup 3"   # drive to SF
```

`./tesla start` then starts `qtcar-gpsmanager`, sets `GUI_smoothGPSUpdates=false` (see below) and
runs `tesla-gps.py` with those options (log: `logs/<time>/gps.log`). Standalone:

```sh
vehicle/tesla-gps.py --pos 37.4419,-122.1430                      # stand still
vehicle/tesla-gps.py --pos 37.4419,-122.1430 --to 37.4275,-122.1697 [--speed 50] [--speedup 4] [--loop]
vehicle/tesla-gps.py --gpx track.gpx
vehicle/tesla-gps.py --path "37.44,-122.14 37.45,-122.15"
vehicle/tesla-gps.py --uart /dev/ttyUSB0 --baud 9600         # real receiver, passed through
vehicle/tesla-gps.py ... --can            # with a running tesla-can.py: gear, speed, odometer follow
```

**A real GPS receiver** (USB/serial, e.g. u-blox): `--uart DEV [--baud N]` forwards its NMEA
unchanged, line by line (GpsManager also knows the `GN`/`GL` talker IDs of multi-GNSS receivers).
With `--can`, speed and position from its RMC sentences set gear/speed/odometer, so a real drive
shows up as driving: D above 1 m/s, P again below 0.5 m/s, so a receiver standing still (a few
tenths of a knot of noise, position wandering by metres) stays parked and adds no odometer. The
port is reopened if the receiver is unplugged. Your user needs access to the device (group
`dialout`). Every 30 s it logs what arrives and whether GpsManager has a fix (also with `-q`,
so it's in `logs/<time>/gps.log`):

```
224 sentences in 30s (GGA:30 GLL:30 GSA:30 GSV:60 RMC:30 VTG:30)  RMC status A  sats 08  HDOP 1.10  GpsManager LOC_geoValidFix=true
```

"GpsManager not answering on :4160" means no GpsManager, or an old one still holds the port: once
a `QtCarGpsManager` survived the start script's SIGTERM and kept ports 4160 and 63277 for hours, and
every later run's GpsManager gave up ("Failed to listen on port 4160" in
`qtcar-gpsmanager.log`), so GPS only reached QtCar through the hung one, if at all.
`./tesla start`/`stop` now kill what's left in the chroot after 3 s, stop leftovers of earlier
runs at startup and refuse to start while the ports are taken. The sandbox (`sandbox.sh`) shares
the host network, so don't run both at once.

```sh
./tesla start --gps "--uart /dev/ttyUSB0 --baud 9600"
./tesla start --vehicle --gps "--uart /dev/ttyACM0 --baud 115200"
```

`--to` routes with the firmware's own `valhalla_client` on the installed map tiles (runs on the
host with the firmware's loader, like `valhalla_run_route`), so it only works inside the map
region. Speeds come from the route (length/time per maneuver) unless `--speed` is given.

### What GpsManager and QtCar need (found 2026-09-23)

- **One epoch = GGA, GSA, GSV..., VTG, RMC.** GpsManager publishes a fix on RMC, but only once it
  has seen a GGA *and* a complete GSV set (satellites in view) since the last one. With only
  RMC + GGA it parses everything but never sets `LOC_geoValidFix`.
- **Raw GPS vs. estimate.** GpsManager's estimator (`GPSLocationEstimator`) only takes a GPS
  position while the car reports a speed (`VAPI_vehicleSpeed`); parked, it keeps dead-reckoning
  from the last position (Tesla HQ in a fresh setup). QtCar draws the car at the estimate whenever
  `LOC_estimatedGPSValid` is true, whatever the location source says.
  - `GUI_smoothGPSUpdates=false` (persisted in QtCar's DB) makes QtCar ask GpsManager to stop
    estimating ("Location source is now raw GPS").
  - But the estimator sets `LOC_estimatedGPSValid=true` every time GpsManager starts and only
    clears it when `LOC_enableSmoothGPSUpdates` *changes*, which doesn't happen when that is
    already stored as false. QtCar also keeps its own persisted copy. So `tesla-gps.py` sets the
    flag to false in GpsManager (port 4160) and QtCar (port 4220) every 2 s (`--estimate` to
    leave it alone).
- Debug: QtCar-framework services answer
  `http://127.0.0.1:<port>/_data_get_value_request_?name=<data value>` and
  `_data_set_value_request_?name=...&value=...` (`curl --http1.0`; the server keeps the
  connection open). Ports: QtCar 4220, GpsManager 4160, VehicleService (qtcar-vehicle) 4030,
  simulator 4190. Useful values: `LOC_geoLocation`, `LOC_estimatedLocation`,
  `LOC_estimatedGPSValid`, `VAPI_shiftState`, `VAPI_odometer`. `[gps] verbose_log=true` in
  `settings.conf` makes the estimator log its decisions.

## qtcar-settings.py

QtCar persists `GUI_*`, `FEATURE_*`, `VAPI_*` (car config) and some `LOC_*` data values in
`/home/tesla/.Tesla/data/QtCarSettings.db` (QVariant blobs). Only edit it while QtCar is stopped.

```sh
sudo vehicle/qtcar-settings.py chroot/home/tesla/.Tesla/data/QtCarSettings.db              # list
sudo vehicle/qtcar-settings.py chroot/home/tesla/.Tesla/data/QtCarSettings.db GUI_smoothGPSUpdates=false
sudo vehicle/qtcar-settings.py chroot/home/tesla/.Tesla/data/QtCarSettings.db -d GUI_smoothGPSUpdates
```

Once the simulator has sent a car config, QtCar and QtCarVehicle store it (`VAPI_*`) and restart
themselves ("CID UI is restarting reason: DVs changed: VAPI_..."); `./tesla start` restarts both.
