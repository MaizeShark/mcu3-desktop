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
  Touches: QtCar replays them on its own uinput touchscreen (`tesla-uinput`) and the X server
  delivers them to the browser's (invisible) window, as on the car. With `--native` the preloaded
  `egl_pixmap_shim.so` keeps that window away from the desktop's window manager (on top, opacity
  0, no focus). Over VNC, Xvfb doesn't read that touchscreen: the shim sends taps as clicks and
  drags as wheel steps instead, and the udev rule keeps the desktop from taking it (else the
  desktop's pointer jumped with every touch in the browser).
- Limits: an old browser; pages that need a newer one fail. Pages that create a WebAudio
  oscillator lose that feature. Over VNC there are no multi-touch gestures (pinch zoom).

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

## Arcade

Always on (launcher -> Arcade): Beach Buggy Racing 2 (Tesla's own port, "cobalt") and the MAME
classics (Asteroids, Missile Command, Centipede, Lunar Lander, ...). 2048 is part of QtCar.

- How: QtCar asks the escalator to start the game (`sv up mame`, `start-tesla-game-cobalt`); the
  kit's `/sbin/sv` wrapper runs it through `qtcar-service` instead of runit, and stops it again on
  EXIT (`sv DOWN mame`, `sv force-stop cobalt`).
- **Beach Buggy Racing 2** draws its own window at QtCar's game area (0,60 1920x1140) and reads
  four evdev devices: `game-touch` (made by `input_to_virtual` from the touchscreen, mapped into
  the game window), `game-steering` and `game-scroll-left/-right` (made by `qtcar-vehicle` while
  the game runs, from the steering angle, the brake pedal and the scroll wheels). On the car you
  steer with the wheel and brake with the pedal; the car accelerates by itself. Here they come
  from CAN signals: needs `--vehicle`, and the panel's **Arcade controls** (slider, keys, a
  gamepad) set them. Touch works without it (on-screen arrows and pedals).
- **MAME** games: QtCar shows the game's picture itself and sends the keys with XTest to MAME's
  window, which stays below QtCar: COIN/START in the top bar, the rest with the scroll wheels
  (`GUI_steeringWheelControlsMode` TeslAtari), i.e. the panel's scroll wheel buttons.
- Native screen: the car has no window manager. `tools/game-windows.py` (started by `./tesla
  start`) takes the game windows back from the desktop's: BBR2 at QtCar's place and on top, MAME
  below QtCar with the keyboard focus. It also hides the mouse cursor while the run lasts (QtCar
  moves it with the steering wheel, for the trackball games).
- Speed: both render on the GPU with `--native` on Intel (the game users get `/dev/dri` too). In
  software (VNC, other GPUs) BBR2 needs 6+ cores and runs slowly; taps can get lost.
- Sound: through the media channel (`tplug-media`), like the media player; with `--audio`. The
  backup camera's ffmpeg is off while a game runs: game + camera made audiod fall behind (pump
  errors, audible dropouts). A few can still come while the game loads.
- Logs: in the image, `/run/qtcar-sv/cobalt.log`, `mame.log`; `/tmp/games/cobalt-input.log`.
  `game-windows.log` in the run folder.
- Limits: the MAME games aren't really usable on a desktop (the desktop's panel over QtCar's top
  bar, windows in odd places); Beach Buggy Racing 2 and 2048 are the ones to play. VNC mode
  (Xvfb) is untested with the games.

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
  Climate: power, fan speed, air distribution, recirculation and A/C are answered like the car's
  climate controller would; seat heaters and temperatures work in the UI (nothing heats).
  A tire below 2.2 bar (preset `tire-low`, the panel's sliders) shows the TPMS warning (with the
  drive rail on).
  While charging, tesla-can.py fills the battery at the set charge current (`BMS_packCurrent` into
  a 75 kWh pack at 360 V): energy/range added, time remaining, complete at 100%.
- Limits: the charger values on the charging screen (A, V) are the simulator's. Details: [`vehicle/README.md`](../vehicle/README.md).

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
- Kernels with a coarse timer tick (Debian: 250 Hz; Ubuntu uses 1000 Hz) made the loopback card
  run in 4 ms jumps and the audio stutter badly. Then `./tesla start` clocks the `model3` card
  from a dummy sound card on a high-resolution timer (`snd-dummy` "hrclock", snd-aloop's
  `timer_source`), automatically; the log says "the model3 card runs on a high-resolution clock".
- PulseAudio/PipeWire must not adopt the loopback cards: install `tools/99-tesla-sound.rules`
  (`./tesla check` warns). Without PipeWire, the host side plays through `pacat` or `aplay`.
- Limits: the stereo mix of the 8 amplifier channels is approximate. Streaming services don't
  work: TuneIn needs Tesla's media backend, Spotify's endpoint for this firmware is gone. Radio
  and Bluetooth have no hardware.

## Bluetooth

`--bluetooth` (`BLUETOOTH=1`): the car's own Bluetooth stack (Broadcom's BSA and Tesla's `btd`
from the firmware) runs on the PC's adapter. Pairing a phone, the phone app (contacts, call
lists), media control work; with `--audio` the phone's music plays through the car's audio.

- Needs: a Bluetooth adapter (`BT_ADAPTER=hci0`), python3. BlueZ (`bluetooth.service`) is
  stopped while it runs and gets the adapter back at `./tesla stop`.
- How: `bluetooth/hci-bridge.py` hands the adapter's raw HCI channel to the firmware's stack as
  if it were its serial Broadcom chip. Details: [`bluetooth/README.md`](../bluetooth/README.md).
- Music: select the Bluetooth source in the Media app ("Phone").
- Logs: `bsa_server.log`, `btd.log`, `qtcar-bluetooth.log`, `bt-bridge.log`, `a2dpbridge.log`.
- Limits: tested with an Intel AX210 and two Android phones. The music connection sometimes
  needs a second attempt (the car reconnects by itself). Calls are untested.

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
  (any format) into that device. It's off while an arcade game runs (together they overloaded the
  laptop: audible dropouts), like the car's dashcam in game mode.
- Logs: `camera.log` (ffmpeg, and when it was off for a game).
- Limits: the source must deliver frames; ffmpeg retries every 2 s until it does.
