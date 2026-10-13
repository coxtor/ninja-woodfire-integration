#!/usr/bin/env python3
"""Extract the Ninja Woodfire local key bundle from YOUR OWN copy of the app.

This tool ships **no key material**. It recovers the keys by emulating the app's
own key routine inside *your own copy* of its native library (from an app package
you are licensed to use) and reading the compiled-in constants out of emulated
memory. It then runs the key-derivation maths (an algorithm, not a key) with your
grill's Bluetooth address to produce the three slots the integration needs,
printed as one base64 "key bundle".

It auto-detects everything: it finds libgrillcore.so inside an .apkm/.apk, finds
the key routine and malloc in the library itself (no hard-coded offsets), and can
find your grill's address over Bluetooth. So it is not tied to one app version.

Needs:  pip install unicorn capstone cryptography
        (plus `bleak` if you let it auto-detect the grill's address)

Usage:
    # point it at the APKM/APK you downloaded (or an extracted .so);
    # power the grill on and it finds the address itself:
    python3 extract_ninja_keys.py --app ninja_1.28.0.apkm
    # or give the address / a specific .so / the key-init address yourself:
    python3 extract_ninja_keys.py --app libgrillcore.so --mac AA:BB:CC:DD:EE:FF
"""
from __future__ import annotations

import argparse
import base64
import io
import struct
import sys
import zipfile

try:
    import capstone
    from unicorn import (Uc, UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE,
                         UC_HOOK_MEM_UNMAPPED)
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
                                     UC_ARM64_REG_X30, UC_ARM64_REG_SP, UC_ARM64_REG_PC)
except ImportError:
    sys.exit("needs unicorn + capstone:  pip install unicorn capstone")
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 0x1000
STACK_BASE = 0x7000_0000
STACK_SIZE = 0x200000
HEAP_BASE = 0x1000_0000
HEAP_SIZE = 0x400000
KDF_MALLOC_SIZE = 0x180   # the key routine's first allocation (the big const table)

# Constant buffers the key routine allocates, in order (size -> name). slot0's
# nonce is read from code; everything before it is read from emulated memory.
_WANT = [(0x180, "pv8"), (0x20, "slot5k"), (0x10, "slot5n"),
         (0x20, "c32"), (0x10, "c16"), (0x20, "slot0k")]


def _align_down(x): return x & ~(PAGE - 1)
def _align_up(x): return (x + PAGE - 1) & ~(PAGE - 1)


# --------------------------------------------------------------------------- #
# Input: pull libgrillcore.so out of an .apkm / .apk / .so
# --------------------------------------------------------------------------- #
def _so_from_apk(apk_bytes: bytes) -> bytes | None:
    z = zipfile.ZipFile(io.BytesIO(apk_bytes))
    for n in z.namelist():
        if n.endswith("libgrillcore_android.so") and "arm64" in n:
            return z.read(n)
    return None


def load_grillcore(path: str) -> bytes:
    """Return the arm64 libgrillcore.so bytes from a .so, .apk or .apkm."""
    raw = open(path, "rb").read()
    if raw[:4] == b"\x7fELF":
        return raw
    if raw[:2] != b"PK":
        sys.exit(f"{path} is neither an ELF .so nor a zip (.apk/.apkm)")
    # a .apk directly containing the lib?
    direct = _so_from_apk(raw)
    if direct:
        return direct
    # an .apkm: look through the split APKs
    z = zipfile.ZipFile(io.BytesIO(raw))
    for n in z.namelist():
        if n.endswith(".apk"):
            so = _so_from_apk(z.read(n))
            if so:
                return so
    sys.exit("no arm64 libgrillcore_android.so found inside that package")


# --------------------------------------------------------------------------- #
# ELF parsing + auto-locating malloc@plt, the key routine and the .text delta
# --------------------------------------------------------------------------- #
def _sections(data: bytes) -> dict:
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shes = struct.unpack_from("<H", data, 0x3a)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3c)[0]
    e_shstr = struct.unpack_from("<H", data, 0x3e)[0]
    secs = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shes
        secs.append(dict(
            name=struct.unpack_from("<I", data, o)[0],
            addr=struct.unpack_from("<Q", data, o + 16)[0],
            off=struct.unpack_from("<Q", data, o + 24)[0],
            size=struct.unpack_from("<Q", data, o + 32)[0],
            entsize=struct.unpack_from("<Q", data, o + 56)[0]))
    strtab = secs[e_shstr]

    def nm(n):
        e = data.index(b"\0", strtab["off"] + n)
        return data[strtab["off"] + n:e].decode()
    return {nm(s["name"]): s for s in secs}


def _text_delta(data: bytes) -> int:
    """vaddr - fileoff of the executable (r-x) LOAD segment."""
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phes = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    for i in range(e_phnum):
        o = e_phoff + i * e_phes
        if struct.unpack_from("<I", data, o)[0] == 1 and (struct.unpack_from("<I", data, o + 4)[0] & 1):
            va = struct.unpack_from("<Q", data, o + 0x10)[0]
            fo = struct.unpack_from("<Q", data, o + 0x08)[0]
            return va - fo
    return 0


def _find_malloc_plt(data: bytes, secs: dict) -> int:
    dsym, dstr, rela, plt = secs[".dynsym"], secs[".dynstr"], secs[".rela.plt"], secs[".plt"]

    def symname(n):
        e = data.index(b"\0", dstr["off"] + n)
        return data[dstr["off"] + n:e].decode()
    midx = None
    for i in range(dsym["size"] // dsym["entsize"]):
        o = dsym["off"] + i * dsym["entsize"]
        if symname(struct.unpack_from("<I", data, o)[0]) == "malloc":
            midx = i
            break
    if midx is None:
        sys.exit("could not find malloc in the library")
    for j in range(rela["size"] // rela["entsize"]):
        r_info = struct.unpack_from("<Q", data, rela["off"] + j * rela["entsize"] + 8)[0]
        if (r_info >> 32) == midx:
            return plt["addr"] + 0x20 + j * 0x10  # AArch64 PLT: 0x20 header, 0x10/entry
    sys.exit("could not find malloc@plt")


def _find_kdf_entry(data: bytes, secs: dict, malloc_plt: int) -> int:
    """The key routine: the function whose first big allocation is malloc(0x180)."""
    text = secs[".text"]
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    code = data[text["off"]:text["off"] + text["size"]]
    bl_targets, recent_180, site = [], None, None
    for ins in md.disasm(code, text["addr"]):
        m, ops = ins.mnemonic, ins.op_str
        if m == "bl" and ops.startswith("#"):
            t = int(ops[1:], 16)
            bl_targets.append(t)
            if t == malloc_plt and recent_180 is not None and ins.address - recent_180 < 0x20:
                site = recent_180
                break
        elif m in ("mov", "movz") and ops.startswith("w0,") and ops.replace(" ", "").endswith(f",#{hex(KDF_MALLOC_SIZE)}"):
            recent_180 = ins.address
    if site is None:
        sys.exit("could not locate the key routine (no malloc(0x180) site); "
                 "unsupported build — pass --kdf with its address")
    below = [t for t in bl_targets if t <= site]
    if not below:
        sys.exit("could not resolve the key routine entry; pass --kdf")
    return max(below)


# --------------------------------------------------------------------------- #
# Emulate the key routine, read the constants, derive the keys
# --------------------------------------------------------------------------- #
def _have_all(mallocs) -> bool:
    need = {}
    for sz, _ in _WANT:
        need[sz] = need.get(sz, 0) + 1
    have = {}
    for sz, *_ in mallocs:
        have[sz] = have.get(sz, 0) + 1
    return all(have.get(sz, 0) >= n for sz, n in need.items())


def _read_slot0(data: bytes, call_site: int, text_delta: int) -> tuple[bytes, bytes]:
    """Read slot0's 4 u64 key + 2 u64 nonce, built with mov/movz/movk + str [x0,#off]."""
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    f = call_site - text_delta
    regs, words = {}, []
    for ins in md.disasm(data[f:f + 0x400], call_site):
        m, ops = ins.mnemonic, [o.strip() for o in ins.op_str.split(",")]
        if m in ("mov", "movz") and len(ops) >= 2 and ops[1].startswith("#"):
            v = int(ops[1][1:], 0)
            if "lsl" in ins.op_str:
                v <<= int(ins.op_str.split("lsl")[1].replace("#", "").strip(), 0)
            regs[ops[0]] = v & 0xFFFFFFFFFFFFFFFF
        elif m == "movk" and len(ops) >= 2:
            sh = int(ins.op_str.split("lsl")[1].replace("#", "").strip(), 0) if "lsl" in ins.op_str else 0
            imm = int(ops[1][1:], 0)
            regs[ops[0]] = ((regs.get(ops[0], 0) & ~(0xFFFF << sh)) | (imm << sh)) & 0xFFFFFFFFFFFFFFFF
        elif m == "str" and len(ops) >= 2 and ops[0] in regs and ops[1].startswith("[x0"):
            words.append(regs[ops[0]])
            if len(words) >= 6:
                break
    if len(words) < 6:
        sys.exit(f"slot-0 constant read incomplete ({len(words)}/6)")
    return (b"".join(struct.pack("<Q", w) for w in words[:4]),
            b"".join(struct.pack("<Q", w) for w in words[4:6]))


def _extract_constants(data: bytes, kdf: int, malloc_plt: int, text_delta: int) -> dict:
    uc = Uc(UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN)
    _map_segments(uc, data)
    uc.mem_map(STACK_BASE - STACK_SIZE, STACK_SIZE)
    uc.mem_map(HEAP_BASE, HEAP_SIZE)
    uc.reg_write(UC_ARM64_REG_SP, STACK_BASE - 0x1000)

    scratch = HEAP_BASE + HEAP_SIZE - 0x2000
    uc.mem_write(scratch, b"\x00" * 0x400)
    uc.mem_write(scratch + 0x8, struct.pack("<Q", scratch + 0x100))
    seed_slice = scratch + 0x80
    uc.mem_write(seed_slice, struct.pack("<QQQ", 0, scratch + 0x180, 32))
    uc.mem_write(scratch + 0x180, bytes(range(32)))
    uc.reg_write(UC_ARM64_REG_X0, scratch)
    uc.reg_write(UC_ARM64_REG_X1, seed_slice)

    st = {"next": HEAP_BASE, "mallocs": [], "done": False}

    def bump(size):
        a = st["next"]
        st["next"] = _align_up(a + max(size, 0x10) + 0x10)
        return a

    def hook_code(uc, address, size, _):
        if st["done"]:
            uc.emu_stop(); return
        try:
            word = struct.unpack("<I", uc.mem_read(address, 4))[0]
        except Exception:
            return
        if (word & 0xFC000000) != 0x94000000:
            return
        imm = word & 0x03FFFFFF
        if imm & 0x02000000:
            imm -= 0x04000000
        target = (address + imm * 4) & 0xFFFFFFFFFFFFFFFF
        ret = (address + 4) & 0xFFFFFFFFFFFFFFFF
        if target == malloc_plt:
            sz = uc.reg_read(UC_ARM64_REG_X0)
            buf = bump(sz)
            st["mallocs"].append((sz, buf, address))
            uc.reg_write(UC_ARM64_REG_X0, buf)
        else:
            if _have_all(st["mallocs"]):
                st["done"] = True
                uc.emu_stop()
                return
            uc.reg_write(UC_ARM64_REG_X0, bump(0x40))
        uc.reg_write(UC_ARM64_REG_X30, ret)
        uc.reg_write(UC_ARM64_REG_PC, ret)

    def hook_unmapped(uc, access, address, size, value, _):
        try:
            uc.mem_map(_align_down(address), PAGE); return True
        except Exception:
            return False

    uc.hook_add(UC_HOOK_CODE, hook_code)
    uc.hook_add(UC_HOOK_MEM_UNMAPPED, hook_unmapped)
    uc.reg_write(UC_ARM64_REG_X30, STACK_BASE - 0x2000)

    pc, faults = kdf, 0
    for _ in range(8000):
        if st["done"]:
            break
        try:
            uc.emu_start(pc, kdf + 0x8000, count=50000)
            break
        except Exception:
            cur = uc.reg_read(UC_ARM64_REG_PC)
            faults += 1
            if faults > 400 or cur == 0:
                break
            pc = cur + 4

    if not _have_all(st["mallocs"]):
        sys.exit(f"could not capture the constant buffers (saw "
                 f"{[hex(s) for s, *_ in st['mallocs']]}); unsupported build — try --kdf")
    out, used, slot0k_site = {}, [False] * len(st["mallocs"]), None
    for want_sz, name in _WANT:
        for i, (sz, buf, addr) in enumerate(st["mallocs"]):
            if not used[i] and sz == want_sz:
                used[i] = True
                out[name] = bytes(uc.mem_read(buf, want_sz))
                if name == "slot0k":
                    slot0k_site = addr
                break
    key0, out["slot0n"] = _read_slot0(data, slot0k_site, text_delta)
    if key0 != out["slot0k"]:
        sys.exit("slot-0 key mismatch (memory vs code); unsupported build")
    return out


def _map_segments(uc: Uc, data: bytes):
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phes = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    for i in range(e_phnum):
        o = e_phoff + i * e_phes
        if struct.unpack_from("<I", data, o)[0] != 1:
            continue
        p_offset = struct.unpack_from("<Q", data, o + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, o + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, o + 0x20)[0]
        p_memsz = struct.unpack_from("<Q", data, o + 0x28)[0]
        base = _align_down(p_vaddr)
        uc.mem_map(base, _align_up(p_vaddr + p_memsz) - base)
        uc.mem_write(p_vaddr, data[p_offset:p_offset + p_filesz])


def _cbc(pt: bytes, key: bytes, iv: bytes) -> bytes:
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(pt) + e.finalize()


def _derive_slots(seed: bytes, c: dict[str, bytes]) -> list[tuple[bytes, bytes]]:
    """The app's key-derivation maths, using the constants read from the library."""
    pv8, c32, c16 = c["pv8"], c["c32"], c["c16"]
    slot0 = (c["slot0k"], c["slot0n"])
    slot5 = (c["slot5k"], c["slot5n"])
    acc = 0
    for j in range(32):
        s = seed[j % 6]
        acc = (acc + s + s * c32[j]) & 0xFFFFFFFF
    slots = [slot0]
    for slot in range(1, 5):
        block = pv8[slot * 64:slot * 64 + 64]
        key_in, nonce_in = bytearray(32), bytearray(16)
        acc_i = slot * (acc // 3)
        for i in range(32):
            acc_i += slot + 4
            base = (seed[i % 6] + ((acc_i + i * slot) & 0xFF)) & 0xFF
            key_in[i] = base ^ block[i]
            if i < 16:
                nonce_in[i] = base ^ block[32 + i]
        slots.append((_cbc(c32, bytes(key_in), bytes(nonce_in)),
                      _cbc(c16, bytes(key_in), bytes(nonce_in))))
    slots.append(slot5)
    return slots


# --------------------------------------------------------------------------- #
# Find the grill's MAC over Bluetooth (from the cleartext advertised name)
# --------------------------------------------------------------------------- #
def _find_grill_mac(timeout: float = 10.0) -> str:
    try:
        from bleak import BleakScanner
    except ImportError:
        sys.exit("no --mac given and bleak is not installed: pip install bleak (or pass --mac)")
    import asyncio
    found: dict[str, str] = {}

    def cb(dev, adv):
        name = (adv.local_name or getattr(dev, "name", None) or "")
        if name.startswith("NCEU") and len(name) >= 16:
            mac_hex = name[4:16]
            if all(ch in "0123456789abcdefABCDEF" for ch in mac_hex):
                found[mac_hex.lower()] = name

    async def scan():
        scanner = BleakScanner(detection_callback=cb)
        print(f"Scanning {timeout:.0f}s for a Woodfire grill (power it on, in range)…",
              file=sys.stderr)
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()

    asyncio.run(scan())
    if not found:
        sys.exit("no Woodfire grill found over Bluetooth; power it on and keep it in "
                 "range, or pass --mac")
    if len(found) > 1:
        macs = ", ".join(":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()
                         for h in sorted(found))
        sys.exit(f"more than one Woodfire grill in range ({macs}); re-run with "
                 "--mac <your grill's address> so keys are only derived for your own grill")
    mac_hex = next(iter(found))
    mac = ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2)).upper()
    print(f"Found grill: {mac} — make sure this is your own grill before using the bundle",
          file=sys.stderr)
    return mac


def main():
    p = argparse.ArgumentParser(description="Extract the Woodfire local key bundle "
                                "from your own copy of the app")
    p.add_argument("--app", required=True,
                   help="the .apkm / .apk you downloaded, or an extracted libgrillcore.so")
    p.add_argument("--mac", default=None,
                   help="grill Bluetooth address AA:BB:CC:DD:EE:FF; omit to auto-detect over BLE")
    p.add_argument("--kdf", type=lambda x: int(x, 0), default=None,
                   help="override the key-routine address if auto-detection fails")
    args = p.parse_args()

    mac = args.mac or _find_grill_mac()
    seed = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(seed) != 6:
        sys.exit("could not determine a 6-byte grill Bluetooth address")

    data = load_grillcore(args.app)
    secs = _sections(data)
    malloc_plt = _find_malloc_plt(data, secs)
    text_delta = _text_delta(data)
    kdf = args.kdf or _find_kdf_entry(data, secs, malloc_plt)
    print(f"key routine @ {kdf:#x}, malloc@plt {malloc_plt:#x}", file=sys.stderr)

    consts = _extract_constants(data, kdf, malloc_plt, text_delta)
    slots = _derive_slots(seed, consts)
    tx, rx, beacon = slots[4], slots[3], slots[0]
    bundle = b"".join((tx[0], tx[1], rx[0], rx[1], beacon[0], beacon[1]))
    assert len(bundle) == 144
    print(base64.b64encode(bundle).decode())


if __name__ == "__main__":
    main()
