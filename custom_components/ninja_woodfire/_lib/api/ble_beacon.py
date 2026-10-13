"""Passive BLE advertisement beacon decoder for the Woodfire grill.

The grill broadcasts its live state (crucially the meat-probe temperature and
the lid state, which the GATT 0x0f frame never carries) inside its BLE
advertisement manufacturer data. The vendor app reads it passively while
scanning; so can we, over Home Assistant's Bluetooth stack (Bluetooth-proxy
capable), without a connection, session token or per-device secret.

The payload is encrypted. Reversed from the firmware (libgrillcore
FUN_002727f8 / FUN_00273bac) and validated byte-exact against live captures:

  cipher  AES-128-ECB used as CBC with ciphertext-stealing (CTS), per segment
  key     the global BLE "slot 0" key   (seed-independent, not a vendor secret)
  iv      the global BLE "slot 0" nonce

Decryption is a pure function of the advertisement bytes (no per-frame secret).
The main advertisement's manufacturer data (mfg1, 20 bytes) decrypts to the
first 20 plaintext bytes, which hold every field we expose; the scan response's
mfg2 (23 bytes) carries only fields we ignore, so we never need it.

Per-segment CTS decrypt (segment length L, 16 < L < 32, tail t = L-16):
  D0    = AES_ECB_dec(ct[0:16]) XOR iv
  plain = D0[0:t] || (AES_ECB_dec(D0[t:16] || ct[16:L]) XOR iv)

Fields are bit-packed LSB-first (widths from the firmware bit-reader):
  value(off, width) = sum over i in [0,width): bit(off+i) << i
"""
from __future__ import annotations

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# SharkNinja company identifier in the Woodfire advertisement (little-endian
# 4f 0c on the wire). Home Assistant keys manufacturer_data by this value.
COMPANY_ID = 0x0C4F

# The advertisement is encrypted with the grill's beacon slot (a key+nonce the
# caller supplies from the extraction tool — see README). No key is embedded here.
_BACKEND = default_backend()

# (bit offset, bit width). Pinned against the live grill display: probe fields
# read raw °C (beacon 53 == displayed 53 °C), the lid bit toggles 1<->0 with the
# physical lid independent of the cook state.
_PROBE0 = (78, 10)   # probe 0 temperature, raw °C
_PROBE1 = (88, 10)   # probe 1 temperature, raw °C  (0 == not plugged in)
_LID = (48, 1)       # 1 == lid open, 0 == lid closed
_PHASE = (25, 5)     # cook phase
_PROMPT = (98, 6)    # user prompt shown on the grill display

# Mapped against a full cook captured over the air (phase/prompt confirmed
# against the grill display): preheat->add food->cooking->flip->done->get food.
_PHASE_MAP = {2: "ignition", 3: "preheat", 4: "cooking"}  # 0 == idle/done; off == no beacon
_PROMPT_MAP = {1: "addfood", 4: "flipfood", 6: "done", 7: "getfood"}  # 0 == none

# A 10-bit probe field reading its maximum looks like an open circuit / no
# probe; anything above a sane meat range is treated as "not connected".
_PROBE_MAX_C = 300

# The main advertisement's manufacturer data (mfg1) is 20 bytes; the scan
# response's (mfg2) is 23. mfg1 holds every field we decode.
_MFG1_LEN = 20


def _aes_dec(block: bytes, key: bytes) -> bytes:
    return Cipher(algorithms.AES(key), modes.ECB(), _BACKEND).decryptor().update(block)


def _xor(a: bytes, b: bytes) -> bytes:
    out = bytearray(len(a))
    for i in range(len(a)):
        out[i] = a[i] ^ b[i]
    return bytes(out)


def _decrypt_segment(ct: bytes, key: bytes, iv: bytes) -> bytes:
    """CBC-with-ciphertext-stealing decrypt of one manufacturer-data segment."""
    length = len(ct)
    if length == 16:
        return _xor(_aes_dec(ct, key), iv)
    tail = length - 16
    d0 = _xor(_aes_dec(ct[:16], key), iv)
    return d0[:tail] + _xor(_aes_dec(d0[tail:16] + ct[16:length], key), iv)


def _bitread(buf: bytes, off: int, width: int) -> int:
    """Read `width` bits at bit offset `off`, LSB-first (as the firmware does)."""
    value = 0
    for i in range(width):
        pos = off + i
        if pos >> 3 >= len(buf):
            break
        value |= ((buf[pos >> 3] >> (pos & 7)) & 1) << i
    return value


def _probe_c(raw: int) -> float | None:
    """A probe field in °C; 0 or an out-of-range reading means 'not connected'."""
    if raw == 0 or raw > _PROBE_MAX_C:
        return None
    return float(raw)


def _first_mfg_ad(raw: bytes) -> bytes | None:
    """Return the data of the first company-0x0C4F manufacturer AD in a raw adv."""
    i = 0
    while i + 1 < len(raw):
        length = raw[i]
        if length == 0:
            break
        ad = raw[i + 1 : i + 1 + length]
        if len(ad) >= 3 and ad[0] == 0xFF and ad[1] == 0x4F and ad[2] == 0x0C:
            return bytes(ad[3:])
        i += 1 + length
    return None


def mfg1_from_advertisement(
    manufacturer_data: dict[int, bytes] | None, raw: bytes | None = None
) -> bytes | None:
    """Pick the main-advertisement manufacturer ciphertext (mfg1) from HA adv data.

    bleak may surface the raw advertisement or a per-company manufacturer_data
    map. In the raw adv the first company-0x0C4F AD is always mfg1; otherwise we
    take the map entry and keep its first 20 bytes (a combined mfg1||mfg2 blob
    starts with mfg1). A lone 23-byte mfg2 carries none of our fields, so it is
    rejected.
    """
    if raw:
        blob = _first_mfg_ad(raw)
        if blob and len(blob) >= _MFG1_LEN:
            return blob[:_MFG1_LEN]
    blob = (manufacturer_data or {}).get(COMPANY_ID)
    if blob and len(blob) >= _MFG1_LEN and len(blob) != 23:
        return bytes(blob[:_MFG1_LEN])
    return None


def parse_beacon(mfg: bytes | None, key: bytes, iv: bytes) -> dict | None:
    """Decode the mfg1 ciphertext (company 0x0C4F) into live readings.

    `key`/`iv` are the grill's beacon slot (supplied by the caller). Returns None
    if the payload is too short. Probe temperatures are None when no probe is
    plugged into that port; `lid_open` reflects the lid switch.
    """
    if not mfg or len(mfg) < _MFG1_LEN:
        return None
    plain = _decrypt_segment(bytes(mfg[:_MFG1_LEN]), key, iv)
    return {
        "probe0_temp": _probe_c(_bitread(plain, *_PROBE0)),
        "probe1_temp": _probe_c(_bitread(plain, *_PROBE1)),
        "lid_open": bool(_bitread(plain, *_LID)),
        "phase": _PHASE_MAP.get(_bitread(plain, *_PHASE)),
        "prompt": _PROMPT_MAP.get(_bitread(plain, *_PROMPT)),
    }
