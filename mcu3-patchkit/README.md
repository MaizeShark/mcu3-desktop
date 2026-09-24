# mcu3-patchkit

Rebuilds the working Tesla Model 3 MCU chroot (firmware 2019.20.4.2) from the pristine
dump in `../mcu3-original`. Output matches `MCU2/mcu3.ext4` as of 2026-05-31. All
5 patched binaries come out byte-identical to that image.

## Contents

| Path | What |
|---|---|
| `mcu3_patch.py` | Applies everything. Idempotent, checks every patch's original bytes and SHA-1 first |
| `sources.toml` | Where each library comes from: Ubuntu 16.04 package URL, sha256, path inside the package |
| `src/icu_preload.c` | Source of `icu_preload.so` (verified: compiles to the same code) |
| `src/fake_sandbox.c` | Reconstructed source of `/usr/tesla/UI/bin/chrome-sandbox` (verified: same code) |

### Where payload files come from

The repository contains no binaries: the libraries are downloaded, the shims are built. For each
file, `mcu3_patch.py` tries in order:

1. `payload/<path>` (the local cache), if its hash matches;
2. the `url` in `sources.toml`: a package (`.deb`, `.tar.*`, `.zip`) from the Ubuntu 16.04
   archive with `member` naming the file inside. Packages are cached in `downloads/`. The
   download is checked against `sha256` and the extracted file against the exact hash in
   `mcu3_patch.py`;
3. for `icu_preload.so`, `egl_pixmap_shim.so`, `cef_nosandbox.so` and the fake `chrome-sandbox`:
   compiled from `src/` with gcc.

Tested 2026-09-24: all 21 files obtained into an empty `payload/` (17 downloaded, 4 built).

The fake `chrome-sandbox` of the old May image was built dynamically on a modern host and needs glibc ≥ 2.34,
but the chroot has 2.22, so it can't run there. The fallback build links it statically, which
avoids this. (With `cef_nosandbox.so` CEF doesn't use it anymore.)

## Build a fresh image

```sh
./tesla build-image --fresh          # new mcu3-new.ext4 from mcu3-original/: copy, kit, maps
./tesla build-image                  # after a git pull: apply what's new (only what's missing)
./tesla build-image --check          # what an update would change
```

By hand, `--fresh` is:

```sh
truncate -s 6G mcu3-new.ext4 && mkfs.ext4 -F mcu3-new.ext4
sudo mkdir -p /mnt/mcu3new && sudo mount -o loop mcu3-new.ext4 /mnt/mcu3new
sudo cp -a mcu3-original/. /mnt/mcu3new/
sudo python3 mcu3-patchkit/mcu3_patch.py /mnt/mcu3new
sudo umount /mnt/mcu3new
```

Another image: `IMAGE=` in `tesla.conf`.

**Kit version**: after a successful run the kit writes `/etc/mcu3-patchkit` into the image
(`kit=` a hash over `mcu3_patch.py` and `src/`, the git commit, the date, skipped groups).
`./tesla check` compares it with the current kit and says when the image needs
`./tesla build-image`. Any edit of `mcu3_patch.py` or `src/` changes the version, also a comment.

## Usage

```sh
python3 mcu3_patch.py --list               # all groups / patches / files
sudo python3 mcu3_patch.py ROOT --check    # report only, changes nothing (text files: missing / N lines differ)
sudo python3 mcu3_patch.py ROOT -q         # only what isn't already ok
python3 mcu3_patch.py --kit-version        # the current kit version
sudo python3 mcu3_patch.py ROOT            # apply
sudo python3 mcu3_patch.py ROOT --skip cef --skip gui-camera-init-revert
sudo python3 mcu3_patch.py ROOT --vin ... --birthday ...
```

Groups: `core` (needed binary patches), `cef` (browser), `mesa`,
`assets`, `system`, `identity`, `vehicle` (simulator without its drive-inverter module, for
`--vehicle`, see `../vehicle/README.md`), `audio`. The script refuses to run while something is
mounted inside ROOT, so stop QtCar first (`./tesla stop`). Patched and replaced files are kept as `<file>.orig`.

## Differences from the old image

- `icu_preload.so` lives in `/usr/lib` instead of `/tmp`, and `/startup.sh` points there.
- `/startup.sh` also sets DNS (`/var/run/connman/resolv.conf`). Without it no map tiles
  load. This was the actual cause of the "invalid API key" issue.
- `libLLVM-6.0.so`, `libexpatw.so.1` and `libsensors.so.4` are symlinks instead of duplicate copies.
- Left out on purpose: the Google API key patch (reverted, DNS was the real issue), the libcef.so
  sandbox patch (reverted), crash dumps, `/model3_init.sh` (old QEMU attempt), QtCarSettings.db.

## Notes

- The browser (2026-09-23, session 4). What it needed:
  - `src/cef_nosandbox.c`, preloaded by `startup.sh`: sets `CefSettings.no_sandbox` in a
    `cef_initialize()` wrapper. Env: `CEF_LOG_SEVERITY` (startup.sh: warning), `CEF_LOG_FILE`,
    `CEF_REMOTE_DEBUGGING_PORT` (startup.sh: 9222, DevTools on localhost:
    `curl http://127.0.0.1:9222/json`), `CEF_EXTRA_ARGS` (Chromium switches).
  - The old `gui-cef-nosandbox` patch is reverted (`gui-cef-nosandbox-revert`): it patched
    libQtCarGUI's copy of `ChromiumManager::initializeCef`, but QtCar uses its own copy, so it
    never did anything. The same holds for `gui-escalator-*` and `gui-cgroup-exit-*` (harmless).
  - `UI/lib/libcef.so` links to `/usr/lib/libcef.so` and CEF looks for its resources next to it;
    `v8_context_snapshot.bin` (plus `devtools_resources.pak`, `swiftshader`) were missing there
    and every renderer died with "Failed to deserialize the V8 snapshot blob".
  - `cef-no-oscillator`: Tesla's CEF has Blink's stub FFT, so a WebAudio `OscillatorNode` crashes
    the renderer (kernel log: `tesla-cef-zygot segfault ... libcef.so[4165885]`, an AVX vector
    loop writing to a null array; the SSE path crashes the same way). `createOscillator()` now
    returns null. `new OscillatorNode()` and `createPeriodicWave()` would still crash.
  - `egl_pixmap_shim.c` read X pixmap sizes wrong (0x0).
- Audio (session 5, `--audio`): apps write to tplug channels (/dev/shm), AudioWeaver
  (`AWE_command_line_tesla`, DSP) processes, **audiod** does the ALSA I/O ("pump") for it and
  owns the sound card. On a PC: snd-aloop as card `model3` (asound.conf block maps pcm 0/2/3 to
  its subdevices), AWE gets `/etc/asound-awe.conf` via `ALSA_CONFIG_PATH` so it can't grab the
  card (on the car its minijail has no /dev/snd; otherwise audiod gets EBUSY and never pumps),
  the eCall tplugs are resampled (48 vs 8 kHz, AWE exited), and `audiod-no-a2b` keeps audiod
  from starting the A2B stack (I2C amplifiers/mics; a2b_stackAlloc fails and it exits).
  audiod commands: `echo "volume ?" | socat - tcp:127.0.0.1:18466` (`?` reads, `<cmd> <v>` sets,
  list in /opt/audioweaver/dsp-control.csv). Base amp channels (1-based): 4,5,7 left, 2,6,8
  right, 1 both, 3 unused; `./tesla start` sums them to stereo (`sox remix -m`) into pw-play.
- `startup.sh` writes `vm.mmap_min_addr=0` and `vm.overcommit_memory=1` to the **host** kernel
  (`/proc` is bind-mounted). They stay set until reboot.
- Developer mode needs no patch. While QtCar is running, inside the chroot:
  `wget -q -O - "http://127.0.0.1:4220/_data_set_value_request_?name=GUI_developerMode&value=true"`

## Firmware services

Besides QtCar, the firmware has 17 more services built on the same framework (`/etc/sv/qtcar-*`).
The kit installs `/usr/local/bin/qtcar-service`, which starts one of them the way its runit script
does on the car (same binary, options and user), without minijail, AppArmor, cgroups or firewall,
and on loopback instead of the car's `192.168.90.x` network:

```sh
./tesla start --services "qtcar-connman"    # started before QtCar, logs in logs/
./tesla services                            # the available services
```

Survey 2026-09-23, each service alone for 6 s: all 17 start and keep running.
- `qtcar-vehicle`: the CAN↔data-value bridge (VehicleService :4030, Power :4170); it expects
  CAN frames as UDP from the gateway on :1234 / :31415
- `qtcar-carserver`, `-gpsmanager`, `-audiod`, `-evlogservice`, `-connman`, `-mediaserver`,
  `-tmserver`, `-nuanceserver`, `-monitor`, `-radioserver`, `-bluetooth`, `-speechrecognizer`,
  `-spotifyserver`, `-ecallclient`, `-connmetrics`, `-startup`: run; the only errors are missing
  hardware (LTE GPIO) and unsupported features ("Service Directives")

Not a runit service but in `/usr/tesla/UI/bin`: `QtCarSimService`, Tesla's vehicle simulator
(486 `SIM_*` signals: position, battery, doors, gear, charging, …). `qtcar-service qtcar-sim`
starts it: it acts as the gateway and sends a simulated car's CAN frames to `qtcar-vehicle`.
`./tesla start --vehicle` uses it (details in `../vehicle/README.md`).

`./tesla start` restarts services that exit, like runit: `qtcar-vehicle` exits on purpose when the
car configuration changes.

Testing without sudo: a user-owned copy of the rootfs runs in rootless podman:
`podman run --rm --network host -e QCSVC_NOUSER=1 --rootfs $PWD/sandbox/rootfs qtcar-service qtcar-vehicle`
(`sandbox/` = `cp -a mcu3-original sandbox/rootfs` + this kit; a few root-only files are skipped by
the copy, e.g. `/usr/bin/escalator`, so QtCar dies in CEF there after ~20 s; see
`notes/session4-*.md` for running it under gdb with the browser skipped).
