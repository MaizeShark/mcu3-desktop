#!/usr/bin/env python3
"""Extract the CAN signal database of the firmware (message ids, bit layouts, scaling, enums).

The firmware has no DBC file, but libQtCarVAPI's GUICanCracker::crackMessage() decodes every
CAN message QtCar knows (for the CAN viewer in the diagnostics UI): for each id it pulls the
signals out of the 8 data bytes and calls CANDataManager::storeSignalValue(hash, value, valid).
This script runs that function in an emulator (Unicorn) on test frames: all zeros, then each bit
set on its own. Which signal changes and by how much gives each signal's bits, scale, offset and
sign. Multiplexed messages are explored branch by branch (a bit that makes new signals appear
opens a new branch). Names, units, message ids/periods and enum values come from the tables in
libQtCarCANData.

  extract-can-db.py ROOTFS [-o vehicle/work/can-db.json]

Needs: pip install unicorn pyelftools. The result is derived from the firmware, so it stays out
of git (vehicle/work/).
"""
import argparse, json, math, os, struct, sys
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

LIB = "usr/tesla/UI/lib"


class Elf:
    def __init__(self, path):
        self.path = path
        self.data = open(path, "rb").read()
        self.elf = ELFFile(open(path, "rb"))
        self.segs = [s for s in self.elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        self.dynsym = self.elf.get_section_by_name(".dynsym")
        self.syms = {}
        for s in self.dynsym.iter_symbols():
            if s["st_value"]:
                self.syms[s.name] = (s["st_value"], s["st_size"])
        self.rel = {}
        for sec in self.elf.iter_sections():
            if isinstance(sec, RelocationSection):
                for r in sec.iter_relocations():
                    sym = self.dynsym.get_symbol(r["r_info_sym"]).name if r["r_info_sym"] else None
                    self.rel[r["r_offset"]] = (sym, r["r_addend"])

    def off(self, va):
        for s in self.segs:
            if s["p_vaddr"] <= va < s["p_vaddr"] + s["p_filesz"]:
                return s["p_offset"] + va - s["p_vaddr"]
        raise KeyError(hex(va))

    def u32(self, va):
        return struct.unpack("<I", self.data[self.off(va):self.off(va) + 4])[0]

    def i64(self, va):
        return struct.unpack("<q", self.data[self.off(va):self.off(va) + 8])[0]

    def cstr(self, va):
        o = self.off(va)
        return self.data[o:self.data.index(b"\0", o)].decode("latin-1")

    def ptr(self, va):
        """(symbol, addend) of a relocated pointer, or (None, target address)."""
        r = self.rel.get(va)
        return r if r else (None, 0)


def read_tables(candata):
    """Messages and signals from libQtCarCANData: ETH_messages + ETH_<msg>_signals + Diag maps."""
    va, size = candata.syms["ETH_messages"]
    msgs = []
    for a in range(va, va + size, 48):
        name = candata.cstr(candata.ptr(a)[1])
        cid, dlc = candata.u32(a + 8), candata.u32(a + 12)
        period, nsig = candata.u32(a + 16), candata.u32(a + 20)
        sym, add = candata.ptr(a + 40 - 8)
        sigs = []
        if sym and sym in candata.syms:
            sva = candata.syms[sym][0] + add
            for i in range(nsig):
                e = sva + 40 * i
                sname = candata.cstr(candata.ptr(e)[1])
                h = candata.u32(e + 8)
                unit = candata.cstr(candata.ptr(e + 16)[1])
                msym, madd = candata.ptr(e + 32)
                enum = {}
                if msym and msym in candata.syms:
                    m = candata.syms[msym][0] + madd
                    for k in range(64):
                        label = candata.cstr(candata.ptr(m + 16 * k + 8)[1])
                        if not label:
                            break
                        enum[str(candata.i64(m + 16 * k) & 0xffffffff)] = label
                sigs.append({"name": sname, "hash": h, "unit": unit, "enum": enum})
        msgs.append({"name": name, "id": cid, "dlc": dlc, "period_ms": period, "signals": sigs})
    return msgs


class Cracker:
    """GUICanCracker::crackMessage(this, bus, id, data) in Unicorn."""
    STACK, DATA, THIS, STOP = 0x7f0000000000, 0x7f1000000000, 0x7f2000000000, 0x7f3000000000

    def __init__(self, vapi):
        from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE, UC_PROT_ALL
        from unicorn import x86_const as X
        self.X = X
        self.vapi = vapi
        mu = self.mu = Uc(UC_ARCH_X86, UC_MODE_64)
        for s in vapi.segs:
            lo = s["p_vaddr"] & ~0xfff
            hi = (s["p_vaddr"] + s["p_memsz"] + 0xfff) & ~0xfff
            mu.mem_map(lo, hi - lo, UC_PROT_ALL)
            mu.mem_write(s["p_vaddr"], vapi.data[s["p_offset"]:s["p_offset"] + s["p_filesz"]])
        for base in (self.STACK, self.DATA, self.THIS, self.STOP):
            mu.mem_map(base, 0x100000, UC_PROT_ALL)
        self.func = vapi.syms["_ZN13GUICanCracker12crackMessageEiiPh"][0]
        # PLT stubs: find them by the call targets named in the disassembly (objdump-free: via .rela.plt)
        plt = {}
        relaplt = vapi.elf.get_section_by_name(".rela.plt")
        pltsec = vapi.elf.get_section_by_name(".plt")
        for i, r in enumerate(relaplt.iter_relocations()):
            name = vapi.dynsym.get_symbol(r["r_info_sym"]).name
            plt[pltsec["sh_addr"] + 16 * (i + 1)] = name
        self.plt = plt
        self.stores = []
        mu.hook_add(UC_HOOK_CODE, self._hook, begin=pltsec["sh_addr"], end=pltsec["sh_addr"] + pltsec["sh_size"])

    def _hook(self, mu, addr, size, user):
        X = self.X
        name = self.plt.get(addr)
        if name == "_ZN14CANDataManager16storeSignalValueEidbi":
            h = mu.reg_read(X.UC_X86_REG_ESI)
            valid = mu.reg_read(X.UC_X86_REG_EDX) & 0xff
            xmm0 = mu.reg_read(X.UC_X86_REG_XMM0)
            val = struct.unpack("<d", struct.pack("<Q", xmm0 & 0xffffffffffffffff))[0]
            self.stores.append((h, val, bool(valid)))
            rax = 0
        elif name == "_ZN14CANDataManager8instanceEv":
            rax = self.THIS
        else:
            rax = 0
        # emulate ret
        rsp = mu.reg_read(X.UC_X86_REG_RSP)
        ret = struct.unpack("<Q", mu.mem_read(rsp, 8))[0]
        mu.reg_write(X.UC_X86_REG_RSP, rsp + 8)
        mu.reg_write(X.UC_X86_REG_RAX, rax)
        mu.reg_write(X.UC_X86_REG_RIP, ret)

    def run(self, cid, data8):
        X, mu = self.X, self.mu
        self.stores = []
        mu.mem_write(self.DATA, bytes(data8) + b"\0" * 8)
        rsp = self.STACK + 0x80000
        mu.mem_write(rsp, struct.pack("<Q", self.STOP))
        mu.reg_write(X.UC_X86_REG_RSP, rsp)
        mu.reg_write(X.UC_X86_REG_RDI, self.THIS)
        mu.reg_write(X.UC_X86_REG_RSI, 3)          # bus 3: the one crackMessage handles
        mu.reg_write(X.UC_X86_REG_RDX, cid)
        mu.reg_write(X.UC_X86_REG_RCX, self.DATA)
        mu.emu_start(self.func, self.STOP, count=200000)
        out = {}
        for h, v, ok in self.stores:
            out.setdefault(h, (v, ok))
        return out


def analyse(cr, cid, dlc, want=(), maxbases=1024):
    """Explore branches; returns {hash: {"base": bytes, "bits": [...], "scale", "offset", "signed"}}.
    want: the hashes the message should have; if some are still missing after the bit-flip search
    (mux values that need several bits at once), every value of each small signal is tried."""
    nbits = 8 * max(dlc, 1)
    zero = bytes(8)
    bases = [zero]
    seen_sets = {frozenset(cr.run(cid, zero))}
    result = {}
    i = 0
    tried_mux = False
    while True:
        if i >= len(bases) or i >= maxbases:
            missing = set(want) - set(result)
            if not missing or tried_mux:
                break
            tried_mux = True
            for h, L in list(result.items()):
                bits = L.get("bits") or []
                if not 0 < len(bits) <= 8 or L["base"] != zero.hex():
                    continue
                for v in range(1 << len(bits)):
                    d = bytearray(8)
                    for k, bit in enumerate(bits):
                        if v >> k & 1:
                            d[bit // 8] |= 1 << (bit % 8)
                    s = frozenset(cr.run(cid, d))
                    if s not in seen_sets:
                        seen_sets.add(s)
                        bases.append(bytes(d))
            continue
        base = bases[i]
        i += 1
        b0 = cr.run(cid, base)
        flips = {}
        for bit in range(nbits):
            d = bytearray(base)
            d[bit // 8] ^= 1 << (bit % 8)
            r = cr.run(cid, d)
            flips[bit] = r
            s = frozenset(r)
            if s not in seen_sets and len(bases) < maxbases:
                seen_sets.add(s)
                bases.append(bytes(d))
        for h, (v0, _) in b0.items():
            if h in result:
                continue
            contrib = {}
            for bit, r in flips.items():
                if h in r and r[h][0] != v0 and not (math.isnan(r[h][0]) or math.isinf(r[h][0])):
                    set_in_base = bool(base[bit // 8] >> (bit % 8) & 1)
                    delta = r[h][0] - v0
                    contrib[bit] = -delta if set_in_base else delta
            if not contrib:
                result[h] = {"base": base.hex(), "bits": [], "scale": 0, "offset": v0, "signed": False, "const": True}
                continue
            mags = sorted(abs(c) for c in contrib.values())
            scale = mags[0]
            order = {}
            ok = True
            for bit, c in contrib.items():
                k = math.log2(abs(c) / scale)
                if abs(k - round(k)) > 1e-6:
                    ok = False
                order[bit] = int(round(k))
            if not ok or len(set(order.values())) != len(order):
                result[h] = {"base": base.hex(), "nonlinear": True, "bitset": sorted(contrib)}
                continue
            n = max(order.values()) + 1
            bits = [None] * n
            for bit, k in order.items():
                bits[k] = bit
            top = bits[-1]
            signed = contrib[top] < 0
            # value at base = offset + scale * (raw bits of this signal set in base)
            raw_base = sum(1 << k for bit, k in order.items() if base[bit // 8] >> (bit % 8) & 1)
            if signed and raw_base >> (n - 1) & 1:
                raw_base -= 1 << n
            offset = v0 - scale * raw_base
            result[h] = {"base": base.hex(), "bits": bits, "scale": scale, "offset": offset, "signed": signed}
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rootfs")
    ap.add_argument("-o", "--output", default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "work", "can-db.json"))
    ap.add_argument("--only", help="comma separated message names (for testing)")
    args = ap.parse_args()
    candata = Elf(os.path.join(args.rootfs, LIB, "libQtCarCANData.so"))
    vapi = Elf(os.path.join(args.rootfs, LIB, "libQtCarVAPI.so"))
    msgs = read_tables(candata)
    cr = Cracker(vapi)
    only = set(args.only.split(",")) if args.only else None
    out = {}
    nsig = nok = 0
    for m in msgs:
        if only and m["name"] not in only:
            continue
        lay = analyse(cr, m["id"], m["dlc"], [s["hash"] for s in m["signals"]])
        sigs = {}
        for s in m["signals"]:
            nsig += 1
            L = lay.get(s["hash"])
            e = {"unit": s["unit"]} if s["unit"] else {}
            if s["enum"]:
                e["enum"] = s["enum"]
            if L is None:
                e["missing"] = True
            else:
                e.update(L)
                if L.get("bits"):
                    nok += 1
                    if float(L["scale"]).is_integer():
                        e["scale"] = int(L["scale"])
                    if float(L["offset"]).is_integer():
                        e["offset"] = int(L["offset"])
            sigs[s["name"]] = e
        out[m["name"]] = {"id": m["id"], "dlc": m["dlc"], "period_ms": m["period_ms"], "signals": sigs}
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print("%d messages, %d signals, %d with a bit layout -> %s" % (len(out), nsig, nok, args.output), file=sys.stderr)


if __name__ == "__main__":
    main()
