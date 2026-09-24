# Setup

Everything here is needed once. Afterwards `./tesla check` shows whether all is in place.

## What you need

- **An x86-64 PC with Linux.** Developed and used on KDE neon (Ubuntu 24.04 base, PipeWire,
  X11 and Wayland desktops). On a Mac: a Linux VM (on Apple Silicon with x86 emulation, slow).
- **About 10 GB of disk space**: the firmware dump (~2 GB), the image (6 GB), maps (a few GB
  per region while building).
- **sudo**: the chroot, mounts and kernel modules need root. `./tesla` asks for the password
  once per start.
- **Your own dump of the MCU3 firmware 2019.20.4.2** (x86_64). This repository contains no
  firmware and no files from it, and nothing here downloads it. Only this version was ever tested:
  the patch kit checks every file it patches and refuses unknown versions.

## 1. Host packages

Ubuntu / Debian names:

```sh
sudo apt install xvfb x11vnc x11-utils curl python3 e2fsprogs cargo   # always
sudo apt install krdc                        # a VNC viewer (or virt-viewer, remmina, tigervnc-viewer)
sudo apt install alsa-utils sox pipewire-bin # --audio
sudo apt install ffmpeg v4l2loopback-dkms    # --camera
pip install --user unicorn pyelftools        # --vehicle (extracts the CAN database once)
sudo apt install podman                      # only to build map tiles
```

Python 3.11 or newer is needed.

## 2. Touch

`tesla-touch` turns the VNC mouse into a multitouch screen for QtCar (see
[`tesla-touch/README.md`](../tesla-touch/README.md)):

```sh
(cd tesla-touch && cargo build --release)
# keep the host desktop from using the virtual touchscreen too
sudo cp tesla-touch/99-tesla-touch.rules /etc/udev/rules.d/ && sudo udevadm control --reload
```

## 3. The image

Put your firmware dump (the root filesystem, `usr/tesla/UI/bin/QtCar` inside) at
`mcu3-original/` in the repository folder, owned by root as extracted. Never modify it; the
image is a patched copy.

```sh
./tesla build-image --fresh
```

This creates `mcu3-new.ext4` (6 GB, `--size`): it copies the dump, applies the patch kit
([`mcu3-patchkit/`](../mcu3-patchkit/README.md): binary patches, a software OpenGL renderer,
config, service scripts) and installs maps if some are built. The kit downloads nothing from Tesla.
The open-source libraries it adds (Mesa, LLVM, ...) are downloaded once from the Ubuntu 16.04
archive (`mcu3-patchkit/sources.toml`, checked by hash) and cached in `mcu3-patchkit/payload/`;
its shims are compiled from `mcu3-patchkit/src/` (needs gcc and internet access the first time).

After a `git pull`, bring the image up to date with `./tesla build-image` (only what changed is
applied; `--check` shows what would change). The image remembers which kit version patched it
(`/etc/mcu3-patchkit`), and `./tesla check` warns when it's outdated.

The image is mounted at `chroot/` while it runs (`IMAGE` and `CHROOT` in `tesla.conf`).

## 4. Maps (optional)

Offline routing needs map tiles, built from an OpenStreetMap extract with Valhalla 2.4.3 in
podman (the build image takes ~30 min once, a region like Northern California a few minutes):

```sh
podman build -t valhalla:2.4.3 -f navigation/Containerfile navigation
navigation/build-tiles.sh ~/Downloads/norcal-latest.osm.pbf US     # region: US, EU, ...
./tesla build-image                                                # installs them
```

Details: [`navigation/README.md`](../navigation/README.md). Without maps the map still shows
(online tiles), but routing doesn't work.

## 5. Kernel modules

- `uinput` (touch): usually loaded; else `sudo modprobe uinput`.
- `snd-aloop` (`--audio`): `./tesla start` loads it as card `model3`. If it's already loaded
  without that name: `sudo modprobe -r snd-aloop`.
- `v4l2loopback` (`--camera`): two devices, one for your camera source and one for QtCar:
  `sudo modprobe v4l2loopback devices=2 video_nr=10,32 exclusive_caps=1,1`
  (permanently: `/etc/modprobe.d/` and `/etc/modules-load.d/`).

## 6. Check

```sh
./tesla check
```

Every line is `ok`, `--` (not needed for what's enabled), `WARN` or `FAIL`, with a hint how to
fix it. Then: [usage.md](usage.md).
