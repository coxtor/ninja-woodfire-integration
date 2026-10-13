"""Passive BLE advertisement beacon decoder for the Woodfire grill.

The grill broadcasts its live state (crucially the meat-probe temperature, which
the GATT 0x0f frame never carries) inside its BLE advertisement manufacturer
data — in cleartext, bit-packed, no encryption and no key. The vendor app reads
it passively while scanning; so can we, over Home Assistant's Bluetooth stack
(Bluetooth-proxy capable), without a connection, session token or secret.

Layout reversed from the firmware bit-reader (FUN_00273ff8) and confirmed field
by field against the grill display — see docs/COMMAND_REFERENCE.md
("BEACON GEKNACKT"). The manufacturer payload is read LSB-first:

  value(off, width) = sum over i in [0,width): bit(off+i) << i

All the fields we expose live within the first 20 bytes (the main
advertisement's manufacturer data); a scan-response adds more that we ignore.
"""
from __future__ import annotations

# SharkNinja company identifier in the Woodfire advertisement (little-endian
# 4f 0c on the wire). Home Assistant keys manufacturer_data by this value.
COMPANY_ID = 0x0C4F

# (bit offset, bit width). Pinned via a Frida hook on the firmware bit-reader
# and validated against the live display (probe0 field == displayed °C).
_PROBE0 = (78, 10)   # probe 0 temperature, raw °C  (confirmed: 3 == 3, 21 ~= 22)
_PROBE1 = (88, 10)   # probe 1 temperature, raw °C  (0 == not plugged in)
_CHAMBER = (68, 10)  # chamber temperature candidate (scale vs display unverified)
_TIMER = (51, 17)    # cook countdown (unit unverified)

# A 10-bit probe field reading its maximum looks like an open circuit / no
# probe; anything above a sane meat range is treated as "not connected".
_PROBE_MAX_C = 300


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


# The main advertisement's manufacturer data (20 bytes) holds the fields we read;
# a scan response adds 23 more that start with the grill MAC. Its second byte is a
# constant type tag (0x0D) we use to tell the two apart.
_MFG1_LEN = 20
_MFG1_TAG = 0x0D


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
    """Pick the main-advertisement manufacturer payload (mfg1) from HA adv data.

    bleak may surface either the advertisement's manufacturer data (mfg1) or the
    scan response's (mfg2) under the same company id. Prefer the raw adv, where
    the first company-0x0C4F AD is always mfg1; otherwise disambiguate by length
    and the type tag.
    """
    if raw:
        blob = _first_mfg_ad(raw)
        if blob and len(blob) >= _MFG1_LEN:
            return blob
    blob = (manufacturer_data or {}).get(COMPANY_ID)
    if not blob:
        return None
    if len(blob) >= _MFG1_LEN + 20:          # combined mfg1||mfg2
        return blob[:_MFG1_LEN]
    if len(blob) >= _MFG1_LEN and blob[1] == _MFG1_TAG:
        return blob
    return None


def parse_beacon(mfg: bytes | None) -> dict | None:
    """Decode the manufacturer payload (mfg1, company 0x0C4F) into live readings.

    Returns None if the payload is too short to hold the fields we read. Probe
    temperatures are None when no probe is plugged into that port.
    """
    if not mfg or len(mfg) < _MFG1_LEN:
        return None
    return {
        "probe0_temp": _probe_c(_bitread(mfg, *_PROBE0)),
        "probe1_temp": _probe_c(_bitread(mfg, *_PROBE1)),
    }
