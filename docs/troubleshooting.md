# Troubleshooting

## First steps

1. **`./tesla check`**: host tools, kernel modules, the image and its kit version, maps, ports,
   settings. Every problem comes with a hint.
2. **`./tesla status`** while it runs: which service is DOWN or restarts often, and its log.
3. **`./tesla logs`** after a run (also printed when it stops): QtCar and service restarts with
   their times, segfaults from the kernel log, audio pump errors and clipping, the GPS fix,
   services that never printed anything, known error messages.

## Problem reports

Send the output of `./tesla logs` and the run's folder `logs/<date-time>/` (the summary names
it). `config` in there shows the settings used. For a service that hangs or behaves oddly,
`./tesla start --strace "SERVICE"` records what it does (`strace-SERVICE.txt` in the folder).

## Symptoms

| Symptom | Cause / fix |
|---|---|
| `port 4160 is in use` (or 4220, 4030, ...) | a leftover of an earlier run (`./tesla stop`), or the sandbox (`podman ps`, `podman kill <id>`) |
| `the image is mounted at X, not at chroot/` | it's mounted elsewhere (by hand, or an old mount point); mounting it twice would corrupt it. `sudo umount X` |
| `couldn't unmount chroot/, it's busy` | the list below it shows who holds it (a shell inside it, a file indexer, ...); stop that, then `sudo umount chroot` |
| `the image was patched by another kit version` | `./tesla build-image` (after a `git pull`) |
| Media -> USB stays at "Loading..." | the audio services didn't come up (`./tesla status`); with a kit before 2026-09-24 they waited 50 s for the car's gateway: `./tesla build-image` |
| No sound | `./tesla logs` (pump errors? audio services without output?); `snd-aloop` loaded without `id=model3`: `sudo modprobe -r snd-aloop` and start again |
| Sound stutters / mostly silent | `./tesla logs` shows many pump errors: the loopback card is taken by PulseAudio/PipeWire (install `tools/99-tesla-sound.rules`, then reload snd-aloop), or the kernel tick is coarse and the high-resolution clock didn't come up (`audio-clock.log`) |
| Bluetooth music silent | select the Bluetooth source ("Phone") in the Media app; `a2dpbridge` must run (`./tesla status`) |
| The PC's Bluetooth doesn't work after a run | `./tesla stop` normally gives it back; else `sudo modprobe -r btusb && sudo modprobe btusb`, or a reboot |
| Camera picture black | the source must deliver frames; `camera.log` has ffmpeg's messages; shift into R |
| GPS: the car doesn't move | a receiver indoors has no fix (`gps.log`: RMC status V); GpsManager not running (`./tesla status`) |
| Navigation: "maps missing" or no route | maps not installed (`./tesla check`); the destination is outside the installed region; online routing doesn't work |
| A setting or car value doesn't stick | QtCar stores it in its settings DB in the image; `vehicle/qtcar-settings.py` edits it |
| The host desktop reacts to touches | the udev rule for tesla-touch isn't installed (`./tesla check`) |
| Arcade: Beach Buggy Racing 2 ignores taps | it renders in software and runs at a few frames a second (`/run/qtcar-sv/cobalt.log` in the image: "DRI2: could not open /dev/dri/card0"); hold a tap longer, or use `--native` with an Intel GPU |
| Arcade: the car doesn't steer, "no device path found for game-steering" | start with `--vehicle` (qtcar-vehicle makes the device); the image needs the current kit (`./tesla build-image`) |
| Arcade: a game window with a title bar, in the wrong place, or MAME ignores COIN/START | the window helper isn't running (`game-windows.log` in the run folder) |
| QtCar restarts right after the start | normal once: it applies the car configuration and restarts itself (`events.log` gives the reason) |
| Many ERROR lines in `qtcar.log` | normal: services, hardware and online servers that aren't there |
