# bluetooth (experimental)

The firmware's Bluetooth stack is Broadcom's BSA (`bsa_server`), not BlueZ. On the car it talks to
a BCM4349 over a UART (`bsa_server -d /dev/ttyS0 -p /lib/firmware/bcm-bt.hcd`); Tesla's `btd`
(the "lgit" stack, `libbsa`) sits on top and publishes `com.tesla.Bluetooth` on the system D-Bus,
where `QtCarBluetooth` (service `qtcar-bluetooth`) connects.

`hci-bridge.py` gives BSA a pseudo terminal as its "UART" and forwards the H4 packets unchanged to
a PC's adapter through the kernel's HCI user channel (raw, exclusive; BlueZ must not use the
adapter). No translation: HCI is the same for every vendor. Broadcom-only vendor commands are
rejected by other controllers, and BSA goes on; Broadcom's "LM diagnostics" packets (H4 type 7,
`07 f0 01`) are dropped by the bridge.

Manual setup so far (a laptop with an Intel AX210, 2026-09-25), with QtCar running:

```sh
sudo systemctl stop bluetooth; sudo hciconfig hci0 down
sudo bluetooth/hci-bridge.py &                       # -> /tmp/hci-bridge-tty
sudo touch chroot/dev/ttyS0; sudo mount --bind $(readlink /tmp/hci-bridge-tty) chroot/dev/ttyS0
sudo chroot chroot sh -c 'mkdir -p /var/run/dbus /var/run/bsa_server /var/run/btd /var/lib/btd;
    rm -f /var/run/messagebus.pid; dbus-daemon --system --fork'
sudo chroot chroot /usr/bin/bsa_server -d /dev/ttyS0 -u /var/run/bsa_server/ &   # -all=5 traces, -b snoop
sudo chroot chroot sh -c 'cd /var/run/btd && exec /usr/bin/btd' &
sudo chroot chroot /usr/local/bin/qtcar-service qtcar-bluetooth &
```

(`bsa_server` segfaults with `env -i`; run it with sudo's environment.)

State: pairing, encryption, hands-free, AVRCP and the phonebook (PBAP, contacts and call lists)
work with Pixel 7a phones. A2DP (music) fails: the phone opens A2DP while the car does too, its own
connection attempt hangs in an SDP query and times out after 30 s, and Android then drops the
device. Not built into `./tesla` yet; audio into AudioWeaver (`a2dpbridge`, a second snd-aloop card
"virtual") isn't set up either.
