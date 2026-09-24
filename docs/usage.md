# Usage

## Commands

| Command | What it does | sudo |
|---|---|---|
| `./tesla start [options]` | checks, then starts everything; stays in the foreground until Ctrl+C or `./tesla stop` | yes |
| `./tesla stop` | stops a running start from any terminal; cleans up what a killed run left (processes, mounts) | only if something is left |
| `./tesla status` | running or not; each service up/down with its restart count; the helpers; the image mount | no |
| `./tesla check` | host tools, kernel modules, the image and its kit version, maps, ports, the settings | no |
| `./tesla logs [RUN]` | summary of a run (default: the last one); `--list` lists the runs | no |
| `./tesla config` | the settings a start would use and where each comes from | no |
| `./tesla services` | the firmware services that can be added with `--services` | no |
| `./tesla build-image` | updates the image (kit + maps); `--check`, `--fresh`, `--maps REGION`, `--sandbox` | yes |
| `./tesla help` | the list of commands | no |

`./start_all.sh` is the old name of `./tesla start` and still works.

## Starting

```sh
./tesla start                                    # the UI with navigation (if maps are installed)
./tesla start --vehicle                          # + a parked car, web panel
./tesla start --vehicle --gps 37.4419,-122.1430  # + standing in Palo Alto
./tesla start --vehicle --gps "--pos 37.4419,-122.1430 --to 37.4275,-122.1697 --speedup 2"
./tesla start --audio --music ~/Music            # sound, a USB stick with music
./tesla start --camera /dev/video10              # backup camera (shown in R)
./tesla start --dry-run --audio                  # only check and show what would start
```

A start first runs the checks (only problems are shown; a failed check stops it, `--force` starts
anyway), then prints what runs and where:

```
==> Running  (stop: Ctrl+C here, or ./tesla stop)
    Screen    1920x1200, VNC localhost:5900 (viewer: krdc)
    Features  vehicle, audio, navigation
    Off       gps music camera  (./tesla start --help)
    Panel     http://localhost:8099/  (doors, charging, lights, services, logs)
    Services  valhalla qtcar-tmserver qtcar-sim qtcar-vehicle audioweaver audiod qtcar-audiod qtcar-mediaserver
    Logs      logs/20260924-154854/  (./tesla logs: summary)
```

QtCar restarts itself now and then to apply settings (e.g. after the first car configuration
arrives); `./tesla start` starts it again, like the car does. Services that exit are restarted too.
When it stops, a summary of the run's logs is printed.

## Options and settings

Every option has a setting of the same meaning in `tesla.conf`. The file is created from
`tesla.conf.example` on the first start, with every line commented out (= the default). Order:
defaults < `tesla.conf` < environment variables of the same name < command line options.
`--config FILE` uses another file.

| Option | Setting (default) | What |
|---|---|---|
| `--vehicle` / `--no-vehicle` | `VEHICLE=0` | fake vehicle data and the web panel |
| `--color C` | `VEHICLE_COLOR=` | RedMulticoat SolidBlack SilverMetallic MidnightSilver DeepBlue PearlWhite (implies `--vehicle`) |
| `--wheels W` | `VEHICLE_WHEELS=` | Pinwheel18 Stiletto19 Stiletto20 Stiletto20Staggered Gemini19Square Gemini19Staggered |
| `--performance P` | `VEHICLE_PERFORMANCE=` | Base Performance Ludicrous BasePlus |
| `--gps SPEC` / `--no-gps` | `GPS=` | `LAT,LON`, or `tesla-gps.py` options (see [features.md](features.md#gps)) |
| `--audio` / `--no-audio` | `AUDIO=0` | sound |
| | `AUDIO_REMIX=...` | which amplifier channels go left/right (`sox remix` syntax) |
| `--music DIR` / `--no-music` | `MUSIC=` | a folder as USB stick |
| `--camera DEV` / `--no-camera` | `CAMERA=` | backup camera source |
| | `CAMERA_DEV=/dev/video32`, `CAMERA_SIZE=1280x960` | the device QtCar reads, the picture size |
| `--nav` / `--no-nav` | `NAV=1` | offline navigation (when maps are installed) |
| `--services "A B"` | `SERVICES=` | more firmware services (`./tesla services`) |
| `--native` / `--vnc` | `SCREEN=vnc` | QtCar on this PC's own screen (see below) or on a virtual screen over VNC |
| `--touch-device DEV` | `TOUCH_DEVICE=auto`, `NATIVE_DISPLAY=:0` | native: the touchscreen and the X display |
| `--size WxH` | `SIZE=1920x1200` | the UI's size (the car's screen) |
| `--qtcar-args "..."` | `QTCAR_ARGS=` | more QtCar options |
| `--viewer CMD` | `VIEWER=` | VNC viewer; empty = the first of krdc, remote-viewer, remmina, vncviewer; `none` |
| `--remote` | `REMOTE=0` | VNC (with a password) and the panel reachable from the network |
| | `VNC_PORT=5900`, `PANEL_PORT=8099` | ports |
| `--no-restart` | `RESTART=1` | don't restart QtCar when it exits |
| | `IMAGE=./mcu3-new.ext4`, `CHROOT=./chroot` | the image and its mount point |
| `--verbose`, `-v` | | QtCar's output in the terminal too |
| `--strace "SVC ..."` | | run services under strace; traces end up in the run's log folder |
| `--dry-run`, `-n` | | only check and show what would start |
| `--force`, `-f` | | start although a check failed |

`./tesla config` prints the resulting values with their source. Unknown names in `tesla.conf`
are reported by the checks (typos).

## The screen

The UI runs on a virtual display (Xvfb `:1`) and is shown over VNC on `localhost:5900`.
`./tesla start` opens a viewer.

- **Touch:** left click / drag = one finger, mouse wheel = pinch zoom, right click = long press.
- **Viewers:** KRDC, `remote-viewer` and Remmina scale the picture to the window; TigerVNC
  `vncviewer` can't. Scaling doesn't affect touch accuracy.
- **From another machine:** `ssh -L 5901:localhost:5900 <this-pc>`, then connect to
  `localhost:5901` (macOS: Screen Sharing, `open vnc://localhost:5901`). Or `--remote` with a
  password set once by `x11vnc -storepasswd`.

## On the PC's own screen (native)

`./tesla start --native`, run from the desktop session (or with `NATIVE_DISPLAY`/`XAUTHORITY`
pointing at it): QtCar opens fullscreen on the real screen, no Xvfb, VNC or viewer.

- **Scaling:** the UI is 1920x1200 like the car's screen. On a panel with another size the X
  screen is set to 1920x1200 and scaled to fit (`xrandr --transform`), with black bars; the
  original setting comes back when it stops.
- **Touch:** the first touchscreen (a multitouch device marked as direct) is grabbed and passed
  through by `tesla-touch --from`, mapped to the letterboxed UI; real multitouch works (pinch).
  The desktop doesn't see those touches while it runs. Without a touchscreen, the mouse works as
  one finger, as over VNC.
- The screensaver is off while it runs. Stop with `./tesla stop` from another terminal (or
  Ctrl+C in the start terminal, Alt+Tab to reach it).
- Tested on a ThinkPad T480 (1920x1080 touch panel) with a Debian 13 live system: the whole
  thing ran from RAM, `./tesla build-image` on the laptop, no install needed besides the tools.

## The web panel

With `--vehicle`: **http://localhost:8099/**

- **What QtCar sees:** 22 live values (gear, speed, odometer, battery, doors, lights, telltales,
  tire pressures, ...), read from the vehicle service every 2 s.
- **Presets:** parked, drive, stop, charging, charge-complete, unplugged, doors-open/-closed,
  locked/unlocked, lights-on/-off, tires-ok, tire-low, winter, summer, rail-off.
- **Controls:** drive rail, gear, speed, battery; doors, frunk, trunk, lock; charging state,
  current, time to full; lights; turn signals, hazards, parking brake, seat belt; outside and
  inside temperature; tire pressures; any CAN signal by name.
- **GPS marks:** with `--gps`, gear, speed, drive rail and odometer follow the GPS and carry a
  GPS badge. A change there holds until the GPS car starts or stops moving.
- **Services:** every service and helper of the run with an up/down dot and its restart count,
  links to each log file and to the run summary.

## Stopping

Ctrl+C in the start terminal, or `./tesla stop` anywhere. Both stop QtCar, the services and the
helpers, and unmount what the start mounted (the image too, if the start mounted it). The summary
of the run follows. If a run was killed or its terminal closed, `./tesla stop` cleans up what it
left.
