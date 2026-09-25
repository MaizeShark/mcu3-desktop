#!/usr/bin/env python3
"""Fake vehicle CAN data for QtCar: sends CAN frames to QtCarVehicle, signal by signal.

On the car the gateway forwards CAN frames to the MCU as UDP datagrams: 2 bytes CAN id (big
endian) + 8 data bytes, broadcast to port 1234 (--udp :1234 in /etc/RunQtCar.vars). QtCarVehicle
(service qtcar-vehicle) turns them into data values (VAPI_*, ...) for QtCar. This tool encodes
signals by name with the firmware's own signal database (vehicle/work/can-db.json, made by
extract-can-db.py) and sends each message at its period, with counter and checksum filled in.

  tesla-can.py DI_gear=P DI_odometer=12345             run: send these signals until Ctrl+C
  tesla-can.py --preset parked [SIGNAL=VALUE ...]       start from a preset (see PRESETS)
  tesla-can.py --set DI_gear=D DI_vehicleSpeed=50       change values of the running instance
  tesla-can.py --set --preset charging                  ... or apply a preset to it
  tesla-can.py --list-presets                           what the presets set
  tesla-can.py --preset parked --http 8099              plus a web page to change things at runtime
  tesla-can.py --preset parked --respond                answer the UI's requests (frunk button, locks, ...)
  tesla-can.py --unset DI_vehicleSpeed                  stop sending a signal (its message stops
                                                        when no signal of it is left)
  tesla-can.py --status                                 what the running instance sends
  tesla-can.py --find odometer                          search signals (name, message, unit, enum)
  tesla-can.py SIM_driveRailOn=true                     SIM_* names go to QtCarSimService instead
                                                        (its data values, HTTP port 4190)

VALUE is a number in the signal's unit (see --find) or one of its enum labels (P, R, N, D, ...).
Other signals of the same message are sent as 0. A message that someone else is sending is left
to them: this tool listens on the port and pauses such ids (--no-yield to send anyway).

With --sim-port PORT (and QtCarSimService started with --udp :PORT, as start_all.sh does) the
sim's frames come here first and are forwarded, with the signals set here written into them. So
any signal can be set, also in messages the sim sends (e.g. BMS_packCurrent in 0x132).
"""
import argparse, collections, gzip, http.server, json, math, os, socket, struct, subprocess, sys, threading, time, urllib.parse

HERE = os.path.dirname(os.path.realpath(__file__))
DB_PATH = os.path.join(HERE, "work", "can-db.json")
CONTROL = ("127.0.0.1", 20100)

# CAN ids QtCarSimService sends by itself (firmware 2019.20.4.2, Model 3 defaults)
SIM_IDS = {0x03e, 0x102, 0x103, 0x129, 0x132, 0x204, 0x20d, 0x210, 0x212, 0x214, 0x21d, 0x221, 0x229,
           0x243, 0x249, 0x261, 0x264, 0x292, 0x2a9, 0x2e1, 0x2e8, 0x2f2, 0x320, 0x321, 0x322, 0x339,
           0x340, 0x352, 0x36f, 0x3a1, 0x3c2, 0x3e2, 0x3e3, 0x3f1, 0x3f9, 0x3fa, 0x438, 0x458, 0x4e2,
           0x743, 0x744, 0x7ff}

TPMS_WARN_BAR = 2.2     # soft warning below this (25 % under the Model 3's 2.9 bar)

PRESETS = {
    # what QtCarSimService leaves out: the drive inverter (gear, speed, odometer)
    "parked": {
        "DI_gear": "P", "DI_systemState": "STANDBY", "DI_immobilizerState": "DISARMED",
        "DI_vehicleSpeed": 0, "DI_uiSpeed": 0, "DI_uiSpeedUnits": "MPH",
        "DI_odometer": 12345,
        # with the drive rail on, ESPStatusMessage (0x145) turns ESP on while DI_tcTelltaleOn
        # is invalid, and BRAKE amber unless the iBooster reports READY/ACTUATION
        "DI_tcTelltaleOn": "OFF", "DI_ptcStateUI": "ON",
        "IBST_iBoosterStatus": "READY", "IBST_driverBrakeApply": "BRAKES_NOT_APPLIED",
        # the simulator's tires are at 1.0 bar; a TPMS module that has learned its sensors
        "SIM_tpmsPressureFL": 2.9, "SIM_tpmsPressureFR": 2.9, "SIM_tpmsPressureRL": 2.9, "SIM_tpmsPressureRR": 2.9,
        "TPMS_learningStatus": "PASSED", "TPMS_localisationStatus": "PASSED",
    },
    # the rest change one part of the car; combine them, also at runtime (--set --preset NAME).
    # SIM_* go to QtCarSimService; CAN signals in messages the sim sends (BMS_packCurrent in
    # 0x132) need --sim-port. See vehicle/README.md for what each one drives in QtCar.
    "drive": {"SIM_driveRailOn": "true", "DI_gear": "D", "DI_uiSpeedHighSpeed": 30, "DI_vehicleSpeed": 48},
    "stop": {"DI_gear": "P", "DI_uiSpeedHighSpeed": 0, "DI_vehicleSpeed": 0},
    "rail-off": {"SIM_driveRailOn": "false"},
    "charging": {
        "SIM_chargePortDoor": "true", "SIM_chargerProximity": 2, "SIM_chargePortLatch": "Engaged",
        "SIM_isPilotGood": "true", "SIM_chargeState": "Charging", "SIM_chargeTimeToFull": 1.5,
        "BMS_packCurrent": 30,      # positive = into the pack: charge rate (mi/hr) on the charging screen
    },
    "charge-complete": {"SIM_chargeState": "Complete", "SIM_chargeTimeToFull": 0, "BMS_packCurrent": 0},
    "unplugged": {
        "SIM_chargeState": "Disconnected", "SIM_chargerProximity": 0, "SIM_chargePortLatch": "Disengaged",
        "SIM_isPilotGood": "false", "SIM_chargePortDoor": "false", "SIM_chargeTimeToFull": 0, "BMS_packCurrent": 0,
    },
    "doors-open": {"SIM_driverDoor": "true", "SIM_passFrontDoor": "true", "SIM_driverRearDoor": "true",
                   "SIM_passRearDoor": "true", "SIM_rearTrunk": "true", "SIM_frontTrunk": "true"},
    "doors-closed": {"SIM_driverDoor": "false", "SIM_passFrontDoor": "false", "SIM_driverRearDoor": "false",
                     "SIM_passRearDoor": "false", "SIM_rearTrunk": "false", "SIM_frontTrunk": "false"},
    "locked": {"SIM_vehicleLockState": "true"},
    "unlocked": {"SIM_vehicleLockState": "false"},
    "lights-on": {"SIM_headLights": "true", "SIM_parkingLights": "true"},
    "lights-off": {"SIM_headLights": "false", "SIM_parkingLights": "false", "SIM_highbeamSwitch": "false",
                   "SIM_frontFogLights": "false"},
    # a pressure below TPMS_WARN_BAR also sets that wheel's warning (Sender.set). The warning
    # (telltale, "Tire pressure low") shows with the drive rail on; the image needs
    # [vapi] filter_tpms_faults=false (the kit sets it)
    "tires-ok": {"SIM_tpmsPressureFL": 2.9, "SIM_tpmsPressureFR": 2.9, "SIM_tpmsPressureRL": 2.9, "SIM_tpmsPressureRR": 2.9},
    "tire-low": {"SIM_tpmsPressureFL": 1.7},
    "winter": {"SIM_hvacOutsideTemp": -5, "SIM_hvacInsideTemp": 2},
    "summer": {"SIM_hvacOutsideTemp": 32, "SIM_hvacInsideTemp": 40},
}


# How the "car" answers requests from QtCar. QtCarVehicle sends the UI's requests as CAN frames to
# the gateway (UDP broadcast to :4321, 2-byte header bus << 12 | CAN id, then 8 data bytes); the
# simulator ignores most of them, so tesla-can.py (--respond) plays the ECUs:
# UI signal -> (kind, function(value) -> values to set). "edge": on a change to a non-zero value
# (buttons: frunk, trunk), "level": on every change (switches).
def _toggle(name):
    return lambda v, cur: {name: "false" if str(cur.get(name, "false")).lower() == "true" else "true"}


def _blower(req):
    """The fan speed the car runs at for a requested one (AUTO: 3)."""
    return {"VCRIGHT_hvacBlowerSegment": "3" if req in ("AUTO", "SNA") else req}


RESPONSES = {
    "UI_frunkRequest": ("edge", lambda v, cur: {"SIM_frontTrunk": "true"}),
    "UI_trunkRequest": ("edge", _toggle("SIM_rearTrunk")),        # no power liftgate: open, or close again
    "UI_lockRequest": ("level", lambda v, cur: {"SIM_vehicleLockState": "true"} if v in ("LOCK", "REMOTE_LOCK")
                       else {"SIM_vehicleLockState": "false"} if v in ("UNLOCK", "REMOTE_UNLOCK") else {}),
    "UI_lightSwitch": ("level", lambda v, cur: {
        "ON": {"SIM_headLights": "true", "SIM_parkingLights": "true"},
        "PARKING": {"SIM_headLights": "false", "SIM_parkingLights": "true"},
        "OFF": {"SIM_headLights": "false", "SIM_parkingLights": "false"}}.get(v, {})),
    "UI_frontFogSwitch": ("level", lambda v, cur: {"SIM_frontFogLights": "true" if v == "1" else "false"}),
    # wiper button in the UI -> the front body controller's wiper state (0x2E1, in the sim's frames)
    "UI_wiperRequest": ("level", lambda v, cur: {"VCFRONT_wiperState": {
        "OFF": "PARK", "AUTO": "INT_AUTO_LOW", "SLOW_INTERMITTENT": "INTERMITTENT_LOW",
        "FAST_INTERMITTENT": "INTERMITTENT_HIGH", "SLOW_CONTINUOUS": "CONT_SLOW",
        "FAST_CONTINUOUS": "CONT_FAST"}[v]} if v not in ("SNA",) else {}),
    # climate (UI_hvacRequest 0x2f3) -> the right body controller's feedback (0x243, in the sim's
    # frames), which the climate panel shows (fan speed, A/C, ...). AUTO: what a car might pick.
    # "req:NAME" in cur = the UI's last request of that signal.
    "UI_hvacReqUserPowerState": ("level", lambda v, cur: {"VCRIGHT_hvacPowerState": "OFF", "VCRIGHT_hvacBlowerSegment": "OFF",
                                                          "VCRIGHT_hvacACRunning": "OFF"} if v == "OFF" else
                                 {"VCRIGHT_hvacPowerState": v, **_blower(cur.get("req:UI_hvacReqBlowerSegment", "AUTO"))}),
    "UI_hvacReqBlowerSegment": ("level", lambda v, cur: _blower(v) if cur.get("req:UI_hvacReqUserPowerState") != "OFF" else {}),
    "UI_hvacReqAirDistributionMode": ("level", lambda v, cur: {
        "VCRIGHT_hvacAirDistributionMode": "PANEL_FLOOR" if v == "AUTO" else v.replace("MANUAL_", "")}),
    "UI_hvacReqRecirc": ("level", lambda v, cur: {"VCRIGHT_hvacRecirc": v}),
    "UI_hvacReqSecondRowState": ("level", lambda v, cur: {"VCRIGHT_hvacSecondRowState": v}),
    "UI_hvacReqACDisable": ("level", lambda v, cur: {"VCRIGHT_hvacACRunning": "OFF" if v == "OFF" else "ON"}),
    "UI_openChargePortDoorRequest": ("edge", lambda v, cur: {"SIM_chargePortDoor": "true"}),
    "UI_closeChargePortDoorRequest": ("edge", lambda v, cur: {"SIM_chargePortDoor": "false"}),
}


def decode_signal(frame, s):
    raw = sum(((frame[b // 8] >> (b % 8)) & 1) << k for k, b in enumerate(s["bits"]))
    if s.get("signed") and raw >> (len(s["bits"]) - 1) & 1:
        raw -= 1 << len(s["bits"])
    return s.get("enum", {}).get(str(raw)) or ("%g" % (raw * s["scale"] + s.get("offset", 0)))


class ChargeModel:
    """The battery fills while charging. The simulator keeps its battery level where it is, so
    the charging screen said "+0 mi" (GUI_chargeSessionIdealEnergyAdded compares the energy
    with the session's start). While SIM_chargeState is Charging, SIM_batteryLevel rises by what
    the charge current (BMS_packCurrent, positive = into the pack) delivers at PACK_V into
    PACK_KWH, and the time to full follows; at 100 % the state becomes Complete. The level starts
    from what QtCarVehicle shows."""
    PACK_V, PACK_KWH, EVERY = 360.0, 75.0, 5.0

    def __init__(self, snd):
        self.snd, self.last, self.level = snd, None, None

    def tick(self, now):
        if self.last is not None and now - self.last < self.EVERY:
            return
        dt, self.last = (now - self.last if self.last is not None else 0.0), now
        sim = self.snd.sim.values
        if sim.get("SIM_chargeState") != "Charging":
            self.level = None
            return
        if self.level is None:           # a new session: from the set level, else the car's
            try:
                self.level = float(sim.get("SIM_batteryLevel") or dv_request(4030, "_data_get_value_request_?name=VAPI_batteryLevel", 0.5))
            except (TypeError, ValueError):
                return
            return
        amps = max(0.0, self.snd.values.get("BMS_packCurrent", 0.0))
        self.level = min(100.0, self.level + amps * self.PACK_V * dt / 3600 / 1000 / self.PACK_KWH * 100)
        self.snd.sim.set("SIM_batteryLevel", "%.2f" % self.level)
        if amps > 0:                     # "Time Remaining" (hours) at this rate
            hours = (100 - self.level) / 100 * self.PACK_KWH / (amps * self.PACK_V / 1000)
            self.snd.sim.set("SIM_chargeTimeToFull", "%.2f" % hours)
        if self.level >= 100:
            for k, v in PRESETS["charge-complete"].items():
                self.snd.set(k, v)
            print("car: battery full, charge complete", file=sys.stderr, flush=True)


class Responder:
    """Listens to QtCarVehicle's outgoing frames (:4321): records them for the panel and, with
    respond, answers the UI requests in RESPONSES."""
    PORT = 4321

    def __init__(self, snd, respond=True):
        self.snd = snd
        self.respond = respond
        self.last = {}
        self.watch = {}          # CAN id -> [(signal name, signal)]
        for name in RESPONSES:
            mname, s = snd.sigs[name]
            self.watch.setdefault(snd.db[mname]["id"], []).append((name, s))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.bind(("", self.PORT))
        self.sock.setblocking(False)

    def poll(self):
        while True:
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                return
            if len(data) != 10:
                continue
            cid = int.from_bytes(data[:2], "big") & 0x7ff
            self.snd.record(cid, data[2:], "qtcar", time.monotonic())
            for name, s in self.watch.get(cid, ()) if self.respond else ():
                v = decode_signal(data[2:], s)
                prev = self.last.get(name)
                self.last[name] = v
                if prev is None and RESPONSES[name][0] == "edge" or v == prev:
                    continue
                kind, fn = RESPONSES[name]
                if kind == "edge" and v in ("0", "IDLE", "NONE"):
                    continue
                cur = dict(self.snd.values, **self.snd.sim.values)
                cur.update(("req:" + k, x) for k, x in self.last.items())
                values = fn(v, cur)
                for k, val in values.items():
                    self.snd.set(k, val)
                if values:
                    print("car: %s=%s -> %s" % (name, v, " ".join("%s=%s" % kv for kv in values.items())),
                          file=sys.stderr, flush=True)


def load_db(path):
    if not os.path.exists(path):
        sys.exit("no signal database at %s\nmake it with: vehicle/extract-can-db.py <rootfs>" % path)
    db = json.load(open(path))
    sig = {}
    for mname, m in db.items():
        for sname, s in m["signals"].items():
            sig[sname] = (mname, s)
    return db, sig


def parse_value(s, text):
    enum = s.get("enum", {})
    for k, label in enum.items():
        if label.lower() == text.lower():
            return float(k)
    try:
        return float(text)
    except ValueError:
        sys.exit("bad value %r (enum: %s)" % (text, ", ".join(enum.values()) or "none"))


def encode_into(frame, s, value):
    bits = s["bits"]
    raw = int(round((value - s.get("offset", 0)) / s["scale"])) if s["scale"] else 0
    n = len(bits)
    if s.get("signed"):
        raw = max(-(1 << (n - 1)), min((1 << (n - 1)) - 1, raw)) & ((1 << n) - 1)
    else:
        raw = max(0, min((1 << n) - 1, raw))
    for k, bit in enumerate(bits):
        if raw >> k & 1:
            frame[bit // 8] |= 1 << (bit % 8)
        else:
            frame[bit // 8] &= ~(1 << (bit % 8))


def dv_request(port, query, timeout=2.0):
    """GET a QtCar-framework data value URL (the server keeps the connection open: read by
    Content-Length). Returns the body or None."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
            c.sendall(("GET /%s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n" % query).encode())
            data = b""
            while True:
                head, sep, body = data.partition(b"\r\n\r\n")
                if sep:
                    n = [int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")]
                    if n and len(body) >= n[0]:
                        break
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
        return data.partition(b"\r\n\r\n")[2].decode(errors="replace").strip()
    except OSError:
        return None


class SimValues:
    """SIM_* data values of QtCarSimService: sent when they change, and all of them again every
    few seconds (the sim may start later or restart)."""
    PORT = 4190

    def __init__(self):
        self.values = {}
        self.dirty = set()
        self.event = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def set(self, name, value):
        if self.values.get(name) != value:
            self.values[name] = value
            self.dirty.add(name)
            self.event.set()

    def pop(self, name):
        self.values.pop(name, None)

    def run(self):
        while True:
            changed = self.event.wait(3)
            self.event.clear()
            names = list(self.dirty) if changed else list(self.values)
            self.dirty.clear()
            for k in names:
                if k in self.values:
                    dv_request(self.PORT, "_data_set_value_request_?name=%s&value=%s" % (k, urllib.parse.quote(str(self.values[k]))))


class Sender:
    def __init__(self, db, sigs, target, yield_=True, sim_port=None):
        self.db, self.sigs = db, sigs
        self.values = {}         # signal -> value
        self.lock = threading.Lock()
        self.counters = {}
        self.next_due = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", 0))
        self.own_port = self.sock.getsockname()[1]
        self.target = target
        self.sim = SimValues()
        self.foreign = {}        # CAN id -> last time another sender sent it
        self.by_id = {m["id"]: n for n, m in db.items()}
        self.selectors = {}
        self.sim_ids = {}        # CAN id -> last time it came from the sim (--sim-port)
        self.seen = {}           # CAN id -> the last frames on the wire (record(), for the panel)
        self.muxinfo = {}
        self.simsock = None      # --sim-port: QtCarSimService's frames come in here and are forwarded
        if sim_port:
            self.simsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.simsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.simsock.bind(("", sim_port))
            self.simsock.setblocking(False)
        self.listen = None
        if yield_:
            try:
                self.listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                self.listen.bind(("", target[1]))
                self.listen.setblocking(False)
            except OSError as e:
                print("can't listen on port %d (%s), not yielding to other senders" % (target[1], e), file=sys.stderr)
                self.listen = None

    def watch(self, now):
        """Note CAN ids other senders are sending."""
        while self.listen:
            try:
                data, addr = self.listen.recvfrom(65536)
            except BlockingIOError:
                return
            if addr[1] != self.own_port and len(data) >= 2:
                cid = int.from_bytes(data[:2], "big")
                self.record(cid, data[2:], "other", now)
                if cid not in self.foreign or now - self.foreign[cid] > 2:
                    name = next((n for n, m in self.db.items() if m["id"] == cid), "?")
                    if any(self.sigs[v][0] == name for v in self.values):
                        print("0x%03x %s is sent by someone else now, pausing it" % (cid, name), file=sys.stderr, flush=True)
                self.foreign[cid] = now

    def set(self, name, text):
        if name.startswith("SIM_"):
            self.sim.set(name, str(text).lower() if str(text).lower() in ("true", "false") else str(text))
            if name.startswith("SIM_tpmsPressure"):
                self.tpms_warning(name[-2:], float(text))
            return
        if name not in self.sigs:
            raise KeyError("unknown signal %s (try --find)" % name)
        self.values[name] = parse_value(self.sigs[name][1], str(text))

    def tpms_warning(self, wheel, bar):
        """The TPMS module's soft warning for a wheel (and the global one) from its pressure."""
        self.values["TPMS_softWarning" + wheel] = float(bar < TPMS_WARN_BAR)
        self.values["TPMS_c_globalSoftWarning"] = float(any(self.values.get("TPMS_softWarning" + w) == 1
                                                            for w in ("FL", "FR", "RL", "RR")))

    def checksum(self, m, base, frame):
        """Fill in the checksum byte, if the message has one (VehicleUtils::calculateChecksum)."""
        for sname, s in m["signals"].items():
            if (s["base"] == base or s["base"] == "00" * 8) and sname.endswith("Checksum") \
                    and len(s["bits"]) == 8 and s["bits"][0] % 8 == 0:
                idx = s["bits"][0] // 8
                cid = m["id"]
                frame[idx] = ((cid & 0xff) + (cid >> 8) + sum(b for i, b in enumerate(frame[:m["dlc"]]) if i != idx)) & 0xff
                return

    def selector_bits(self, mname):
        """Bits of a multiplexed message that select the branch (empty if not multiplexed)."""
        if mname not in self.selectors:
            sigs = self.db[mname]["signals"].values()
            zero = "00" * 8
            setbits = {b for s in sigs for b in range(64) if bytes.fromhex(s["base"])[b // 8] >> (b % 8) & 1}
            self.selectors[mname] = sorted({b for s in sigs if s["base"] == zero and set(s["bits"]) & setbits
                                            for b in s["bits"]})
        return self.selectors[mname]

    def mux(self, mname):
        """(selector bits, signals common to all branches) of a message; for unmultiplexed ones
        ([], all signals)."""
        if mname not in self.muxinfo:
            sel = self.selector_bits(mname)
            sigs = self.db[mname]["signals"]
            zero = "00" * 8
            branch_bits = {b for s in sigs.values() if s["base"] != zero for b in s["bits"]}
            # a zero-base signal is branch 0's unless it doesn't overlap any branch's bits
            common = {n for n, s in sigs.items() if not sel or s["base"] == zero and not set(s["bits"]) & branch_bits}
            self.muxinfo[mname] = (sel, common)
        return self.muxinfo[mname]

    @staticmethod
    def branch(sel, data):
        """The multiplexer value of a frame (0 if not multiplexed)."""
        return sum((data[b // 8] >> (b % 8) & 1) << k for k, b in enumerate(sel) if b // 8 < len(data))

    def record(self, cid, data, src, now):
        """Remember the last frame of each CAN id (per multiplexer branch), who sent it and when."""
        e = self.seen.get(cid)
        if e is None:
            e = self.seen[cid] = {"frames": {}, "times": collections.deque(maxlen=20)}
        data = bytes(data).ljust(8, b"\0")
        e.update(src=src, t=now, last=data)
        e["times"].append(now)
        mname = self.by_id.get(cid)
        e["frames"][self.branch(self.mux(mname)[0], data) if mname else 0] = data

    def wire(self, ids, now):
        """For the panel: every CAN id seen [age s, frames/s, sender], and for the given ids the
        frames (hex, per branch) and their decoded signals."""
        wire, msgs = {}, {}
        for cid, e in self.seen.items():
            ts = e["times"]
            dt = ts[-1] - ts[0]
            wire[cid] = [round(now - e["t"], 1), round((len(ts) - 1) / dt, 1) if dt > 0 else 0, e["src"]]
        for cid in ids:
            e, mname = self.seen.get(cid), self.by_id.get(cid)
            if not e or not mname:
                continue
            sel, common = self.mux(mname)
            values = {}
            for sname, s in self.db[mname]["signals"].items():
                f = e["last"] if sname in common else e["frames"].get(self.branch(sel, bytes.fromhex(s["base"])))
                if f is not None:
                    values[sname] = decode_signal(f, s)
            msgs[cid] = {"frames": {k: f.hex() for k, f in sorted(e["frames"].items())}, "values": values}
        return {"wire": wire, "msgs": msgs}

    def compact_db(self):
        """The signal database for the panel: messages sorted by id, signals with their range."""
        out = []
        for mname, m in sorted(self.db.items(), key=lambda x: x[1]["id"]):
            sel, common = self.mux(mname)
            sigs = []
            for sname, s in m["signals"].items():
                n = len(s["bits"])
                lo, hi = (-(1 << (n - 1)), (1 << (n - 1)) - 1) if s.get("signed") else (0, (1 << n) - 1)
                off = s.get("offset", 0)
                d = {"n": sname, "len": n, "min": lo * s["scale"] + off, "max": hi * s["scale"] + off, "scale": s["scale"]}
                if s.get("unit"):
                    d["u"] = s["unit"]
                if s.get("enum"):
                    d["e"] = s["enum"]
                if sname not in common:
                    d["m"] = self.branch(sel, bytes.fromhex(s["base"]))
                sigs.append(d)
            out.append({"id": m["id"], "n": mname, "period": m["period_ms"], "dlc": m["dlc"],
                        "sim": m["id"] in SIM_IDS, "mux": bool(sel), "sigs": sigs})
        return out

    def merge(self, now):
        """Forward QtCarSimService's frames (--sim-port) to the target, with the signals set here
        written over the sim's values."""
        while self.simsock:
            try:
                data, addr = self.simsock.recvfrom(65536)
            except BlockingIOError:
                return
            if len(data) < 3:
                continue
            cid = int.from_bytes(data[:2], "big")
            self.sim_ids[cid] = now
            mname = self.by_id.get(cid)
            if mname:
                frame = bytearray(data[2:].ljust(8, b"\0"))
                sel = self.selector_bits(mname)
                hit = False
                for name, v in self.values.items():
                    mn, s = self.sigs[name]
                    if mn != mname:
                        continue
                    base = bytes.fromhex(s["base"])
                    if all((frame[b // 8] ^ base[b // 8]) >> (b % 8) & 1 == 0 for b in sel):
                        encode_into(frame, s, v)
                        hit = True
                if hit:
                    self.checksum(self.db[mname], None, frame)
                    data = data[:2] + bytes(frame[:len(data) - 2])
            self.sock.sendto(data, self.target)
            self.record(cid, data[2:], "sim", now)

    def frames(self, now):
        """(key, message, bytes) for each message branch with signals set that is due now."""
        groups = {}
        for name, v in self.values.items():
            mname, s = self.sigs[name]
            groups.setdefault((mname, s["base"]), []).append((s, v))
        for (mname, base), items in groups.items():
            key = (mname, base)
            cid = self.db[mname]["id"]
            if now < self.next_due.get(key, 0) or now - self.foreign.get(cid, -9) < 1 or now - self.sim_ids.get(cid, -9) < 1:
                continue    # someone else sends it, or the sim does and merge() writes our values into its frames
            period = self.db[mname]["period_ms"] or 1000
            self.next_due[key] = now + min(max(period, 20), 1000) / 1000.0
            m = self.db[mname]
            frame = bytearray.fromhex(base)
            for s, v in items:
                encode_into(frame, s, v)
            cnt = next((s for sname, s in m["signals"].items() if (s["base"] == base or s["base"] == "00" * 8)
                        and sname.endswith("Counter") and sname not in self.values), None)
            if cnt:
                c = self.counters.get(key, 0)
                encode_into(frame, cnt, c)
                self.counters[key] = (c + 1) % (1 << len(cnt["bits"]))
            self.checksum(m, base, frame)
            yield key, m, bytes(frame)

    def tick(self, now):
        self.watch(now)
        self.merge(now)
        for key, m, frame in self.frames(now):
            self.sock.sendto(struct.pack(">H", m["id"]) + frame, self.target)
            self.record(m["id"], frame, "set", now)


# data values the web page shows (read from QtCarVehicle, port 4030)
PANEL_VALUES = ["VAPI_driveRailOn", "VAPI_shiftState", "VAPI_vehicleSpeed", "VAPI_odometer", "VAPI_batteryLevel",
                "VAPI_ratedRange", "VAPI_doorState", "VAPI_isLocked", "VAPI_isCharging", "VAPI_chargePortLatch",
                "VAPI_chargeTimeToFull", "VAPI_batteryCurrent", "VAPI_headLights", "VAPI_highBeamLights",
                "VAPI_frontFogLights", "HVAC_outsideTemp", "TPMS_pressureFL", "TPMS_pressureFR", "TPMS_pressureRL",
                "TPMS_pressureRR", "VAPI_telltaleESP", "VAPI_telltaleBrakes"]


def run_info(log_dir):
    """Services and helpers of the ./tesla run that writes to log_dir (tools/logsummary.py)."""
    if not log_dir:
        return None
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "tools"))
        import logsummary
        services, helpers = logsummary.service_status(log_dir)
    except Exception as e:    # the panel works without it
        return {"error": str(e)}
    logs = sorted((f, os.path.getsize(os.path.join(log_dir, f))) for f in os.listdir(log_dir)
                  if f.endswith((".log", ".err", ".out")) or f == "config")
    return {"dir": os.path.relpath(log_dir, os.path.join(HERE, "..")), "services": services, "helpers": helpers,
            "logs": logs}


def log_text(log_dir, name, full):
    """A log file of the run as text: the last 400 lines unless full. None if there's no such file."""
    if not log_dir or name != os.path.basename(name) or name.startswith("."):
        return None
    if name == "summary":
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "logsummary.py"), log_dir],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr).encode()
    path = os.path.join(log_dir, name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    if not full:
        lines = data.splitlines(keepends=True)
        if len(lines) > 400:
            data = b"[... %d earlier lines; ?all=1 for everything ...]\n" % (len(lines) - 400) + b"".join(lines[-400:])
    return data


def serve_panel(snd, port, bind="127.0.0.1", log_dir=None):
    """Web page to change values at runtime: vehicle/panel.html + a small JSON API."""
    sigs = snd.sigs

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")    # an old page would send old controls
            if len(data) > 4096 and "gzip" in self.headers.get("Accept-Encoding", ""):
                data = gzip.compress(data, 5)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def state(self):
            with snd.lock:
                values = {k: fmt(sigs, k, v).split("=", 1)[1] for k, v in snd.values.items()}
                values.update(snd.sim.values)
            live = {n: dv_request(4030, "_data_get_value_request_?name=" + n, 0.5) for n in PANEL_VALUES}
            return {"values": values, "live": live, "presets": {k: v for k, v in PRESETS.items()},
                    "owners": dict(snd.owner), "run": run_info(log_dir)}

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.reply(200, open(os.path.join(HERE, "panel.html"), "rb").read(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.reply(200, self.state())
            elif self.path == "/api/db":
                with snd.lock:
                    self.reply(200, snd.compact_db())
            elif self.path.startswith("/api/can"):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ids = [int(i, 0) for i in ",".join(q.get("ids", [])).split(",") if i.strip()]
                with snd.lock:
                    self.reply(200, snd.wire(ids, time.monotonic()))
            elif self.path.startswith("/logs/"):
                name, _, query = self.path[len("/logs/"):].partition("?")
                data = log_text(log_dir, urllib.parse.unquote(name), "all=1" in query)
                if data is None:
                    self.reply(404, {"error": "no such log"})
                else:
                    self.reply(200, data, "text/plain; charset=utf-8")
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                values = dict(PRESETS[req["preset"]]) if req.get("preset") else {}
                values.update(req.get("values", {}))
                with snd.lock:
                    for k, v in values.items():
                        snd.set(k, v)
                        snd.owner.pop(k, None)
                    for k in req.get("unset", []):
                        snd.values.pop(k, None)
                        snd.sim.pop(k)
                if req.get("quiet"):     # the game controls: many small changes, no log, no state
                    self.reply(200, {"ok": True})
                    return
                print("panel: " + " ".join("%s=%s" % kv for kv in values.items()) +
                      "".join(" -%s" % k for k in req.get("unset", [])), file=sys.stderr, flush=True)
                self.reply(200, self.state())
            except (KeyError, ValueError, SystemExit) as e:
                self.reply(400, {"error": str(e)})

    srv = http.server.ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def control(cmd):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(json.dumps(cmd).encode(), CONTROL)
    try:
        return json.loads(s.recv(65536))
    except socket.timeout:
        sys.exit("no running tesla-can.py (control port %d)" % CONTROL[1])


def fmt(sigs, name, v):
    if name not in sigs:
        return "%s=%s" % (name, v)
    s = sigs[name][1]
    label = s.get("enum", {}).get(str(int(v))) if float(v).is_integer() else None
    return "%s=%s" % (name, label or ("%g" % v))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("assign", nargs="*", metavar="SIGNAL=VALUE")
    ap.add_argument("--preset", action="append", default=[], choices=sorted(PRESETS))
    ap.add_argument("--set", action="store_true", help="send SIGNAL=VALUE to the running instance")
    ap.add_argument("--unset", nargs="+", metavar="SIGNAL")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--list-presets", action="store_true")
    ap.add_argument("--respond", action="store_true",
                    help="answer QtCar's requests (frunk/trunk buttons, locks, light switch, ...: RESPONSES)")
    ap.add_argument("--http", type=int, metavar="PORT", help="web page to change values at runtime (vehicle/panel.html)")
    ap.add_argument("--http-bind", default="127.0.0.1", metavar="ADDR",
                    help="address for --http (default 127.0.0.1; 0.0.0.0 = reachable from the network)")
    ap.add_argument("--log-dir", help="the ./tesla run's log folder: the panel shows its services and logs")
    ap.add_argument("--find", metavar="TEXT")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--target", default="127.255.255.255:1234")
    ap.add_argument("--no-yield", action="store_true", help="send even ids someone else is sending")
    ap.add_argument("--sim-port", type=lambda s: int(s) if s else None, metavar="PORT",
                    help="QtCarSimService sends to this port (its --udp :PORT): forward its frames, with "
                         "the signals set here written over its values")
    args = ap.parse_args()

    if args.list_presets:
        for name, values in PRESETS.items():
            print("%-16s %s" % (name, " ".join("%s=%s" % kv for kv in values.items())))
        return
    db, sigs = load_db(args.db)
    if args.find:
        t = args.find.lower()
        for name, (mname, s) in sorted(sigs.items(), key=lambda x: (db[x[1][0]]["id"], x[0])):
            if t in name.lower() or t in mname.lower():
                m = db[mname]
                enum = " [%s]" % " ".join("%s=%s" % kv for kv in s.get("enum", {}).items()) if s.get("enum") else ""
                print("0x%03x %-24s %-36s %s%s%s" % (m["id"], mname + ("*" if m["id"] in SIM_IDS else ""), name,
                                                     s.get("unit", ""), " scale %g" % s["scale"] if s["scale"] != 1 else "", enum))
        print("(* = QtCarSimService sends this message)", file=sys.stderr)
        return
    if args.status:
        r = control({"cmd": "status"})
        for k, v in sorted(r["values"].items()):
            print(fmt(sigs, k, v))
        return
    if args.unset:
        print(control({"cmd": "unset", "names": args.unset}).get("error", "ok"))
        return
    pairs = [a.split("=", 1) for a in args.assign]
    if any(len(p) != 2 for p in pairs):
        ap.error("arguments are SIGNAL=VALUE")
    if args.set:
        values = {k: v for p in args.preset for k, v in PRESETS[p].items()}
        values.update(pairs)
        r = control({"cmd": "set", "values": values})
        print(r.get("error", "ok"))
        return

    host, port = args.target.rsplit(":", 1)
    snd = Sender(db, sigs, (host, int(port)), not args.no_yield, args.sim_port)
    for p in args.preset:
        for k, v in PRESETS[p].items():
            snd.set(k, v)
    for k, v in pairs:
        try:
            snd.set(k, v)
        except KeyError as e:
            sys.exit(str(e))
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.bind(CONTROL)
    ctl.setblocking(False)
    print("sending: " + " ".join(fmt(sigs, k, v) for k, v in sorted(dict(snd.values, **snd.sim.values).items())),
          file=sys.stderr, flush=True)
    snd.owner = {}
    if args.http:
        serve_panel(snd, args.http, args.http_bind, args.log_dir)
    # QtCar's outgoing frames: answered with --respond, shown on the panel
    responder = Responder(snd, args.respond) if args.respond or args.http else None
    charge = ChargeModel(snd)
    try:
        while True:
            with snd.lock:
                snd.tick(time.monotonic())
                charge.tick(time.monotonic())
                if responder:
                    responder.poll()
            try:
                data, addr = ctl.recvfrom(65536)
                cmd = json.loads(data)
                reply = {}
                try:
                    snd.lock.acquire()
                    if cmd["cmd"] == "set":
                        before = dict(snd.values, **snd.sim.values)
                        for k, v in cmd["values"].items():
                            snd.set(k, v)
                            # who set it last (tesla-gps.py --can: "gps"), shown on the panel
                            if cmd.get("source"):
                                snd.owner[k] = cmd["source"]
                            else:
                                snd.owner.pop(k, None)
                        after = dict(snd.values, **snd.sim.values)
                        changed = [k for k in cmd["values"] if before.get(k) != after.get(k)]
                        if changed:
                            print("set: " + " ".join(fmt(sigs, k, after.get(k)) for k in changed), file=sys.stderr, flush=True)
                    elif cmd["cmd"] == "unset":
                        for k in cmd["names"]:
                            snd.values.pop(k, None)
                            snd.sim.pop(k)
                    reply["values"] = dict(snd.values, **snd.sim.values)
                except (KeyError, SystemExit) as e:
                    reply["error"] = str(e)
                finally:
                    snd.lock.release()
                ctl.sendto(json.dumps(reply).encode(), addr)
            except BlockingIOError:
                pass
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
