"""BLE wire protocol for the Woodfire grill (transport-independent codec).

Pure byte-level encode/decode: AES-256-CBC + XOR mask framing, the cook-command
layout, and the GET_GrillState equivalent (RX type 0x0f). No I/O here, and **no
key material**: the session slots (key+nonce per direction) and the beacon key
are supplied by the caller. They are produced by an out-of-tree extraction tool
that reads them from the user's own copy of the vendor app — see the README.
"""
from __future__ import annotations

import base64
import binascii
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FRAME_MAGIC = 0x019F6B4D

Slot = tuple[bytes, bytes]  # (32-byte key, 16-byte nonce)

# Keys bundle: the two session slots plus the beacon slot, in that order.
KeyBundle = tuple[Slot, Slot, Slot]  # (tx_slot, rx_slot, beacon_slot)


def _slot_at(raw: bytes, off: int) -> Slot:
    return (raw[off:off + 32], raw[off + 32:off + 48])


def parse_slots_blob(blob: str) -> KeyBundle:
    """Parse the paste-block the extraction tool produces into the key bundle.

    The blob is the concatenation, in order, of three (32-byte key, 16-byte
    nonce) slots — the app->grill session slot, the grill->app session slot and
    the beacon slot — i.e. 144 raw bytes, given either as base64 or as
    whitespace-separated hex tokens. No keys are embedded in this package; the
    blob comes from the extraction tool (see README).
    """
    tokens = blob.split()
    if len(tokens) > 1:
        try:
            raw = b"".join(bytes.fromhex(t) for t in tokens)
        except ValueError as err:
            raise ValueError(f"invalid hex in key blob: {err}") from err
    else:
        try:
            raw = base64.b64decode(blob.strip(), validate=True)
        except (binascii.Error, ValueError) as err:
            raise ValueError(f"key blob must be base64 or hex: {err}") from err
    if len(raw) != 144:
        raise ValueError(f"key blob must decode to 144 bytes, got {len(raw)}")
    return _slot_at(raw, 0), _slot_at(raw, 48), _slot_at(raw, 96)

# Cook mode -> command byte [2]. Grill uses heat level (1-3) in temp; others use °C.
MODE_BYTE = {
    "grill": 1, "smoker": 2, "air crisp": 3, "bake": 4,
    "roast": 5, "dehydrate": 7, "reheat": 8,
}
MODE_NAME = {v: k for k, v in MODE_BYTE.items()}

_PROBE_NC = 0xFFFC  # -4 sentinel: probe element not connected
CONNECT_CMD = bytes([0x10]) + bytes(22)  # establish-session request, token 0


def _cbc(key: bytes, iv: bytes):
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def encrypt(pt: bytes, key: bytes, iv: bytes) -> bytes:
    e = _cbc(key, iv).encryptor()
    return e.update(pt) + e.finalize()


def decrypt(ct: bytes, key: bytes, iv: bytes) -> bytes:
    d = _cbc(key, iv).decryptor()
    return d.update(ct) + d.finalize()


def _mask(buf: bytes) -> bytes:
    """Self-inverse XOR obfuscation keyed by the first four bytes."""
    out = bytearray(buf)
    for i in range(4, len(out)):
        out[i] ^= out[i & 3]
    return bytes(out)


def encode_frame(command: bytes, key: bytes, iv: bytes, *, token: int = 0,
                 session_id: int | None = None, field2: int = 0,
                 pad: bytes | None = None) -> bytes:
    """Build one BLE frame: payload = sid|magic|token|field2|len|command|pad, masked, CBC."""
    if session_id is None:
        session_id = int.from_bytes(os.urandom(4), "little")
    head = (struct.pack("<I", session_id) + struct.pack("<I", FRAME_MAGIC)
            + struct.pack("<I", token) + struct.pack("<I", field2)
            + bytes([len(command)]) + command)
    padlen = (-len(head)) % 16
    head += (pad or os.urandom(padlen))[:padlen]
    return encrypt(_mask(head), key, iv)


def decode_frame(frame: bytes, key: bytes, iv: bytes) -> dict | None:
    """Decrypt + unmask a frame. Returns None if the magic does not match."""
    if len(frame) < 16 or len(frame) % 16:
        return None
    pt = _mask(decrypt(frame, key, iv))
    if pt[4:8] != struct.pack("<I", FRAME_MAGIC):
        return None
    length = pt[16]
    # The declared command length must fit the decrypted buffer; a bad length
    # means a corrupt or forged frame, not a short read.
    if length == 0 or 17 + length > len(pt):
        return None
    return {
        "session_id": struct.unpack_from("<I", pt, 0)[0],
        "token": struct.unpack_from("<I", pt, 8)[0],
        "field2": struct.unpack_from("<I", pt, 12)[0],
        "command": pt[17:17 + length],
        "payload": pt,
    }


def _probe_slot(setpoint: int | None) -> bytes:
    if not setpoint:
        return bytes(6)
    return struct.pack("<h", int(setpoint)) + bytes([0x00, 0x63, 0x00, 0x01])


def build_cook(*, mode: str, temp: int, seconds: int, smoke: bool = False,
               skip_preheat: bool = False, probe0: int | None = None,
               probe1: int | None = None) -> bytes:
    """23-byte cook command. `temp` is heat level 1-3 for grill, else °C."""
    if mode not in MODE_BYTE:
        raise ValueError(f"BLE cannot encode cook mode {mode!r}")
    temp, seconds = int(temp), int(seconds)
    # Reject out-of-range values outright rather than silently wrapping them —
    # this command drives a heating element, so a truncated value is dangerous.
    if not 0 <= temp <= 500:
        raise ValueError(f"temp out of range (0-500): {temp}")
    if not 0 <= seconds <= 86400:
        raise ValueError(f"seconds out of range (0-86400): {seconds}")
    out = bytearray(23)
    out[0] = 0x0A
    out[2] = MODE_BYTE[mode]
    out[3] = 1 if smoke else 0
    struct.pack_into("<H", out, 4, temp)
    struct.pack_into("<I", out, 6, seconds)
    out[10:16] = _probe_slot(probe0)
    out[16:22] = _probe_slot(probe1)
    out[22] = 1 if skip_preheat else 0
    return bytes(out)


def build_stop() -> bytes:
    """Stop = a cook command with zero temperature and duration."""
    out = bytearray(23)
    out[0] = 0x0A
    out[2] = MODE_BYTE["grill"]
    return bytes(out)


def parse_grill_info(body: bytes) -> dict:
    """Decode a grill-info frame (body[0] == 0x0c): model id + serial (DSN).

    Layout seen in captures: 0x0c, 1 byte, then the 16-char model id (e.g.
    'AC000W…'), and later the ASCII serial ('SND…'). Both are ASCII,
    NUL-padded. Returns {} for other frame types.
    """
    if not body or body[0] != 0x0C:
        return {}

    def _ascii(chunk: bytes) -> str:
        return chunk.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()

    out: dict[str, str] = {}
    model = _ascii(body[2:18])
    if model:
        out["model"] = model
    idx = body.find(b"SND")
    if idx != -1:
        serial = _ascii(body[idx:idx + 20])
        if serial:
            out["serial"] = serial
    return out


def _t10(raw: int) -> float | None:
    """Temperature ×10 field; the -4 sentinel means 'not connected'."""
    return None if raw == _PROBE_NC else raw / 10.0


def _chamber(raw: int) -> float | None:
    """Chamber temperature: firmware reports °C in 1/20-degree units (×20).

    Verified against a live grill display: raw 3710 → 185.5 °C (display ~185),
    raw 390 → 19.5 °C (room temp). A zero slot means that sensor is not
    reporting in the current firmware state.
    """
    return raw / 20.0 if raw else None


def parse_grill_state(body: bytes) -> dict | None:
    """Decode a GrillState frame (command field, body[0] == 0x0f).

    The 0x0f body is a fixed 138-byte struct (not a variable bincode section, as
    once assumed): every field sits at a constant offset. Offsets were pinned by
    differential analysis of real on-grill captures across grill/bake/smoker and
    a plugged probe (docs/COMMAND_REFERENCE.md):

      @16  u8   mode            @18 u16 setpoint (raw °C / heat level)
      @24  u32  seconds_set     @28 u16 seconds_left     @29 u8 active
      @60  u16  probe0 target (raw °C, 0 = no probe)
      @110/@114 u16  chamber temperature ×20 (one slot populated at a time,
                     depending on firmware state — grill element vs convection)

    The per-probe *reading* is NOT in this GATT frame: the firmware ships it only
    in the encrypted BLE advertisement beacon (the app reads it passively while
    scanning; GATT 0x0f always reports the meat probe as not-connected). Decoding
    the beacon needs its own cipher — see docs/COMMAND_REFERENCE.md.
    """
    if len(body) < 30 or body[0] != 0x0F:
        return None
    u16 = lambda o: struct.unpack_from("<H", body, o)[0]
    out = {
        "mode": MODE_NAME.get(u16(16)),
        "setpoint": u16(18),
        "seconds_set": struct.unpack_from("<I", body, 24)[0],
        "seconds_left": u16(28),
        "active": body[29] != 0,
    }
    if len(body) >= 118:
        # Exactly one of the two chamber slots carries the reading at a time.
        out["chamber"] = _chamber(u16(110)) or _chamber(u16(114))
        out["probe0_target"] = u16(60) or None
    return out
