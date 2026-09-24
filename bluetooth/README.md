# bluetooth

`./tesla start --bluetooth` runs the car's own Bluetooth stack on the PC's adapter: pairing, the
phone (hands-free profile), contacts and call lists (PBAP), media control (AVRCP) and, with
`--audio`, music from the phone (A2DP) through the car's audio stack.

## How

The firmware doesn't use BlueZ. On the car:

```
QtCarBluetooth (qtcar-bluetooth) --D-Bus (com.tesla.Bluetooth)--> btd --libbsa--> bsa_server --UART--> BCM4349
                                                                   |
                                               A2DP audio: btd -> snd-aloop card "virtual" -> a2dpbridge (alsaloop) -> AudioWeaver
```

`bsa_server` is Broadcom's stack (BSA), talking HCI to its chip over `/dev/ttyS0` and loading a
firmware patch into it (`-p bcm-bt.hcd`). `btd` is Tesla's daemon on top ("lgit" stack) and
publishes `com.tesla.Bluetooth` on the firmware's own system bus.

On a PC, `hci-bridge.py` gives BSA a pseudo terminal as its "UART" and forwards the HCI packets
(H4 framing) unchanged to the PC's adapter through the kernel's HCI user channel (raw, exclusive:
BlueZ is stopped while it runs). There is no translation: HCI is the same for every vendor.
What the bridge does handle:

- Broadcom vendor commands (`0xFCxx`, OGF 0x3F) are answered by the bridge with "Unknown HCI
  Command" instead of being sent on. On other controllers these opcodes mean other things, and an
  Intel AX210 hung after BSA's periodic `0xFC48` (it dropped off USB). BSA carries on without
  them. `--pass-vendor` sends them on (a real Broadcom/Cypress controller).
- Broadcom's "LM diagnostics" packets (H4 type 7, `07 f0 01`) are dropped.
- On exit it resets the controller (HCI_Reset), so BlueZ gets a working adapter back.

`./tesla start --bluetooth` (tools/start.sh `start_bluetooth`): stops `bluetooth.service`, starts
the bridge (it takes the adapter down itself), binds its pseudo terminal as the chroot's
`/dev/ttyS0`, and starts the services `dbus` (the firmware's bus), `bsa_server`, `btd` (waits
for both), `qtcar-bluetooth`, and with `--audio` `a2dpbridge`. `./tesla stop` gives the adapter
back to BlueZ; if it doesn't answer, it reloads its driver and, as a last resort, power-cycles its
USB port (`restore_bluetooth` in tools/common.sh).

## State (2026-09-25, ThinkPad T480 with an Intel AX210, Pixel 7a phones on Android 17)

- Pairing, encryption, hands-free, AVRCP and the phonebook work.
- A2DP: connects (a few times it took a second attempt: the phone and the car both start the
  audio connection, and the phone's own attempt can time out; the car reconnects by itself).
  Music plays through AudioWeaver once the car's audio source is Bluetooth: select it in the
  Media app ("Phone"). Underneath that is audiod's `source-select 3` (0 = the car's own media).
- Not tried: calls (the hands-free audio path, eCall/mic channels), a second phone at once.

## Debugging

- `bsa_server` traces: stop the job's process and run it by hand in the chroot,
  `BSA_ARGS="-all=5 -b /tmp/bsa.snoop" /usr/local/bin/qtcar-service bsa_server` (or `bsa_server
  -all=5 ...` directly). It segfaults with an empty environment (`env -i`).
- `hci-bridge.py -v` logs every packet (`>` to the controller, `<` from it).
- The phone's side: Android's HCI snoop log (Developer options, or with root:
  `setprop persist.bluetooth.btsnooplogmode full`, then Bluetooth off/on), readable with tshark;
  `adb logcat` shows the profile state machines (`A2dpStateMachine`, `bluetooth-a2dp`).
- audiod's DSP over TCP 18466: `source-select ?`, `input-meter ?` (inputs 11/12 are A2DP).
