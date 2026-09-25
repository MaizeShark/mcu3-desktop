# Development

## Repository

| Path | What |
|---|---|
| `tesla` | the command: dispatches to `tools/` |
| `tools/common.sh` | settings loading (`tesla.conf` < environment < options), messages, process and mount helpers, `teardown` |
| `tools/check.sh` | the checks (`./tesla check`, and quietly before a start) |
| `tools/start.sh` | `./tesla start`: options, mounts, services, helpers, the QtCar loop, cleanup |
| `tools/build-image.sh` | `./tesla build-image` |
| `tools/logsummary.py` | `./tesla logs`; `service_status()` is also used by `./tesla status` and the panel |
| `tesla.conf.example` | all settings with their defaults |
| `start_all.sh` | alias for `./tesla start` |
| `mcu2-patchkit/` | the patch kit: `mcu2_patch.py`, `src/` (shims, `qtcar-service`, `sv` wrapper), `sources.toml` (where the libraries are downloaded from; cached in the untracked `payload/`) |
| `tesla-touch/` | the touch bridge (Rust) |
| `navigation/` | Valhalla image, tile build, map install |
| `vehicle/` | `tesla-can.py` (CAN + panel), `panel.html`, `tesla-gps.py`, `extract-can-db.py`, `can-sniff.py`, `qtcar-settings.py` |
| `sandbox.sh` | QtCar and services in rootless podman, without sudo |
| `notes/` | the session notes |

Untracked (the `.gitignore` is a whitelist): the firmware dump, images, `chroot/`, `logs/`,
`sandbox/`, `tesla.conf`, build output, `vehicle/work/`, `navigation/work/`.

## Testing without sudo: the sandbox

`sandbox/rootfs` is a user-owned copy of the rootfs (`cp -a mcu2-original sandbox/rootfs` as
your user; root-only files such as the escalator are skipped) with the kit applied
(`./tesla build-image --sandbox`).
`./sandbox.sh` runs QtCar and services in rootless podman on Xvfb `:97`:

```sh
./sandbox.sh -t 90 -s "60 85" --touch qtcar-sim qtcar-vehicle   # 90 s, screenshots at 60 and 85 s
DISPLAY=:97 xdotool mousemove 858 203 click 1                     # tap (with --touch)
./sandbox.sh --help
```

QtCar runs under the host's gdb there (a crash leaves a backtrace in `sandbox/out/qtcar.log`),
with the browser skipped unless `--browser` (the escalator is missing). `--audio`, `--video`,
`--music DIR` pass devices and folders in. The sandbox shares the host network: stop it before
`./tesla start`. More in `CLAUDE.md`.

## Without the UI

- `./tesla start --dry-run`, `./tesla check`, `./tesla config` need no sudo.
- A single service: `podman run --rm --network host -e QCSVC_NOUSER=1 --rootfs $PWD/sandbox/rootfs /usr/local/bin/qtcar-service qtcar-vehicle`
- Data values of running services: `curl --http1.0 "http://127.0.0.1:4220/_data_get_value_request_?name=VAPI_odometer"`
- CAN traffic: `vehicle/can-sniff.py`; signals by name: `vehicle/tesla-can.py --find TEXT`.
- The firmware's own tools on the host: `mcu2-original/lib64/ld-linux-x86-64.so.2 --library-path usr/proto2/lib:usr/lib:lib <bin>`.

## Changing the patch kit

Keep changes in the kit (and `qtcar-service`, `start.sh`, `vehicle/`, `navigation/`), not as
manual edits of the image: the image must be reproducible from the dump. Binary patches are
`(id, group, offset, original bytes, new bytes, description)` entries with the file's SHA-1
before and after. Every edit of `mcu2_patch.py` or `src/` changes the kit version, so
`./tesla check` asks for a `./tesla build-image`.

## Conventions

- Shell: bash, `set -u`; the firmware side is busybox-like `sh`.
- The firmware's glibc is 2.22: preload shims must not pull newer symbol versions
  (`objdump -T x.so | grep GLIBC_`).
- Run firmware programs with a clean locale (`env -i`, `LC_ALL=C`).
- Commits: small and often; the notes of each session go to `notes/`.
