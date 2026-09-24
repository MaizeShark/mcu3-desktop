Tesla Model 3 MCU chroot project in ~/TeslaMCU. Read CLAUDE.md, README.md, then
notes/session5-2026-09-23.md (the latest session: vehicle data, audio, camera, videos, music).

Current state (all working in the real chroot, start_all.sh):
- QtCar with touch (tesla-touch), all firmware services (qtcar-service), offline navigation
  (Northern California), the browser (Chrome 73).
- VEHICLE=1: Tesla's simulator + vehicle/tesla-can.py (merge mode, presets, answers the UI's
  buttons: frunk/trunk/lock/charge port/lights/wipers) and a web panel on http://localhost:8099.
  VEHICLE_COLOR / VEHICLE_WHEELS / VEHICLE_PERFORMANCE.
- GPS=...: fixed position, a Valhalla route, or a real receiver (--uart).
- AUDIO=1: AudioWeaver + audiod (kit patch audiod-no-a2b) on snd-aloop as card "model3",
  host plays it via arecord | sox | pw-play. Chimes, media, fireplace sound, GUI volume work.
- MUSIC=DIR: a folder as USB stick (Media -> USB). CAMERA=/dev/videoN: backup camera via ffmpeg
  -> /dev/video32 (XR24). Fireplace / dog mode / HAL 9000 videos (kit's /sbin/sv wrapper).
- ./sandbox.sh: QtCar + services in rootless podman without sudo (gdb, screenshots, xdotool,
  --audio, --video, --music). Shares the host network: stop it before start_all.sh.

Commit and push to the Forgejo remote as often as you like.

Goal of this session: improve the existing tools and make them more user friendly. No big new
features. In rough order (ask me before large restructurings):

1. Starting: start_all.sh has grown to many env variables (AUDIO, VEHICLE, GPS, MUSIC, CAMERA,
   VEHICLE_COLOR, ...). A config file (e.g. `tesla.conf`, commented, with defaults) and/or
   command line options (`./start_all.sh --audio --music ~/Music`), a `--help`, and a clear
   summary at startup of what is enabled and where things are (VNC, panel, logs).
2. Checks with helpful messages before starting: kit applied to the image (and which version),
   snd-aloop / v4l2loopback / uinput available, host tools present (x11vnc, sox, pw-play,
   ffmpeg, podman ...), sandbox running on the same ports, image mounted/unmounted.
3. Stopping and cleanup: a clean stop (Ctrl+C and a `./start_all.sh stop` or similar), no
   leftovers (processes, mounts). Consider running the image in a rootful container (podman as
   root with the image as rootfs) instead of the chroot, if everything keeps working (devices,
   escalator, browser, audio, camera).
4. After a run: a short log summary (services that restarted, crashes in `journalctl -k`, audio
   underruns, GPS fix), so a problem report is quick.
5. Web panel: show which services run, link to logs; maybe start/stop audio, camera, music there.
   Panel values that fight with GPS (gear/speed) should be clearly marked.
6. Kit: one command to build/update the image (`mcu3_patch.py` + maps + settings), explain
   what changed (`--check` summary), and a simpler "fresh image" path.
7. Docs: README as a user guide (what works, what needs what, how to use each option, known
   limits), component READMEs up to date, notes trimmed. Maybe drop `legacy/`.

Known issues / leftovers (smaller, only if cheap):
- After a restart QtCar reopens USB as the last source and stays at "Loading..."; tapping the
  USB tab works.
- TuneIn needs Tesla's backend (deejay-prd.ui.tesla.services), Spotify's eSDK endpoint is gone;
  an own TuneIn-like backend with internet radio would be a separate project.
- TPMS warnings don't trigger; "+0 mi" charge added; seat heaters / HVAC requests not answered.
- Sound a bit off: the 8 base amp channels are mixed to stereo roughly (AUDIO_REMIX).
- Root-only files (mode 700) are missing from sandbox/rootfs; ask me for a copy when needed.

How to work: test in the sudo-free sandbox first (./sandbox.sh, see CLAUDE.md). I (the user) run
sudo commands and give you the log folder from logs/. Keep changes in the repo tools (patch kit,
qtcar-service, start_all.sh, vehicle/, navigation/), not as manual edits to the image. Keep an eye
on context; stop at a good point and update notes + this prompt before it runs out.
