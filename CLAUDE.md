# CLAUDE.md

Runs the Tesla Model 3 MCU UI (QtCar, firmware 2019.20.4.2, x86_64) in a chroot on a Linux PC,
shown over VNC. Read `README.md`, `docs/` (the user documentation: setup, usage, features,
how it works, troubleshooting, development) and `notes/README.md` (index of the session notes:
how everything here came about) first. Component details are in each folder's README. Keep
`docs/` up to date when behavior changes. Don't call the mount point anything but `chroot/`.

State (end of session 6, 2026-09-24): QtCar, touch, all firmware services, offline navigation,
fake GPS/vehicle data with a web panel, the browser, sound (AudioWeaver + audiod on snd-aloop),
USB music from a folder, the backup camera (v4l2loopback) and the fireplace/dog mode/HAL videos
all work in the real chroot. Session 6 replaced `start_all.sh` by the `./tesla` command
(start/stop/status/check/logs/config/build-image, `tesla.conf`). Session 7 (2026-09-25): every CAN
signal on the web panel, and the arcade (Beach Buggy Racing 2, MAME) playable, tested with
`--native` on the laptop. Details: `notes/session6-*.md`, `notes/session7-*.md`.

## Layout
- `mcu3-original/`: pristine firmware dump, root-owned. Never modify it and never commit it.
- `mcu3-new.ext4`, mounted at `chroot/`: the image `./tesla start`
  runs (`IMAGE`/`CHROOT` in tesla.conf). `./tesla` refuses to mount it a second time elsewhere.
- `mcu3-patchkit/mcu3_patch.py`: turns a rootfs into the working state. Idempotent, `--check`, `--list`,
  `-q`. Writes a version stamp (`/etc/mcu3-patchkit`) that `./tesla check` compares.
  `./tesla build-image` (kit + maps) wraps it; `--sandbox` updates `sandbox/rootfs` without sudo.
  - `src/qtcar-service.sh` starts the firmware's `qtcar-*` services and `valhalla` inside the chroot.
- `tesla-touch/`: Rust mouse→multitouch bridge (`cargo build --release`).
- `navigation/`: Valhalla 2.4.3 podman image, `build-tiles.sh`, `install-maps.sh`.
- `vehicle/`: fake GPS (`tesla-gps.py`) and vehicle data (`tesla-can.py`, CAN signal DB from the
  firmware via `extract-can-db.py` into `vehicle/work/`, untracked), web panel (`panel.html`).
- `tesla` + `tools/`: the user-facing command. `tools/common.sh` (config loading, teardown),
  `game-windows.py` (takes the arcade's windows from a desktop window manager),
  `check.sh` (preflight checks), `start.sh` (what `start_all.sh` did), `logsummary.py` (also
  `service_status()` for `./tesla status` and the panel), `build-image.sh`. Settings:
  `tesla.conf` (untracked, from `tesla.conf.example`) < environment < options.
  `./tesla start --dry-run` and `./tesla check` run without sudo. `start_all.sh` = alias.
- Logs go to `logs/<time>/` (`logs/latest`, `logs/current` while running; `events.log`, `config`,
  `pids`, `state`), the first thing to read when the user reports a problem: `./tesla logs RUN`
  summarizes it (restarts, `journalctl -k` segfaults, audio, GPS; the user is in group adm).
- `sandbox.sh`: QtCar + services in the sudo-free podman sandbox (see below).
- `sandbox/rootfs`: a user-owned copy of the rootfs with the kit and maps applied, for testing
  without sudo (see below). Untracked.

## Git
- `.gitignore` is a **whitelist** (`/*` then `!/...`). A new top-level file or folder has to be
  added there to be tracked. Firmware, images, `logs/`, `sandbox/` and `navigation/work/` stay out.
- Remotes: `origin` is the maintainer's own git server (frequent pushes are fine), `github` is
  the public repository https://github.com/MaizeShark/mcu3-desktop (push only when asked: major
  updates). Both share one history.
- Public repository: never commit firmware files, binaries (`mcu3-patchkit/payload/` is
  downloaded/built by the kit), keys or credentials found in the firmware, or private details
  (addresses, local network). Check before a push to `github`.
- End commit messages with the Co-Authored-By line.

## Testing without sudo
You can't sudo; the user runs sudo commands. Unprivileged user namespaces and bwrap are blocked by
AppArmor, but rootless podman works with the user-owned copy:

```sh
podman run --rm --timeout 20 --network host -e QCSVC_NOUSER=1 --rootfs $PWD/sandbox/rootfs \
    /usr/local/bin/qtcar-service qtcar-tmserver
```

- QtCar itself: `./sandbox.sh [-t SECONDS] [-s "SHOT_TIMES"] [--touch] [SERVICE...]` runs QtCar
  (under the host's gdb, browser creation skipped, since without escalator CEF kills it after
  ~20 s) plus services on Xvfb :97; logs and screenshots in `sandbox/out/`. `--touch` runs
  tesla-touch on :97 without root (the user has an ACL on /dev/uinput); tap with
  `DISPLAY=:97 xdotool mousemove X Y click 1`.
  In the sandbox QtCar reads `/root/.Tesla/...` instead of `/home/tesla/...`. `--browser` keeps
  the browser (a sandbox-only stub fakes escalator and cgroups; CEF child crashes print a
  backtrace to qtcar.log), `-x FILE` adds gdb commands. QtCar remembers whether the Web app was
  open, so check the log for "select app: web" before tapping it (launcher 480,1140; Web 842,980).
- Data values of running services: `curl --http1.0 "http://127.0.0.1:4220/_data_get_value_request_?name=VAPI_odometer"`
  (QtCar 4220, GpsManager 4160, qtcar-vehicle 4030, simulator 4190; `_data_set_value_request_?name=..&value=..`).
- CEF in the sandbox: `CEF_REMOTE_DEBUGGING_PORT=9222 ./sandbox.sh --browser ...`, then
  `curl http://127.0.0.1:9222/json` shows the loaded pages. `CEF_EXTRA_ARGS` adds Chromium switches.
  The real chroot has DevTools on 9222 too (startup.sh).
- The host's gdb works inside the container (`sandbox.sh` does it: host `/` mounted at `/host`,
  run with the host's loader). Attaching from outside (`podman unshare gdb -p`) is blocked.
- Crashes in processes you can't run under gdb (CEF children): the sandbox stub's SIGSEGV handler
  prints `backtrace()` + libcef's load base. Kernel log `libX.so[OFFSET,...]`: OFFSET is the file
  offset (= VA for these libraries). libcef is stripped; identify functions by the strings they
  reference (the annotated-objdump approach in notes/session4) and by their callers.
- The firmware has `strace` (use it inside the container). Host tools such as
  `valhalla_run_route` also run directly:
  `mcu3-original/lib64/ld-linux-x86-64.so.2 --library-path usr/proto2/lib:usr/lib:lib <bin>`.
- For disassembly, `objdump -d -C` on the libraries in `usr/tesla/UI/lib` works well
  (`libQtCarGUI`, `libQtCarUIFramework`).
- After changing files in `sandbox/rootfs`, podman sometimes sees the old state for a moment.
  Re-run before drawing conclusions.

## Gotchas already paid for
- Don't run the chroot's programs with the host's `LANG`/`LC_*` (they come through sudo): C++
  locale errors. Use `env -i` or `LC_ALL=C`.
- Services need loopback broadcast: no `--gw`/`--ip` options, as `qtcar-service` does it.
- QtCar stores feature flags in `/home/tesla/.Tesla/data/QtCarSettings.db` (table `data`, keys
  `DataValues/FEATURE_*`), and they survive config changes.
- `settings.conf` gets merged, never overwritten: the kit and `install-maps.sh` both write keys to it.
- QtCar persists `VAPI_*` (car config), `LOC_estimatedGPSValid` and more in that DB too; stale
  values there explain many "works once, not again" effects. `vehicle/qtcar-settings.py` edits it.
- `Logger::logAssert` crashes on purpose outside "prod" mode (kernel log `libQtCarUtils[b4703]`);
  the kit's `utils-assert-nonfatal` makes it log only.
- QtCar's executable has its own copies of some libQtCarGUI functions (e.g.
  `ChromiumManager::initializeCef`), and those win. Check a backtrace before patching the library.
- Preload shims run against glibc 2.22: pin `dlsym` to `GLIBC_2.2.5` (on 2.22 it's in libdl, which
  not every process loads; only lazy binding keeps unused hooks from failing), and avoid functions
  that pick up newer versions (`atoi`/`strtol` -> `__isoc23_strtol@GLIBC_2.38`). Check with
  `objdump -T x.so | grep GLIBC_`.
- The escalator can exit on its own; `startup.sh` respawns it. Without it CEF init crashes QtCar.
- The sandbox and the real chroot share the host network (same service ports: 4160, 4220, 4030,
  4190, 1234, ...). Stop the sandbox (`podman ps`, `podman kill`) before the user runs
  `./tesla start` (it refuses to start while the ports are taken). A chroot service that ignores
  SIGTERM (a GpsManager did) keeps its ports; `./tesla start`/`stop` SIGKILL leftovers.
  "Failed to listen on port ..." in a service log = check this.
- Shell: zsh with aliases (`rm -i`, `cp -i`, `t`). Use `command rm/cp`, and don't use `pkill -f`
  with patterns that match your own command line (same for `grep` over `/proc/*/cmdline`: use
  `'na[m]e'`). `grep` is ugrep.
