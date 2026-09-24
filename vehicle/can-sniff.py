#!/usr/bin/env python3
"""Listen to the CAN-over-UDP traffic between the (simulated) gateway and QtCarVehicle.

Each datagram on port 1234 (and 31415) is one CAN frame: 2 bytes CAN id (big endian), 8 data
bytes. Some messages are longer (Ethernet-only signals); those are printed as they are.

  can-sniff.py                  print every frame as it changes (id, data)
  can-sniff.py --summary 10     listen 10 s, then list ids with count and last data
  can-sniff.py --id 0x3f5,0x7ff only these ids
  can-sniff.py --decode ...     decode signals with vehicle/work/can-db.json
"""
import argparse, json, os, select, socket, sys, time

DB_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "work", "can-db.json")


def decoder(path):
    by_id = {}
    for mname, m in json.load(open(path)).items():
        by_id[m["id"]] = (mname, m)

    def decode(cid, data):
        if cid not in by_id:
            return ""
        mname, m = by_id[cid]
        out = []
        for sname, s in m["signals"].items():
            base = bytes.fromhex(s["base"])
            # only signals of the branch this frame is in (mux bits of the base must match)
            if any(base[i] & ~data[i] for i in range(8)) or not s.get("bits"):
                continue
            raw = sum(((data[b // 8] >> (b % 8)) & 1) << k for k, b in enumerate(s["bits"]))
            if s.get("signed") and raw >> (len(s["bits"]) - 1) & 1:
                raw -= 1 << len(s["bits"])
            v = raw * s["scale"] + s.get("offset", 0)
            label = s.get("enum", {}).get(str(raw))
            out.append("%s=%s" % (sname, label or ("%g" % v)))
        return mname + " " + " ".join(out)
    return decode


def open_sock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ports", default="1234,31415")
    ap.add_argument("--summary", type=float, metavar="SECONDS")
    ap.add_argument("--id", help="comma separated CAN ids to show")
    ap.add_argument("--all", action="store_true", help="print repeated frames too")
    ap.add_argument("--decode", action="store_true", help="decode signals (vehicle/work/can-db.json)")
    args = ap.parse_args()
    decode = decoder(DB_PATH) if args.decode else (lambda cid, data: "")
    ids = {int(x, 0) for x in args.id.split(",")} if args.id else None
    socks = {open_sock(int(p)): int(p) for p in args.ports.split(",")}
    last, count = {}, {}
    end = time.time() + args.summary if args.summary else None
    while end is None or time.time() < end:
        r, _, _ = select.select(list(socks), [], [], 0.5)
        for s in r:
            data, addr = s.recvfrom(65536)
            port = socks[s]
            if len(data) < 2:
                continue
            cid = int.from_bytes(data[:2], "big")
            if ids and cid not in ids:
                continue
            key = (port, cid if len(data) == 10 else (cid, len(data)))
            count[key] = count.get(key, 0) + 1
            if args.summary is None and (args.all or last.get(key) != data):
                print("%.3f %5d 0x%03x %s%s  %s" % (time.time() % 1000, port, cid, data[2:].hex(" "),
                                                    "" if len(data) == 10 else "  (%d bytes)" % len(data),
                                                    decode(cid & 0x7ff, data[2:10]) if len(data) == 10 else ""), flush=True)
            last[key] = data
    for key in sorted(last, key=str):
        port, cid = key
        d = last[key]
        print("%5d 0x%03x x%-4d %s  %s" % (port, cid if isinstance(cid, int) else cid[0], count[key], d[2:34].hex(" "),
                                          decode(cid & 0x7ff, d[2:10]) if isinstance(cid, int) else ""))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
