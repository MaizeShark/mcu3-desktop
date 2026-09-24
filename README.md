# mcu3-stuff

Runs the Tesla Model 3 touchscreen UI (QtCar) of the **MCU3** on a Linux PC: the firmware runs
in a chroot, the screen is a virtual display you see and touch over VNC. Around it: fake vehicle
data with a web panel, GPS (fixed, driving a route, or a real receiver), sound, a folder as USB
music stick, a backup camera from any V4L2 source, and offline navigation.

> **MCU3?** The media control unit this firmware runs on is often called **MCU-Z**.
> This project calls it MCU3: it follows the MCU2, so 3 makes more sense.

## Please read first

- **No firmware here.** This repository does not contain, provide or redistribute Tesla firmware
  or any files from it, and nothing in it downloads them. You need your own dump of the firmware.
  The patch kit only holds byte patches (a few bytes each, applied to your copy) and its own
  scripts and shims. The open-source libraries it needs (Mesa, LLVM, ...) are downloaded from
  the Ubuntu archive when you build the image, not shipped here.
- **Not affiliated with Tesla.** This is an independent hobby project. It is not made, endorsed
  or supported by Tesla, Inc. "Tesla" and "Model 3" are trademarks of Tesla, Inc.
- **Only firmware 2019.20.4.2 (x86_64)** was ever tested and used. The patch kit checks every
  file it patches and refuses other versions.
- **Made with an LLM.** Sessions 1 and 2 (May 2026) were done with Claude Sonnet, all later ones
  with Claude Opus 5 & 5.5 (about 4.5M Token), guided and tested by a human. The session notes in [`notes/`](notes/)
  show how.
- **Demonstration Video** [Youtube Video](https://youtu.be/45vGXZr9VNI)

## Quick start

```sh
# once (details: docs/setup.md)
sudo apt install xvfb x11vnc x11-utils curl python3 e2fsprogs cargo krdc
(cd tesla-touch && cargo build --release)
sudo cp tesla-touch/99-tesla-touch.rules /etc/udev/rules.d/ && sudo udevadm control --reload
./tesla build-image --fresh           # your firmware dump in mcu3-original/ -> mcu3-new.ext4
./tesla check                         # what's missing, with hints

# run
./tesla start --vehicle --audio --gps 37.4419,-122.1430
# Note: on first start or settings changes QtCar can restart!
./tesla stop                          # or Ctrl+C
```

Touch: left click / drag = finger, mouse wheel = pinch zoom, right click = long press.

## What works

| Feature | Switch |
|---|---|
| The UI with touch: all apps and settings, the 3D car, the browser (Chrome 73), fireplace / dog mode / HAL 9000 | always |
| Offline navigation: routing and turn-by-turn on OpenStreetMap tiles; address search (Google, online) | maps installed |
| Vehicle data: battery, gear, speed, doors, locks, charging, lights, tires, telltales; the UI's buttons work; a web panel to change it all; the car's color and wheels | `--vehicle` |
| GPS: a position, a route to drive, or a real receiver | `--gps` |
| Sound: chimes, turn signals, media, volume | `--audio` |
| A folder as USB stick with music | `--music DIR` |
| Backup camera from any V4L2 device | `--camera DEV` |

`./tesla start --help` lists all options; `tesla.conf` holds your defaults.

## Documentation

| | |
|---|---|
| [docs/setup.md](docs/setup.md) | host packages, kernel modules, the image, maps |
| [docs/usage.md](docs/usage.md) | commands, options, settings, the screen, the web panel |
| [docs/features.md](docs/features.md) | each feature: what it needs, how it works, logs, limits |
| [docs/how-it-works.md](docs/how-it-works.md) | architecture, patch kit, ports, run folders |
| [docs/troubleshooting.md](docs/troubleshooting.md) | checks, log summaries, symptoms and fixes |
| [docs/development.md](docs/development.md) | the sudo-free sandbox, changing the kit, conventions |

What needs what, at a glance: [docs/README.md](docs/README.md#what-needs-what).

## Known limits

- **Online routing doesn't work**: it needs the car's key/credentials for Tesla's servers. Only
  offline routing inside the installed map region works.
- Streaming: TuneIn needs Tesla's media backend, Spotify's endpoint for this firmware is gone.
  USB music and the browser work.
- Not answered yet: seat heaters, climate requests; TPMS warnings don't trigger; the charging
  screen shows "+0 mi" added.
- The car's 8 amplifier channels are mixed to stereo only approximately.
- x86-64 Linux only (on a Mac: a Linux VM).

## License

MIT, see [LICENSE](LICENSE), for the code in this repository. The libraries the patch kit
downloads keep their own licenses. The firmware you use it with is Tesla's and not covered by it.
