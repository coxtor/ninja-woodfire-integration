#!/usr/bin/env python3
"""Extract the Ninja Woodfire local key bundle from YOUR OWN libgrillcore.so.

This tool ships **no key material**. It recovers the keys by emulating the
app's own key routine inside *your own copy* of its native library (pulled from
an APK you downloaded yourself) and reading the compiled-in constants out of
emulated memory. It then runs the key-derivation maths (an algorithm, not a
key) with your grill's Bluetooth address to produce the three session/beacon
slots the integration needs, printed as one base64 "key bundle".

Needs:  pip install unicorn capstone cryptography

Usage:
    python3 extract_ninja_keys.py --so libgrillcore.so --mac AA:BB:CC:DD:EE:FF

For the reference build (app 1.17.1-2361) the defaults below are correct. For a
newer build, pass the key-init function's RVA with --kdf (find it via the
decrypt wrapper that calls it).
"""
from __future__ import annotations

import argparse
import base64
import struct
import sys

try:
    from unicorn import (Uc, UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE,
                         UC_HOOK_MEM_UNMAPPED)
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
                                     UC_ARM64_REG_X30, UC_ARM64_REG_SP, UC_ARM64_REG_PC)
except ImportError:
    sys.exit("needs unicorn:  pip install unicorn")
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Reference build 1.17.1-2361 (sha acb9dfab…). Override --kdf for other builds.
DEFAULT_KDF = 0x174144      # key-init function entry (RVA / file vaddr, base 0)
MALLOC_PLT = 0x491160       # malloc@plt

PAGE = 0x1000
STACK_BASE = 0x7000_0000
STACK_SIZE = 0x200000
HEAP_BASE = 0x1000_0000
HEAP_SIZE = 0x400000

# The constant buffers the key-init allocates, in order (size -> name). slot0's
# nonce is read from code (capstone) because emulation diverges right after the
# slot0 key store; everything before it is read straight from emulated memory.
_WANT = [(0x180, "pv8"), (0x20, "slot5k"), (0x10, "slot5n"),
         (0x20, "c32"), (0x10, "c16"), (0x20, "slot0k")]
_TEXT_VADDR_DELTA = 0x4000   # .text vaddr - file offset (reference build)


def _align_down(x): return x & ~(PAGE - 1)
def _align_up(x): return (x + PAGE - 1) & ~(PAGE - 1)


def _load_segments(uc: Uc, data: bytes):
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, off)[0] != 1:  # PT_LOAD
            continue
        p_offset = struct.unpack_from("<Q", data, off + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        p_memsz = struct.unpack_from("<Q", data, off + 0x28)[0]
        base = _align_down(p_vaddr)
        uc.mem_map(base, _align_up(p_vaddr + p_memsz) - base)
        uc.mem_write(p_vaddr, data[p_offset:p_offset + p_filesz])


def _extract_constants(path: str, kdf: int) -> dict[str, bytes]:
    """Emulate the key-init and read the constant buffers from emulated memory."""
    data = open(path, "rb").read()
    uc = Uc(UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN)
    _load_segments(uc, data)
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
        if (word & 0xFC000000) != 0x94000000:   # only intercept BL
            return
        imm = word & 0x03FFFFFF
        if imm & 0x02000000:
            imm -= 0x04000000
        target = (address + imm * 4) & 0xFFFFFFFFFFFFFFFF
        ret = (address + 4) & 0xFFFFFFFFFFFFFFFF
        if target == MALLOC_PLT:
            sz = uc.reg_read(UC_ARM64_REG_X0)
            buf = bump(sz)
            st["mallocs"].append((sz, buf, address))
            uc.reg_write(UC_ARM64_REG_X0, buf)
        else:
            # Once all constant buffers are allocated AND filled (we're now at the
            # next, non-malloc call — the maths/AES), stop and read memory.
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
        raise RuntimeError(
            f"could not capture all constant buffers (saw sizes "
            f"{[hex(s) for s, _ in st['mallocs']]}); wrong --kdf for this build?")
    # Match the allocated buffers to the expected sizes, in order, read from
    # emulated memory; remember slot0's key-malloc site for the capstone read.
    out, used, slot0k_site = {}, [False] * len(st["mallocs"]), None
    for want_sz, name in _WANT:
        for i, (sz, buf, addr) in enumerate(st["mallocs"]):
            if not used[i] and sz == want_sz:
                used[i] = True
                out[name] = bytes(uc.mem_read(buf, want_sz))
                if name == "slot0k":
                    slot0k_site = addr
                break
    # slot0 key+nonce from code at its malloc site (emulation diverges after it).
    key0, out["slot0n"] = _read_const_pair(data, slot0k_site)
    if key0 != out["slot0k"]:
        raise RuntimeError("slot-0 key mismatch (memory vs code); wrong build?")
    return out


def _have_all(mallocs) -> bool:
    need = {}
    for sz, _ in _WANT:
        need[sz] = need.get(sz, 0) + 1
    have = {}
    for sz, *_ in mallocs:
        have[sz] = have.get(sz, 0) + 1
    return all(have.get(sz, 0) >= n for sz, n in need.items())


def _read_const_pair(data: bytes, call_site: int) -> tuple[bytes, bytes]:
    """Read the 4 u64 (key) + 2 u64 (nonce) that the code at `call_site` builds
    with mov/movz/movk and stores via `str x<n>,[x0,#off]` — for slot 0."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    f = call_site - _TEXT_VADDR_DELTA
    regs: dict[str, int] = {}
    words: list[int] = []
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
        raise RuntimeError(f"slot-0 const read incomplete ({len(words)}/6)")
    key = b"".join(struct.pack("<Q", w) for w in words[0:4])
    iv = b"".join(struct.pack("<Q", w) for w in words[4:6])
    return key, iv


def _cbc(pt: bytes, key: bytes, iv: bytes) -> bytes:
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(pt) + e.finalize()


def _derive_slots(seed: bytes, c: dict[str, bytes]) -> list[tuple[bytes, bytes]]:
    """The app's key-derivation maths, using the constants read from the .so."""
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


def main():
    p = argparse.ArgumentParser(description="Extract the Woodfire local key bundle "
                                "from your own libgrillcore.so")
    p.add_argument("--so", required=True, help="path to libgrillcore.so from your APK")
    p.add_argument("--mac", required=True, help="grill Bluetooth address AA:BB:CC:DD:EE:FF")
    p.add_argument("--kdf", type=lambda x: int(x, 0), default=DEFAULT_KDF)
    args = p.parse_args()

    seed = bytes.fromhex(args.mac.replace(":", "").replace("-", ""))
    if len(seed) != 6:
        sys.exit("--mac must be a 6-byte Bluetooth address")

    consts = _extract_constants(args.so, args.kdf)
    slots = _derive_slots(seed, consts)
    tx, rx, beacon = slots[4], slots[3], slots[0]   # app->grill, grill->app, beacon
    bundle = b"".join((tx[0], tx[1], rx[0], rx[1], beacon[0], beacon[1]))
    assert len(bundle) == 144
    print(base64.b64encode(bundle).decode())


if __name__ == "__main__":
    main()
