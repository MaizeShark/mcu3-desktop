Tesla Model 3 MCU3 project in ~/TeslaMCU (public: github.com/MaizeShark/mcu3-desktop). Read
CLAUDE.md, README.md, docs/, then notes/session6-2026-09-24.md (the latest session: the ./tesla
command, docs, native screen on a touch laptop, GPU, camera, Bluetooth, audio clock).

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

Done late in session 6: contacts in the phone app (QtCarBluetooth's HOME), the arcade starts
(mame classics and Beach Buggy Racing 2 through qtcar-service jobs; BBR2 menus work).

The user's wishes for next time:
A. All CAN signals in the web panel (vehicle/panel.html, tesla-can.py): browse/search every
   message and signal of the CAN database (379 messages, 8759 signals), set and watch them.
B. Arcade controls: BBR2 is played with the steering wheel and pedals on the car.
   input_to_virtual (libTeslaDevices: DeviceDriverScrollWheel, a steering driver) creates
   "game-steering", "game-scroll-left/right" (missing here, only "game-touch"); find their source
   (probably CAN: steering angle, scroll wheels, pedals) and feed it (panel sliders, keyboard,
   gamepad). Also place the game windows at QtCar's TIDK window (Xfce decorates them).

Open, roughly in order (ask before large restructurings):
1. Bluetooth: does selecting "Phone" in QtCar's Media app switch audiod to source 3 (A2DP) by
   itself? Calls (hands-free audio, microphone). The A2DP connect race (sometimes a second try).
   A USB power cycle was needed once to revive the AX210; watch whether the vendor-command filter
   and the reset on exit keep it away.
2. The patch kit: remove the patches that do nothing (gui-escalator-*, gui-cgroup-exit-*, the
   *-revert entries, the chrome-sandbox patches); try `--prod` (QTCAR_ARGS) instead of the assert
   patches, with FEATURE_vectorMapTilesEnabled pinned off (prod mode leaves the map empty). Test on
   the laptop (sudo works there). Existing images still carry the old bytes: handle that.
3. A bootable stick: a script that builds a Debian (or Ubuntu: 1000 Hz tick) live image locally
   from the user's own dump, booting straight into `./tesla start --native` (never distributable:
   it contains the firmware).
4. Smaller: USB "Loading..." should be fixed by the audio_type change (verify); TPMS warnings;
   "+0 mi" charge added; seat heaters / HVAC requests; AMD GPUs (radeonsi_dri.so -> the kit's
   Gallium megadriver).

How to work: test in the sandbox or on the laptop first; the user runs sudo commands on this PC.
Commit often, push to origin (the user's Forgejo); push to `github` only when asked, after
checking for firmware files, binaries, keys and private details.
