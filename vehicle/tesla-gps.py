#!/usr/bin/env python3
"""Fake GPS for QtCar: sends NMEA sentences to QtCarGpsManager over UDP.

On the car the GPS receiver's NMEA stream arrives as UDP on port 63277 (`--udpgps` in
/etc/RunQtCar.vars). QtCarGpsManager (service qtcar-gpsmanager) parses $GPRMC/$GPGGA from any
sender and publishes the fix to QtCar. The chroot shares the host's network, so this runs on the
host.

  tesla-gps.py --pos 37.4419,-122.1430                    stand still (heading with --heading)
  tesla-gps.py --pos 37.4419,-122.1430 --to 37.7793,-122.4193
                                                          drive there along a Valhalla route
  tesla-gps.py --gpx track.gpx                            drive along a GPX track
  tesla-gps.py --path "37.44,-122.14 37.45,-122.15 ..."   drive along straight segments
  tesla-gps.py --uart /dev/ttyUSB0 [--baud 9600]         pass a real GPS receiver's NMEA through

Routes (--to) are calculated with the firmware's own valhalla_client on the installed map tiles
(--rootfs, default: the mounted image chroot/, else sandbox/rootfs). Speed: the route's own
speeds by default, or --speed km/h; --speedup multiplies time. At the end it stays at the last
point (or starts over with --loop).
"""
import argparse, datetime, json, math, os, socket, subprocess, sys, termios, threading, time, tty
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
EARTH_R = 6371008.8


def checksum(body):
    c = 0
    for ch in body:
        c ^= ord(ch)
    return "$%s*%02X\r\n" % (body, c)


def nmea_coord(value, is_lat):
    a = abs(value)
    deg = int(a)
    minutes = (a - deg) * 60
    if is_lat:
        return "%02d%07.4f" % (deg, minutes), "N" if value >= 0 else "S"
    return "%03d%07.4f" % (deg, minutes), "E" if value >= 0 else "W"


# satellites in view: (prn, elevation, azimuth, snr)
SATS = [(2, 62, 45, 44), (5, 48, 120, 42), (7, 35, 200, 40), (9, 71, 300, 45), (13, 22, 80, 36),
        (15, 55, 260, 43), (18, 15, 160, 33), (20, 40, 20, 41), (26, 28, 330, 38), (29, 66, 180, 46)]


def nmea_sentences(lat, lon, speed_ms, heading, alt):
    """One epoch in the order GpsManager needs: it publishes a fix on RMC, and only once GGA and a
    complete GSV set (satellites in view) have come in since the last one."""
    t = datetime.datetime.now(datetime.timezone.utc)
    hms = t.strftime("%H%M%S.") + "%02d" % (t.microsecond // 10000)
    la, ns = nmea_coord(lat, True)
    lo, ew = nmea_coord(lon, False)
    knots = speed_ms * 1.943844
    out = ["GPGGA,%s,%s,%s,%s,%s,1,%02d,0.8,%.1f,M,-30.0,M,," % (hms, la, ns, lo, ew, len(SATS), alt),
           "GPGSA,A,3,%s,1.4,0.8,1.1" % ",".join(["%02d" % s[0] for s in SATS[:12]] + [""] * (12 - len(SATS[:12])))]
    groups = [SATS[i:i + 4] for i in range(0, len(SATS), 4)]
    for n, g in enumerate(groups, 1):
        out.append("GPGSV,%d,%d,%02d,%s" % (len(groups), n, len(SATS), ",".join("%02d,%02d,%03d,%02d" % s for s in g)))
    out.append("GPVTG,%.1f,T,,M,%.2f,N,%.2f,K,A" % (heading % 360, knots, speed_ms * 3.6))
    out.append("GPRMC,%s,A,%s,%s,%s,%s,%.2f,%.1f,%s,,,A" % (hms, la, ns, lo, ew, knots, heading % 360, t.strftime("%d%m%y")))
    return "".join(checksum(s) for s in out)


def dv_request(port, query, timeout=1.0):
    """GET a QtCar-framework data value URL, return the body (None if unreachable). The server
    keeps the connection open, so read by Content-Length."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
            c.sendall(("GET /%s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n" % query).encode())
            data = b""
            while b"\r\n\r\n" not in data or len(data.split(b"\r\n\r\n", 1)[1]) < length(data):
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
        return data.split(b"\r\n\r\n", 1)[-1].decode(errors="replace").strip()
    except OSError:
        return None


def length(data):
    for line in data.split(b"\r\n\r\n", 1)[0].split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":")[1])
    return 1 << 30


class RawGpsKeeper:
    """Keep GpsManager's LOC_estimatedGPSValid false, so QtCar shows the raw GPS position.

    GpsManager's estimator sets LOC_estimatedGPSValid=true whenever it starts, and QtCar then shows
    the estimate (which doesn't follow GPS without vehicle speed) even with GUI_smoothGPSUpdates=false.
    It only resets the flag when LOC_enableSmoothGPSUpdates changes, which doesn't happen when that
    setting is already stored as false. QtCar also stores the flag in its settings DB."""
    PORTS = (4160, 4220)    # GpsManager, QtCar (which keeps its own copy of the flag)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            for port in self.PORTS:
                if dv_request(port, "_data_get_value_request_?name=LOC_estimatedGPSValid", 5) == "true":
                    dv_request(port, "_data_set_value_request_?name=LOC_estimatedGPSValid&value=false", 5)
            time.sleep(2)


class CanLink:
    """Drive state for a running tesla-can.py (its control port), so QtCar shows the car driving:
    gear, speed and odometer follow the fake GPS. Set twice: as QtCarSimService values (with the
    drive rail on the sim simulates the drive inverter itself) and as DI_* CAN signals, which
    tesla-can.py sends only when no sim does."""
    CONTROL = ("127.0.0.1", 20100)

    def __init__(self, odometer_km=None, hysteresis=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last = 0.0
        self.odo = odometer_km
        self.pos = None
        # a real receiver standing still reports a few tenths of a knot and wanders by metres:
        # with hysteresis the car only counts as moving above 1 m/s (until it drops below 0.5)
        self.hysteresis = hysteresis
        self.moving = False
        self.sent = {}

    def update(self, now, lat, lon, speed_ms):
        if self.hysteresis:
            self.moving = speed_ms > (0.5 if self.moving else 1.0)
            if not self.moving:
                speed_ms = 0.0
        else:
            self.moving = speed_ms > 0.1
        if self.pos and self.moving:
            self.odo = (self.odo or 0) + distance(self.pos, (lat, lon)) / 1000
        self.pos = (lat, lon)
        if now - self.last < 0.5:
            return
        self.last = now
        kmh, mph = speed_ms * 3.6, speed_ms * 3.6 / 1.609344
        moving = self.moving
        vals = {
            # the drive rail (HV for driving) on: the UI shows the speed, not just the gear
            "SIM_driveRailOn": "true", "SIM_shiftState": "D" if moving else "P", "SIM_vehicleSpeed": round(mph, 1),
            "DI_gear": "D" if moving else "P", "DI_vehicleSpeed": round(kmh, 2), "DI_uiSpeed": round(mph),
            "DI_uiSpeedHighSpeed": round(mph), "DI_systemState": "ENABLE" if moving else "STANDBY"}
        if self.odo is not None:
            vals["DI_odometer"] = round(self.odo, 3)
            vals["SIM_batteryOdometer"] = round(self.odo / 1.609344, 3)
        # only what changed: tesla-can.py keeps the values, and whatever the web panel changed
        # (gear, drive rail, ...) stays until the drive state here changes again
        vals = {k: v for k, v in vals.items() if self.sent.get(k) != v}
        if not vals:
            return
        self.sent.update(vals)
        self.sock.sendto(json.dumps({"cmd": "set", "values": vals, "source": "gps"}).encode(), self.CONTROL)


BAUDS = {r: getattr(termios, "B%d" % r) for r in (4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800)
         if hasattr(termios, "B%d" % r)}


def open_uart(dev, baud):
    """Open a serial port raw (8N1) at baud; anything that isn't a tty (FIFO, pty test) as is."""
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = BAUDS[baud]
        attrs[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    return fd


def nmea_to_deg(value, hemi):
    if not value:
        return None
    deg_len = value.index(".") - 2
    d = float(value[:deg_len]) + float(value[deg_len:]) / 60
    return -d if hemi in ("S", "W") else d


class UartStats:
    """What the receiver sends, and whether GpsManager makes a fix of it: a line every 30 s (also
    with -q, it goes to logs/<time>/gps.log)."""
    EVERY = 30

    def __init__(self):
        self.reset(time.monotonic())
        self.fix = self.sats = self.hdop = None
        threading.Thread(target=self.run, daemon=True).start()

    def reset(self, now):
        self.t0, self.count, self.types = now, 0, {}

    def add(self, f):
        self.count += 1
        kind = f[0][3:] if len(f[0]) >= 6 else f[0]
        self.types[kind] = self.types.get(kind, 0) + 1
        if kind == "RMC" and len(f) > 2:
            self.fix = f[2]
        elif kind == "GGA" and len(f) > 8:
            self.sats, self.hdop = f[7], f[8]

    def run(self):
        while True:
            time.sleep(self.EVERY)
            now = time.monotonic()
            types = " ".join("%s:%d" % kv for kv in sorted(self.types.items()))
            valid = dv_request(4160, "_data_get_value_request_?name=LOC_geoValidFix", 2)
            notes = []
            if not self.count:
                notes.append("no NMEA from the receiver")
            elif "GSV" not in self.types:
                notes.append("no GSV: GpsManager needs GGA + a full GSV set + RMC per fix")
            if valid is None:
                notes.append("GpsManager not answering on :4160 (not running, or an old one holds the port)")
            print("%d sentences in %ds (%s)  RMC status %s  sats %s  HDOP %s  GpsManager LOC_geoValidFix=%s%s"
                  % (self.count, now - self.t0, types or "-", self.fix, self.sats, self.hdop, valid,
                     "".join("  [%s]" % n for n in notes)), file=sys.stderr, flush=True)
            self.reset(now)


def uart_passthrough(args, sock, target, can):
    """Forward every NMEA sentence from a serial GPS receiver to GpsManager, unchanged. With --can,
    position and speed from its RMC sentences drive the vehicle data like a simulated drive."""
    if args.baud not in BAUDS:
        sys.exit("unsupported baud rate %d (%s)" % (args.baud, ", ".join(map(str, sorted(BAUDS)))))
    last_print = 0.0
    stats = UartStats()
    while True:
        try:
            fd = open_uart(args.uart, args.baud)
        except OSError as e:
            print("%s: %s, retrying" % (args.uart, e), file=sys.stderr, flush=True)
            time.sleep(2)
            continue
        print("reading NMEA from %s at %d baud" % (args.uart, args.baud), file=sys.stderr, flush=True)
        buf = b""
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:            # EOF: unplugged, or the end of a file
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    line = raw.strip(b"\r\x00 ").decode("ascii", "replace")
                    start = line.find("$")
                    if start < 0:
                        continue
                    line = line[start:]
                    sock.sendto((line + "\r\n").encode(), target)
                    f = line.split("*")[0].split(",")
                    stats.add(f)
                    if f[0][3:] == "RMC" and len(f) > 7 and f[2] == "A":
                        lat, lon = nmea_to_deg(f[3], f[4]), nmea_to_deg(f[5], f[6])
                        v = float(f[7] or 0) / 1.943844
                        if can and lat is not None and lon is not None:
                            can.update(time.monotonic(), lat, lon, v)
                        now = time.monotonic()
                        if not args.quiet and now - last_print >= 1:
                            last_print = now
                            print("\r%.6f,%.6f  %5.1f km/h   " % (lat, lon, v * 3.6), end="", file=sys.stderr, flush=True)
        except OSError as e:
            print("\n%s: %s" % (args.uart, e), file=sys.stderr, flush=True)
        os.close(fd)
        time.sleep(1)


def distance(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return math.degrees(math.atan2(y, x)) % 360


def decode_polyline(s, precision=6):
    coords, idx, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while idx < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        coords.append((lat / f, lon / f))
    return coords


def parse_point(text):
    lat, lon = (float(v) for v in text.replace(" ", "").split(",")[:2])
    return lat, lon


def default_rootfs():
    for p in (os.path.join(REPO, "chroot"), os.path.join(REPO, "sandbox", "rootfs")):
        if os.path.isdir(os.path.join(p, "opt", "navigon", "tm")) and os.listdir(os.path.join(p, "opt", "navigon", "tm")):
            return p
    return None


def valhalla_route(rootfs, start, dest):
    """Route with the firmware's valhalla_client on the host. Returns [(lat, lon, speed m/s)]."""
    tm = os.path.join(rootfs, "opt", "navigon", "tm")
    region = next((d for d in sorted(os.listdir(tm)) if len(d) == 2), None)
    tiles = os.path.join(tm, region or "", "valhalla")
    loader = os.path.join(rootfs, "lib64", "ld-linux-x86-64.so.2")
    libs = ":".join(os.path.join(rootfs, p) for p in ("usr/proto2/lib", "usr/lib", "lib"))
    req = {"locations": [{"lat": start[0], "lon": start[1]}, {"lat": dest[0], "lon": dest[1]}],
           "costing": "auto", "directions_options": {"units": "kilometers"}}
    cmd = [loader, "--library-path", libs, os.path.join(rootfs, "usr/bin/valhalla_client"), "-a", "route",
           "--config", os.path.join(rootfs, "usr/tesla/UI/assets/tesla_maps/valhalla.json"),
           "--tile_dir", tiles, "-j", json.dumps(req)]
    # clean environment: the host's locale settings crash the firmware's glibc
    out = subprocess.run(cmd, capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    try:
        trip = json.loads(out.stdout)["trip"]
        leg = trip["legs"][0]
    except (ValueError, KeyError, IndexError):
        sys.exit("valhalla_client failed:\n" + (out.stdout + out.stderr)[-2000:])
    shape = decode_polyline(leg["shape"])
    speeds = [None] * len(shape)
    for m in leg["maneuvers"]:
        v = m["length"] * 1000 / m["time"] if m.get("time") else 0
        for i in range(m["begin_shape_index"], min(m["end_shape_index"] + 1, len(shape))):
            speeds[i] = v
    s = leg["summary"]
    print("route: %.1f km, %d min, %d maneuvers" % (s["length"], s["time"] / 60, len(leg["maneuvers"])), file=sys.stderr)
    return [(la, lo, v or 13.9) for (la, lo), v in zip(shape, speeds)]


def gpx_points(path):
    pts = []
    for el in ET.parse(path).iter():
        if el.tag.split("}")[-1] in ("trkpt", "rtept") and "lat" in el.attrib:
            pts.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
    if not pts:
        sys.exit("no trkpt/rtept in " + path)
    return pts


class Drive:
    """Position along a polyline over time."""

    def __init__(self, points, speed_override):
        self.pts = [p[:2] for p in points]
        self.speed = [speed_override or (p[2] if len(p) > 2 else 13.9) for p in points]
        self.seg = [distance(a, b) for a, b in zip(self.pts, self.pts[1:])]
        self.i, self.along = 0, 0.0
        self.heading = bearing(self.pts[0], self.pts[1]) if len(self.pts) > 1 else 0.0

    def done(self):
        return self.i >= len(self.seg)

    def step(self, dt):
        """Advance dt seconds; returns (lat, lon, speed, heading)."""
        v = 0.0
        left = dt
        while not self.done() and left > 0:
            v = max(self.speed[self.i], 0.5)
            need = (self.seg[self.i] - self.along) / v
            if need > left:
                self.along += v * left
                left = 0
            else:
                left -= need
                self.i += 1
                self.along = 0.0
        if self.done():
            return self.pts[-1][0], self.pts[-1][1], 0.0, self.heading
        a, b = self.pts[self.i], self.pts[self.i + 1]
        f = self.along / self.seg[self.i] if self.seg[self.i] else 0
        if self.seg[self.i] > 0.5:
            self.heading = bearing(a, b)
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, v, self.heading


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pos", type=parse_point, help="position LAT,LON (start of --to)")
    ap.add_argument("--to", type=parse_point, help="drive from --pos to LAT,LON along a Valhalla route")
    ap.add_argument("--gpx", help="drive along a GPX track/route")
    ap.add_argument("--path", help='drive along straight lines: "LAT,LON LAT,LON ..."')
    ap.add_argument("--uart", metavar="DEV", help="pass NMEA from a serial GPS receiver through (e.g. /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=9600, help="baud rate for --uart (default 9600)")
    ap.add_argument("--heading", type=float, default=0.0, help="heading in degrees when standing still")
    ap.add_argument("--alt", type=float, default=30.0, help="altitude in m")
    ap.add_argument("--speed", type=float, help="fixed speed in km/h instead of the route's")
    ap.add_argument("--speedup", type=float, default=1.0, help="time factor for driving (e.g. 5)")
    ap.add_argument("--rate", type=float, default=5.0, help="fixes per second (default 5)")
    ap.add_argument("--loop", action="store_true", help="start the drive over at the end")
    ap.add_argument("--rootfs", help="firmware root with map tiles, for --to (default chroot/ or sandbox/rootfs)")
    ap.add_argument("--target", default="127.0.0.1:63277", help="GpsManager UDP address (default 127.0.0.1:63277)")
    ap.add_argument("--can", action="store_true",
                    help="also set gear, speed and odometer in a running tesla-can.py (vehicle data)")
    ap.add_argument("--odometer", type=float, default=12345, help="start odometer in km for --can (default 12345)")
    ap.add_argument("--estimate", action="store_true",
                    help="leave GpsManager's position estimate on (default: force raw GPS, see RawGpsKeeper)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if args.uart:
        points = None
    elif args.to:
        if not args.pos:
            ap.error("--to needs --pos as the start")
        rootfs = args.rootfs or default_rootfs()
        if not rootfs:
            ap.error("no map tiles found; give --rootfs")
        points = valhalla_route(rootfs, args.pos, args.to)
    elif args.gpx:
        points = gpx_points(args.gpx)
    elif args.path:
        points = [parse_point(p) for p in args.path.split()]
    elif args.pos:
        points = [args.pos]
    else:
        ap.error("give --pos, --to, --gpx, --path or --uart")

    host, port = args.target.rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if not args.estimate:
        RawGpsKeeper().start()
    can = CanLink(args.odometer, hysteresis=bool(args.uart)) if args.can else None
    if args.uart:
        try:
            uart_passthrough(args, sock, (host, int(port)), can)
        except KeyboardInterrupt:
            print(file=sys.stderr)
        return
    speed = args.speed / 3.6 if args.speed else None
    drive = Drive(points, speed)
    drive.heading = args.heading if len(points) == 1 else drive.heading
    period = 1.0 / args.rate
    last_print = 0.0
    try:
        while True:
            lat, lon, v, hdg = drive.step(period * args.speedup)
            sock.sendto(nmea_sentences(lat, lon, v, hdg, args.alt).encode(), (host, int(port)))
            if can:
                can.update(time.monotonic(), lat, lon, v)
            now = time.monotonic()
            if not args.quiet and now - last_print >= 1:
                last_print = now
                print("\r%.6f,%.6f  %5.1f km/h  heading %3.0f   " % (lat, lon, v * 3.6, hdg), end="", file=sys.stderr, flush=True)
            if drive.done() and args.loop and len(points) > 1:
                drive = Drive(points, speed)
            time.sleep(period)
    except KeyboardInterrupt:
        print(file=sys.stderr)


if __name__ == "__main__":
    main()
