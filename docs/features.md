# Features

Each section: what it does, how to switch it on, what it needs, how it works, where to look when
it doesn't, and its limits. The overview table is in [README.md](README.md#what-needs-what).

## The UI and touch

Always on. QtCar, the firmware's UI, runs in the chroot on a virtual X display with a software
OpenGL renderer (Mesa 18 with LLVM 6, from the patch kit). All apps and settings open, the car is drawn
in 3D, the map shows online tiles.

- Needs: the patched image, Xvfb, x11vnc, `tesla-touch`, a VNC viewer.
- How: `tesla-touch` creates a virtual multitouch device (`/dev/uinput`) and bind-mounts its
  event node as `/dev/input/touch` into the chroot, where QtCar's own touch driver reads it. It
  follows the X pointer of the virtual display, so VNC clicks become touches. Details:
  [`tesla-touch/README.md`](../tesla-touch/README.md).
- Logs: `qtcar.log` (QtCar; many ERROR lines are normal: services and hardware that aren't
  there), `touch.err`.
- Limits: the "touchscreen unresponsive" alert can show without touch. The UI assumes a car that
  answers; without `--vehicle` it shows "no car" states.

**Native screen** (`--native`): the same on the PC's own screen, scaled to fit with black bars,
with a real touchscreen passed through (see [usage.md](usage.md#on-the-pcs-own-screen-native)).

## Browser

Always on: the Web app is Tesla's Chromium Embedded Framework build (Chrome 73).

- How: the kit preloads `cef_nosandbox.so` (the Chromium sandbox can't work in the chroot),
  installs the missing V8 snapshot files and patches WebAudio's `createOscillator()` to return
  null (it crashed the renderer: Tesla's build has no FFT). DevTools: `curl http://127.0.0.1:9222/json`.
- Limits: an old browser; pages that need a newer one fail. Pages that create a WebAudio
  oscillator lose that feature.

## Videos: fireplace, dog mode, HAL 9000

Always on (Toybox and the climate screen). QtCar asks its privileged helper (the escalator) to
start them with runit's `sv`; the kit's `/sbin/sv` wrapper starts `/usr/bin/tvideo` on QtCar's
display instead. Sound with `--audio`.

## Offline navigation

On when maps are installed (`--no-nav` switches it off).

- Needs: map tiles built with [`navigation/`](../navigation/README.md) (podman, an OSM extract),
  installed by `./tesla build-image`. One region at a time.
- How: QtCar asks `QtCarTMServer` (service `qtcar-tmserver`) for routes, which asks Tesla's
  modified Valhalla 2.4.3 (`valhalla`, HTTP on :8002) on the tiles in
  `/opt/navigon/tm/<region>/valhalla`. The kit and `install-maps.sh` set the settings QtCar
  needs (`[nav] forcePremiumNav`, the map region).
- Logs: `valhalla.log`, `qtcar-tmserver.log`.
- Limits: **online routing doesn't work** (it needs the car's credentials / key for Tesla's
  servers), so only places inside the installed region can be routed to. Address search and
  geocoding go to Google online and work. Traffic, superchargers availability and similar online
  data don't load.

## Vehicle data and the web panel

`--vehicle` (`VEHICLE=1`). The UI sees a car: configuration (Model 3, color, wheels, map
region), battery and range, gear and speed, doors, trunk and frunk, locks, charge port and
charging, lights, outside temperature, tire pressures, telltales.

- Needs: python3; the CAN signal database, which `./tesla start` extracts from the firmware once
  (`vehicle/work/can-db.json`, needs `pip install --user unicorn pyelftools`).
- How: Tesla's own vehicle simulator (`QtCarSimService`, service `qtcar-sim`) plays the gateway
  and sends a simulated car's CAN frames as UDP. `vehicle/tesla-can.py` sits in between (merge
  mode): it writes its own signals into the simulator's frames (gear, speed, odometer, charge
  current, telltales, anything set on the panel) and forwards them to `qtcar-vehicle`, which turns
  them into the values QtCar shows. It also answers the UI's buttons: frunk, trunk, lock, charge
  port, lights, fog lights, wipers.
- Panel: http://localhost:8099/ (see [usage.md](usage.md#the-web-panel)).
- The car's look: `--color`, `--wheels`, `--performance`. QtCar reads it at startup.
- Logs: `qtcar-sim.log`, `qtcar-vehicle.log`, `can.log` (what tesla-can.py sends and answers).
- Limits: seat heaters and climate requests aren't answered; TPMS warnings don't trigger; the
  charging screen shows "+0 mi" added. Details: [`vehicle/README.md`](../vehicle/README.md).

## GPS

`--gps SPEC` (`GPS=`):

| SPEC | |
|---|---|
| `37.4419,-122.1430` | stand at a fixed position |
| `"--pos 37.4419,-122.1430 --heading 90"` | ... facing east |
| `"--pos A --to B [--speedup 3] [--loop]"` | drive a route from A to B (routed on the installed maps) |
| `"--gpx track.gpx"`, `"--path 'A B C'"` | drive a GPX track, or straight lines |
| `"--uart /dev/ttyACM0 --baud 9600"` | pass a real GPS receiver's NMEA through |

- Needs: `qtcar-gpsmanager` (started automatically); routes: maps; a receiver: read access to the
  device (group `dialout`).
- How: `vehicle/tesla-gps.py` sends NMEA sentences over UDP to GpsManager (port 63277), which
  publishes the position to QtCar. With `--vehicle`, driving also sets gear D, speed and odometer
  (a real receiver only counts as moving above 1 m/s). Without `--vehicle` the map uses the raw
  GPS position (`GUI_smoothGPSUpdates=false`).
- Logs: `gps.log` (every 30 s with a receiver: sentences, fix status, satellites, whether
  GpsManager has a fix), `qtcar-gpsmanager.log`. `./tesla logs` summarizes it.
- Limits: a receiver indoors often has no fix (RMC status V).

## Sound

`--audio` (`AUDIO=1`): chimes, turn signal ticks, media, the fireplace, the volume control.

- Needs: the `snd-aloop` kernel module (loaded as card `model3` at start), `arecord`, `sox`,
  PipeWire's `pw-play`.
- How: the car's sound card is replaced by a loopback card. The apps write into shared-memory
  channels, AudioWeaver (the car's DSP software) mixes them, audiod plays the result on the
  loopback card as the 8 channels of the car's amplifier. On the host, `arecord | sox | pw-play`
  records the other end, mixes the 8 channels to stereo (`AUDIO_REMIX`) and plays it.
- Logs: `audio.log` (the host side: clipping, restarts), `audiod.log`, `audioweaver.log`,
  `qtcar-audiod.log`. `./tesla logs` counts pump errors and clipped samples.
- Limits: the stereo mix of the 8 amplifier channels is approximate. Streaming services don't
  work: TuneIn needs Tesla's media backend, Spotify's endpoint for this firmware is gone. Radio
  and Bluetooth have no hardware.

## USB music

`--music DIR` (`MUSIC=`): a folder shows up as a USB stick (Media -> USB) and plays with
`--audio`.

- How: the folder is bind-mounted read-only at `/home/tesla/media/usb-music` in the chroot, where
  the kit points QtCar's USB search (`[usb] media_path`). QtCar indexes it like a stick.
- Logs: `qtcar.log` (USBAccess, USBDeviceIndexer), `qtcar-mediaserver.log` (playback).

## Backup camera

`--camera DEV` (`CAMERA=`): the picture of any V4L2 camera is shown in R (shift into R on the
panel), e.g. a phone through `scrcpy --video-source=camera --v4l2-sink=/dev/video10 --no-video-playback`.

- Needs: `ffmpeg`, `v4l2loopback` with a second device for QtCar (`/dev/video32`, `CAMERA_DEV`):
  `sudo modprobe v4l2loopback devices=2 video_nr=10,32 exclusive_caps=1,1`.
- How: QtCar reads 32-bit BGRX frames from `[bkcam] deviceId` (set at start, with
  `forceFeedGood`, since no camera module reports the feed as good). ffmpeg converts the source
  (any format) into that device.
- Logs: `camera.log` (ffmpeg).
- Limits: the source must deliver frames; ffmpeg retries every 2 s until it does.
