#!/usr/bin/env python3
"""Read and write QtCar's persisted data values (QtCarSettings.db) while QtCar is not running.

QtCar keeps GUI_*, FEATURE_* and a few other data values in
/home/tesla/.Tesla/data/QtCarSettings.db (table data, key "DataValues/<name>", value = QVariant
in QDataStream format). They survive restarts and config changes.

  qtcar-settings.py DB                         list all stored data values
  qtcar-settings.py DB NAME                    show one
  qtcar-settings.py DB NAME=VALUE [...]        set (true/false, integers, decimals, else text)
  qtcar-settings.py DB -d NAME                 delete (QtCar falls back to its default)

A missing DB is created with QtCar's schema (use --owner to give it to the tesla user).
"""
import argparse, os, sqlite3, struct, sys, time

SCHEMA = ("CREATE TABLE IF NOT EXISTS data (key TEXT PRIMARY KEY,value BLOB,time INTEGER,version INTEGER);"
          "CREATE INDEX IF NOT EXISTS data_key_idx on data (key);")
# QVariant type ids (Qt 4)
T_BOOL, T_INT, T_UINT, T_LONGLONG, T_DOUBLE, T_STRING = 1, 2, 3, 4, 6, 10


def decode(blob):
    if not blob or len(blob) < 5:
        return None
    t, null = struct.unpack(">IB", blob[:5])
    d = blob[5:]
    if null or t == 0:
        return None
    if t == T_BOOL:
        return bool(d[0])
    if t == T_INT:
        return struct.unpack(">i", d[:4])[0]
    if t == T_UINT:
        return struct.unpack(">I", d[:4])[0]
    if t == T_LONGLONG:
        return struct.unpack(">q", d[:8])[0]
    if t == T_DOUBLE:
        return struct.unpack(">d", d[:8])[0]
    if t == T_STRING:
        n = struct.unpack(">I", d[:4])[0]
        return "" if n == 0xFFFFFFFF else d[4:4 + n].decode("utf-16-be")
    return "<type %d: %s>" % (t, d.hex())


def encode(value):
    if isinstance(value, bool):
        return struct.pack(">IB?", T_BOOL, 0, value)
    if isinstance(value, int):
        return struct.pack(">IBi", T_INT, 0, value)
    if isinstance(value, float):
        return struct.pack(">IBd", T_DOUBLE, 0, value)
    s = value.encode("utf-16-be")
    return struct.pack(">IBI", T_STRING, 0, len(s)) + s


def parse_value(text):
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    for conv in (int, float):
        try:
            return conv(text)
        except ValueError:
            pass
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("items", nargs="*", help="NAME or NAME=VALUE")
    ap.add_argument("-d", "--delete", action="append", default=[], metavar="NAME")
    ap.add_argument("--owner", help="uid:gid for a newly created DB")
    args = ap.parse_args()

    new = not os.path.exists(args.db)
    if new and not any("=" in i for i in args.items):
        sys.exit("%s does not exist" % args.db)
    if new:
        os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    now = int(time.time())
    for name in args.delete:
        con.execute("DELETE FROM data WHERE key=?", ("DataValues/" + name,))
    for item in args.items:
        if "=" in item:
            name, val = item.split("=", 1)
            con.execute("INSERT OR REPLACE INTO data VALUES (?,?,?,1)", ("DataValues/" + name, encode(parse_value(val)), now))
        else:
            row = con.execute("SELECT value FROM data WHERE key=?", ("DataValues/" + item,)).fetchone()
            print("%s = %s" % (item, "<not stored>" if row is None else decode(row[0])))
    if not args.items and not args.delete:
        for key, val in con.execute("SELECT key, value FROM data WHERE key LIKE 'DataValues/%' ORDER BY key"):
            print("%s = %s" % (key[len("DataValues/"):], decode(val)))
    con.commit()
    con.close()
    if new and args.owner:
        uid, gid = (int(x) for x in args.owner.split(":"))
        os.chown(args.db, uid, gid)
        os.chown(os.path.dirname(os.path.abspath(args.db)), uid, gid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
