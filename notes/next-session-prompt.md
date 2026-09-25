Tesla Model 3 MCU3 project in ~/TeslaMCU (public: github.com/MaizeShark/mcu3-desktop). Read
CLAUDE.md, README.md, docs/, then notes/session6-2026-09-24.md (the ./tesla command, native
screen on a touch laptop, GPU, camera, Bluetooth, audio clock) and notes/session7-2026-09-25.md
(the latest: every CAN signal on the panel, the arcade).

Current state:
- `./tesla start|stop|status|check|logs|config|services|build-image`, settings in tesla.conf.
  Everything from before works (QtCar, touch, services, offline navigation, vehicle data + web
  panel, GPS, browser, sound, USB music, backup camera, videos).
- New in session 6: `--native` (QtCar on the PC's own screen, letterboxed with black bars, a real
  touchscreen passed through by `tesla-touch --from`, OpenGL on Intel GPUs with Mesa 18's i965),
  `--bluetooth` (the firmware's Broadcom BSA stack + btd on the PC's adapter through
  bluetooth/hci-bridge.py: pairing, phone, contacts, A2DP music into AudioWeaver), the audio
  clock for 250 Hz kernels (snd-dummy "hrclock" as snd-aloop's timer_source), the webcam cropped
  to 4:3, the PulseAudio/PipeWire udev rule (tools/99-tesla-sound.rules).
- Test laptop: ThinkPad T480 (touchscreen, Intel AX210) with a Debian 13 live system in RAM,
  `ssh user@192.168.200.40` (passwordless sudo; everything there is gone after its reboot; don't
  reboot it). Its copy: ~/mcu3-desktop (repo files via rsync of `git ls-files`, the image copied
  with zstd). A rooted Pixel 7a ("Pixel 7a 2") is on this PC's adb, paired with the laptop's "Tesla".

Done in session 7: the panel's "CAN signals" section (browse/search/set/watch all 8759
signals, live decoded values) and "Arcade controls" (steering, brake, scroll wheels; keys,
gamepad). The arcade plays on the laptop (--native): BBR2 by touch and with steering/brake/
scroll wheels (CAN signals -> qtcar-vehicle's uinput devices), MAME with COIN/START and the
wheels (XTest keys, focus by tools/game-windows.py). Still to check with the user: the game
sound, the gamepad, VNC mode with the games, other MAME games.

Done late in session 7: the patch kit went from 25 to 5 binary patches (retired ones are
restored in existing images), the browser shows pages with --native, climate answers, TPMS
warnings, the charging screen's energy added, AMD GPUs behind --gpu (untested). Details:
notes/session7-2026-09-25.md.

Open, roughly in order (ask before large restructurings):
1. Radeon HD 6670 (the user has one): `./tesla start --native --gpu` with the screen on it should
   use Mesa 18's r600 (session 7, untested). Check that QtCar maps r600_dri.so and renders.
2. `./tesla arcade` (the user's idea): run just a game (BBR2) in the desktop's compositor, on
   the GPU, without the full QtCar UI; the arcade over VNC makes no sense in software rendering.
3. Bluetooth calls: the call audio (hands-free, microphone) is untested; the A2DP connect race.
4. A bootable stick: a script that builds a Debian (or Ubuntu: 1000 Hz tick) live image locally
   from the user's own dump, booting straight into `./tesla start --native` (never distributable:
   it contains the firmware).
5. Remaining audio dropouts in games follow the laptop's heat (session 7 notes).
6. Later (the user wants to try it once the hardware works): a "car network" mode for a real
   Autopilot computer (HW2.5) on the PC's Ethernet, bench only. What's known: the car network is
   192.168.90.x, MCU .100, IC .101, gateway .102, AP/"ape" .103, "ape-b" .105, LB .104
   (/etc/hosts, /etc/RunQtCar.vars: QCSUBNET, QCGW, QCAP, ...); the services normally get
   --gw/--ip/... and use multicast 224.0.0.26, which qtcar-service strips for loopback; so the
   mode would put a dedicated NIC (or a netns) on 192.168.90.100 and keep those options.
   GpsManager knows LOC_adas* (a position from the Autopilot side) besides its own receiver:
   with the APE connected, tesla-gps.py would stay off (and maybe parts of tesla-can.py). To find
   out: what the APE sends to the MCU (UDP ports, e.g. with can-sniff.py/tcpdump on that NIC),
   what it needs to run on the bench (12 V, its CAN buses, the gateway's messages), and how the
   MCU services pick the APE's data up. Never on a car that drives.

How to work: test in the sandbox or on the laptop first; the user runs sudo commands on this PC.
Commit often, push to origin (the user's Forgejo); push to `github` only when asked, after
checking for firmware files, binaries, keys and private details.
