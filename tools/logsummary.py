#!/usr/bin/env python3
"""Summary of a run's logs (./tesla logs): restarts, crashes, audio problems, GPS, known errors.

  ./tesla logs              the last run (logs/latest)
  ./tesla logs RUN          a folder in logs/ (name, path, or a prefix such as 20260924-15)
  ./tesla logs --list       the runs, newest last
"""
import argparse
import datetime
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(os.path.dirname(HERE), "logs")
# logs of the host-side helpers; every other *.log in a run is a firmware service
HELPER_LOGS = {"qtcar", "can", "gps", "audio", "camera", "x11vnc", "xvfb", "events", "can-db", "audio-clock", "bt-bridge"}
# service restart lines of the loops in ./tesla start (and start_all.sh before it)
EXIT_RE = re.compile(r"^(?:tesla|start_all): (\S+) exited \((\d+)\).*?(?:\[t=(\d+)\])?$")
# known problems: (regex, text); counted per log
PROBLEMS = [
    (r"Failed to listen on port (\d+)", "port {0} busy (a leftover process? ./tesla stop)"),
    (r"Traceback \(most recent call last\)", "Python error (traceback)"),
    (r"Cannot open ring buffer shared memory", "audio: tplug shared memory not writable"),
    (r"Cannot unlink semaphore", "audio: stale tplug semaphore"),
    (r"Segmentation fault|core dumped", "crashed (segmentation fault)"),
    (r"no map tiles in", "no map tiles"),
    (r"Address already in use", "address already in use"),
]
C = {"b": "\033[1m", "red": "\033[31m", "yel": "\033[33m", "grn": "\033[32m", "dim": "\033[2m", "0": "\033[0m"}


def color(on):
    if not on:
        for k in C:
            C[k] = ""


def runs():
    return sorted(d for d in glob.glob(os.path.join(LOGS, "2*")) if os.path.isdir(d))


def find_run(arg):
    if not arg:
        latest = os.path.join(LOGS, "latest")
        if os.path.isdir(latest):
            return os.path.realpath(latest)
        all_runs = runs()
        return all_runs[-1] if all_runs else None
    if os.path.isdir(arg):
        return os.path.realpath(arg)
    match = [d for d in runs() if os.path.basename(d).startswith(arg)]
    return match[-1] if match else None


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def hms(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def duration(s):
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s % 3600 // 60} min"


def service_status(run):
    """What a run started and whether it's up: [{name, bin, up, restarts, log}] for the services
    and QtCar, [{name, up}] for the host-side helpers (pids file). Used by ./tesla status and the
    web panel (vehicle/tesla-can.py --log-dir)."""
    names = []
    for line in read(os.path.join(run, "events.log")).splitlines():
        m = re.match(r"\d+ \S+ running: (.*)", line)
        if m:
            names = m.group(1).split()
    running = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    running.add(os.path.basename(f.read().split(b"\0")[0]).decode(errors="replace"))
            except OSError:
                pass
    services = []
    for name in names + ["qtcar"]:
        text = read(os.path.join(run, name + ".log"))
        if name == "qtcar":
            binary = "QtCar"
            restarts = sum(1 for l in read(os.path.join(run, "events.log")).splitlines() if " qtcar exit " in l)
        else:
            m = re.search(r"^qtcar-service: \S+ -> (\S+)", text, re.M)
            binary = m.group(1) if m else {"valhalla": "valhalla_server", "audioweaver": "AWE_command_line_tesla",
                                           "audiod": "audiod", "dbus": "dbus-daemon",
                                           "a2dpbridge": "alsaloop"}.get(name, name)
            restarts = sum(1 for l in text.splitlines() if EXIT_RE.match(l))
        services.append({"name": name, "bin": binary, "up": binary in running, "restarts": restarts,
                         "log": name + ".log"})
    helpers = []
    for line in read(os.path.join(run, "pids")).splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, start, name = parts[0], parts[1], " ".join(parts[2:])
        try:
            with open(f"/proc/{pid}/stat") as f:
                up = f.read().rsplit(") ", 1)[1].split()[19] == start
        except (OSError, IndexError):
            up = False
        helpers.append({"name": name, "up": up})
    return services, helpers


def print_status(run):
    services, helpers = service_status(run)
    color(sys.stdout.isatty())
    rel = os.path.relpath(run, os.getcwd())
    print(f"{C['b']}Services{C['0']}")
    for s in services:
        state = f"{C['grn']}up  {C['0']}" if s["up"] else f"{C['red']}DOWN{C['0']}"
        extra = f"{s['restarts']} restart(s)" if s["restarts"] else ""
        if not s["up"]:
            extra += f"  see {rel}/{s['log']}"
        print(f"  {state} {s['name']:20} {extra}")
    print(f"{C['b']}Helpers{C['0']}")
    for h in helpers:
        state = f"{C['grn']}up  {C['0']}" if h["up"] else f"{C['red']}DOWN{C['0']}"
        print(f"  {state} {h['name']}")


class Summary:
    def __init__(self, run):
        self.run = run
        self.lines = []     # (label, text, level)

    def add(self, label, text, level="ok"):
        self.lines.append((label, text, level))

    def times(self):
        """start/stop from events.log, else from the folder name and the newest file."""
        events = read(os.path.join(self.run, "events.log")).splitlines()
        self.events = [e.split(" ", 2) for e in events if e.count(" ") >= 2]
        self.start = self.stop = None
        for t, _, what in self.events:
            if what.startswith("start") and self.start is None:
                self.start = int(t)
            if what == "stop":
                self.stop = int(t)
        if self.start is None:
            try:
                self.start = datetime.datetime.strptime(os.path.basename(self.run), "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                self.start = min(os.path.getmtime(f) for f in glob.glob(self.run + "/*") or [self.run])
        self.running = self.stop is None and os.path.realpath(os.path.join(LOGS, "current")) == self.run
        if self.stop is None:
            files = glob.glob(self.run + "/*")
            self.stop = max([os.path.getmtime(f) for f in files] + [self.start])

    def qtcar(self):
        exits = [w for _, _, w in self.events if w.startswith("qtcar exit")]
        log = read(os.path.join(self.run, "qtcar.log"))
        errors = len(re.findall(r"\t\[[^]]*\] ERROR ", log))
        if not exits and not self.events:     # an older run without events.log
            n = log.count("CID UI is restarting")
            self.add("QtCar", f"{n} restart(s) to apply settings" if n else "no restarts seen", "ok")
            return
        restarts, crashes = [], []
        for w in exits:
            m = re.match(r"qtcar exit (\d+)(?:: (.*))?", w)
            code, why = m.group(1), m.group(2)
            if why:
                restarts.append(why.replace("CID UI is restarting reason: ", ""))
            elif code not in ("130", "137") or w is not exits[-1]:
                crashes.append(code)
        text = "ran without restarts"
        level = "ok"
        if restarts:
            text = f"restarted {len(restarts)}x to apply settings ({restarts[0][:80]})"
        if crashes:
            sig = {"139": "SIGSEGV", "134": "SIGABRT", "137": "SIGKILL"}
            text = f"exited unexpectedly {len(crashes)}x (exit {', '.join(c + ('/' + sig[c] if c in sig else '') for c in crashes)})" \
                + (f"; restarted {len(restarts)}x for settings" if restarts else "")
            level = "bad"
        self.add("QtCar", f"{text}; {errors} ERROR lines in qtcar.log", level)

    def services(self):
        names, restarts = [], {}
        for path in sorted(glob.glob(os.path.join(self.run, "*.log"))):
            name = os.path.basename(path)[:-4]
            if name in HELPER_LOGS:
                continue
            names.append(name)
            lines = read(path).rstrip("\n").splitlines()
            for i, line in enumerate(lines):
                m = EXIT_RE.match(line)
                if not m:
                    continue
                t = int(m.group(3)) if m.group(3) else None
                if t is not None and self.stop and t >= self.stop - 2 and not self.running:
                    continue    # stopped at the end of the run
                if t is None and i >= len(lines) - 2 and not self.running:
                    continue    # older logs without times: the last exit is most likely the stop
                restarts.setdefault(name, []).append((m.group(2), t))
        if not names:
            self.add("Services", "none started", "ok")
            return
        silent = [n for n in names if not read(os.path.join(self.run, n + ".log")).strip()]
        if silent:
            self.add("Services", "no output at all (hung before starting?): " + " ".join(silent), "bad")
        if not restarts:
            self.add("Services", f"{len(names)}, no restarts: {' '.join(names)}", "ok")
            return
        parts = []
        for name, ex in restarts.items():
            codes = sorted({c for c, _ in ex})
            when = [hms(t) for _, t in ex if t][:3]
            parts.append(f"{name} {len(ex)}x (exit {','.join(codes)}{', at ' + ' '.join(when) if when else ''})")
        self.add("Services", f"{len(names)}; restarted: " + "; ".join(parts), "warn")

    def kernel(self):
        since = datetime.datetime.fromtimestamp(self.start - 5).strftime("%Y-%m-%d %H:%M:%S")
        until = datetime.datetime.fromtimestamp(self.stop + 5).strftime("%Y-%m-%d %H:%M:%S")
        try:
            out = subprocess.run(["journalctl", "-k", "-q", "--no-pager", "-o", "short", "--since", since,
                                  "--until", until], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            self.add("Kernel", "journalctl not available", "warn")
            return
        crashes = {}
        for line in out.splitlines():
            m = re.search(r"kernel: (\S+)\[\d+\]: segfault at \S+ .*? in (\S+?)\[([0-9a-f]+)", line)
            if m:
                key = f"{m.group(1)} in {m.group(2)}[{m.group(3)}]"
            elif re.search(r"traps: |general protection|Out of memory|oom-kill", line):
                key = line.split("kernel: ", 1)[-1][:100]
            else:
                continue
            crashes[key] = crashes.get(key, 0) + 1
        if crashes:
            self.add("Kernel", "segfault: " + "; ".join(f"{k}{f' ({n}x)' if n > 1 else ''}" for k, n in crashes.items()), "bad")
        else:
            self.add("Kernel", "no segfaults", "ok")

    def audio(self):
        audiod = os.path.join(self.run, "audiod.log")
        if not os.path.exists(audiod) and not os.path.exists(os.path.join(self.run, "audio.log")):
            return
        notes, level = [], "ok"
        pump = len(re.findall(r"Pump error", read(audiod)))
        if pump > 3:    # one or two at startup are normal
            notes.append(f"{pump} pump error(s) in audiod.log")
            level = "warn" if pump < 30 else "bad"
        host = read(os.path.join(self.run, "audio.log"))
        clipped = sum(int(n) for n in re.findall(r"clipped (\d+) samples", host))
        if clipped:
            notes.append(f"sox clipped {clipped} samples")
            if clipped > 1000:
                level = "warn"
        for pattern, text in [(r"underrun", "underruns"), (r"overrun", "overruns")]:
            n = len(re.findall(pattern, host, re.I))
            if n:
                notes.append(f"{n} {text} (audio.log)")
                level = "warn"
        n = sum(1 for t in re.findall(r"audio pipeline exited.*?\[t=(\d+)\]", host)
                if self.running or int(t) < self.stop - 3)     # not the exit at Ctrl+C / stop
        if n:
            notes.append(f"host pipeline restarted {n}x (arecord | sox | pw-play)")
            level = "warn"
        self.add("Audio", "; ".join(notes) or "no problems", level)

    def gps(self):
        log = read(os.path.join(self.run, "gps.log"))
        if not log.strip():
            return
        status = [l for l in log.splitlines() if "sentences in" in l]
        route = [l for l in log.splitlines() if l.startswith("route:")]
        if status:
            last = status[-1]
            fix = "LOC_geoValidFix=true" in last
            rmc = re.search(r"RMC status (\w)", last)
            sats = re.search(r"sats (\d+)", last)
            hint = re.search(r"\[(.*)\]", last)
            fixes = sum("LOC_geoValidFix=true" in l for l in status)
            text = (f"receiver: RMC {rmc.group(1) if rmc else '?'}, {sats.group(1) if sats else '?'} sats; "
                    f"GpsManager fix in {fixes}/{len(status)} status lines")
            if hint:
                text += f"; last: {hint.group(1)}"
            self.add("GPS", text, "ok" if fix else "warn")
        elif route:
            self.add("GPS", route[-1], "ok")
        else:
            self.add("GPS", log.strip().splitlines()[-1][:120], "ok")

    def camera(self):
        lines = read(os.path.join(self.run, "camera.log")).strip().splitlines()
        msgs = [l for l in lines if not l.startswith("tesla: camera ")]     # the pauses during games
        pauses = sum(1 for l in lines if l.startswith("tesla: camera off"))
        if msgs:
            self.add("Camera", f"{len(msgs)} ffmpeg messages, last: {msgs[-1][:100]}", "warn")
        elif pauses:
            self.add("Camera", f"off during {pauses} game(s)", "ok")

    def problems(self):
        found = []
        for path in sorted(glob.glob(os.path.join(self.run, "*.log")) + glob.glob(os.path.join(self.run, "*.err"))):
            name = os.path.basename(path)
            if name == "audiod.log":
                text = "\n".join(l for l in read(path).splitlines() if not l.startswith(("TIO", "TXX", "TMG")))
            else:
                text = read(path)
            for pattern, desc in PROBLEMS:
                hits = re.findall(pattern, text)
                if hits:
                    groups = hits[0] if isinstance(hits[0], tuple) else (hits[0],)
                    found.append(f"{name}: {desc.format(*groups)}{f' ({len(hits)}x)' if len(hits) > 1 else ''}")
            if name.endswith(".err"):     # stderr of a helper: only real errors, not "stopped"
                errs = [l for l in text.splitlines() if re.search(r"error|failed|panic", l, re.I)
                        and "X connection" not in l]
                if errs:
                    found.append(f"{name}: {errs[-1][:100]}")
        if found:
            for f in found:
                self.add("Problem", f, "bad")

    def features(self):
        conf = read(os.path.join(self.run, "config"))
        if not conf:
            return
        vals = dict(re.match(r"(\w+)=(\S*)", l).groups() for l in conf.splitlines() if re.match(r"\w+=", l))
        on = [k.lower() for k in ("VEHICLE", "AUDIO", "BLUETOOTH") if vals.get(k) == "1"]
        if vals.get("SCREEN") == "native":
            on.append("native screen")
        on += [f"{k.lower()} {vals[k]}" for k in ("GPS", "MUSIC", "CAMERA") if vals.get(k) not in (None, "", "''")]
        self.add("Features", ", ".join(on) or "none (only QtCar)", "ok")

    def build(self):
        self.times()
        self.features()
        self.qtcar()
        self.services()
        self.kernel()
        self.audio()
        self.gps()
        self.camera()
        self.problems()

    def print(self, short=False):
        rel = os.path.relpath(self.run, os.getcwd())
        state = "still running" if self.running else f"until {hms(self.stop)}"
        print(f"{C['b']}Run {rel}{C['0']}: {hms(self.start)} {state} ({duration(self.stop - self.start)})")
        mark = {"ok": C["grn"] + "ok " + C["0"], "warn": C["yel"] + "!! " + C["0"], "bad": C["red"] + "!! " + C["0"]}
        for label, text, level in self.lines:
            print(f"  {mark[level]}{label:9} {text}")
        if not short:
            size = sum(os.path.getsize(f) for f in glob.glob(self.run + "/*") if os.path.isfile(f))
            print(f"  {C['dim']}Logs: {rel}/ ({size / 1e6:.1f} MB); for a problem report send this summary and the folder{C['0']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 prog="./tesla logs")
    ap.add_argument("run", nargs="?", help="log folder (default: the last run)")
    ap.add_argument("--list", action="store_true", help="list the runs")
    ap.add_argument("--short", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    color(sys.stdout.isatty())
    if args.list:
        for d in runs():
            s = Summary(d)
            s.times()
            print(f"  {os.path.basename(d)}  {duration(s.stop - s.start):>12}")
        return 0
    run = find_run(args.run)
    if not run:
        sys.exit(f"no run {args.run or ''} in {LOGS}")
    if args.status:
        print_status(run)
        return 0
    s = Summary(run)
    s.build()
    s.print(args.short)
    return 0


if __name__ == "__main__":
    sys.exit(main())
