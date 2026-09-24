#!/usr/bin/env python3
"""Bridge a pseudo terminal (a UART with H4 framing, as the firmware's bsa_server expects) to a
Bluetooth adapter's raw HCI user channel. The adapter must be down and unused by BlueZ.

  hci-bridge.py [--dev 0] [--link PATH] [-v]
"""
import argparse, ctypes, os, pty, select, socket, sys, termios, tty

AF_BLUETOOTH, BTPROTO_HCI, HCI_CHANNEL_USER = 31, 1, 1
libc = ctypes.CDLL(None, use_errno=True)


class SockaddrHci(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("dev", ctypes.c_ushort), ("channel", ctypes.c_ushort)]


def open_user_channel(dev):
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    addr = SockaddrHci(AF_BLUETOOTH, dev, HCI_CHANNEL_USER)
    if libc.bind(s.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
        e = ctypes.get_errno()
        sys.exit(f"bind hci{dev} user channel: {os.strerror(e)} (adapter up or used by bluetoothd?)")
    return s


# H4 packet: type byte, then a header whose length field gives the payload size
HEADERS = {1: (3, lambda h: h[2]),                   # command: opcode(2) plen(1)
           2: (4, lambda h: h[2] | h[3] << 8),       # ACL: handle(2) dlen(2)
           3: (3, lambda h: h[2]),                   # SCO: handle(2) dlen(1)
           4: (2, lambda h: h[1]),                   # event (not expected from the host side)
           5: (4, lambda h: (h[2] | h[3] << 8) & 0x3fff),  # ISO
           7: (2, lambda h: 0)}                      # Broadcom LM diagnostics ("07 f0 01"): dropped
DROP = {7}


def packets(buf):
    """Split complete H4 packets off the front of buf; returns (packets, rest)."""
    out = []
    while buf:
        t = buf[0]
        if t not in HEADERS:
            print(f"bridge: unknown H4 type {t:#x}, dropping a byte", file=sys.stderr)
            buf = buf[1:]
            continue
        hlen, plen = HEADERS[t]
        if len(buf) < 1 + hlen:
            break
        total = 1 + hlen + plen(buf[1:1 + hlen])
        if len(buf) < total:
            break
        out.append(buf[:total])
        buf = buf[total:]
    return out, buf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=int, default=0)
    ap.add_argument("--link", default="/tmp/hci-bridge-tty")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    s = open_user_channel(a.dev)
    master, slave = pty.openpty()
    tty.setraw(slave)
    name = os.ttyname(slave)
    try:
        os.unlink(a.link)
    except FileNotFoundError:
        pass
    os.symlink(name, a.link)
    os.chmod(name, 0o666)
    print(f"ready: {name} (link {a.link}) <-> hci{a.dev}", flush=True)
    buf = b""
    while True:
        r, _, _ = select.select([master, s], [], [])
        if master in r:
            try:
                data = os.read(master, 4096)
            except OSError:
                data = b""
            if not data:
                continue
            if a.v:
                print(f"raw> {data.hex()}", file=sys.stderr, flush=True)
            pk, buf = packets(buf + data)
            for p in pk:
                if a.v:
                    print(f"> {p[:24].hex()}" + (" (dropped)" if p[0] in DROP else ""), file=sys.stderr, flush=True)
                if p[0] not in DROP:
                    s.send(p)
        if s in r:
            p = s.recv(4096)
            if a.v:
                print(f"< {p[:24].hex()}", file=sys.stderr, flush=True)
            os.write(master, p)


if __name__ == "__main__":
    main()
