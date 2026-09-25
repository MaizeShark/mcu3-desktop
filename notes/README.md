# Notes

How everything here was found out, session by session. The user guide is `../README.md`; the
component READMEs have the current details. These notes are history: later sessions supersede
earlier ones where they disagree.

| Note | What |
|---|---|
| `session1-2026-05-15.md` | the first chroot: squashfs dump, Mesa swrast, the binary patches QtCar needed to start |
| `session2-2026-05-16.md` | touch (QtCar's own EvDevTouchDriver, `/dev/input/touch`), early helper scripts |
| `session3-2026-09-22-23.md` | reconstructing the image as the patch kit, tesla-touch, firmware services (`qtcar-service`), offline navigation, the start of the browser work |
| `session4-2026-09-23.md` | fake GPS, vehicle data (simulator, CAN signal database), the browser (CEF without sandbox, V8 snapshot, WebAudio crash), the sandbox tooling |
| `session5-2026-09-23.md` | telltales, presets and the web panel, per-user service homes, real GPS receivers, UI buttons, audio (AudioWeaver/audiod on snd-aloop), backup camera, videos, USB music, Spotify/TuneIn dead ends |
| `session6-2026-09-24.md` | the `./tesla` command (start/stop/status/check/logs/build-image, tesla.conf), kit version stamp, panel services, docs |
| `session7-2026-09-25.md` | all CAN signals on the panel; the arcade (BBR2 and MAME): input devices, GPU, window manager, focus, sv verbs |
| `ideas.md` | ideas that aren't planned yet (a real HW2.5 Autopilot computer on the PC's Ethernet) |
| `next-session-prompt.md` | the prompt for the next session |
