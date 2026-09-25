# Ideas (not planned yet)

## A real Autopilot computer (HW2.5) on the PC's Ethernet

The user has an HW2.5 Autopilot computer and would like to connect it to the PC once it runs on
the bench, so that it talks to the firmware's services directly (and provides what it has, e.g.
the position, instead of the fake GPS). HW2.5: a CPU that does most of the work, a second CPU
(mostly for the dashcam, as far as the user knows) and a GPU for the neural networks. Bench only,
never in a car that drives.

What the firmware says (2019.20.4.2):
- The car network is 192.168.90.x: MCU (cid/ice) .100, instrument cluster .101, gateway .102,
  Autopilot ("ap", "ape") .103, a second Autopilot node ("ap-b", "ape-b") .105, .104 "lb"
  (`/etc/hosts`, `/etc/RunQtCar.vars`: `QCSUBNET`, `QCCID`, `QCGW`, `QCAP`, `QCLB`, ...).
- On the car the QtCar services get these addresses (`--gw`, `--ip`, ...) and use multicast
  (224.0.0.26). `qtcar-service` strips them on purpose so everything runs on loopback here. A
  "car network" mode would put a dedicated NIC (or a network namespace) on 192.168.90.100 and
  keep those options.
- QtCarGpsManager knows `LOC_adas*` values (a position from the Autopilot side) besides its own
  receiver: with the Autopilot computer connected, `tesla-gps.py` would stay off, maybe parts of
  `tesla-can.py` too.

To find out: what the Autopilot computer sends to the MCU (UDP ports; `vehicle/can-sniff.py` or
tcpdump on that NIC), what it needs to run on the bench (12 V, its CAN buses, the gateway's
messages), and how the MCU's services pick its data up.
