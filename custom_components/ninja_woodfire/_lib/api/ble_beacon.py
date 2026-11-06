"""Passive BLE advertisement beacon decoder for the Woodfire grill.

The grill broadcasts its live state (crucially the meat-probe temperature and
the lid state, which the GATT 0x0f frame never carries) inside its BLE
advertisement manufacturer data. The vendor app reads it passively while
scanning; so can we, over Home Assistant's Bluetooth stack (Bluetooth-proxy
capable), without a connection, session token or per-device secret.

The payload is encrypted. Reversed from the firmware (libgrillcore
FUN_002727f8 / FUN_00273bac) and validated byte-exact against live captures:

  cipher  AES-128-ECB used as CBC with ciphertext-stealing (CTS), per segment
  key     the global BLE "slot 0" key    (the same on every grill; caller-supplied)
  iv      the global BLE "slot 0" nonce   (caller-supplied)

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
_REMAINING = (51, 17)  # seconds left on the cook timer (1 Hz countdown while cooking)
_TOTAL = (104, 16)     # the cook's set duration in seconds (== remaining during preheat)

# ProbeState.state within each probe's detailed record in mfg2 (only present
# when the scan response was captured alongside the main advertisement - see
# mfg1_from_advertisement). Bit-exact live-validated 2026-11-06 against the
# app's own GrillCombinedState log, three sessions: 0/1/2 = PcsPOR/PcsStart/
# PcsCooking (3 transitions), 3/4 = PcsDoneAfterCook/PcsGetFood (probe hit its
# target), 5 = PcsResting (the app's own post-target rest timer; its
# ProbeState.seconds field - idx44 here - also matched the app's rest-timer
# seconds exactly). Mapped to the same vocabulary ProbeInfo.state already
# uses for the cloud backends (models.py), with "resting" added to it.
_PROBE0_STATE = (224, 4)
_PROBE1_STATE = (272, 4)
_PROBE_STATE_MAP = {
    0: "none",       # PcsPOR - no target / idle
    1: "none",       # PcsStart - target just set, not yet meaningfully progressing
    2: "cooking",    # PcsCooking
    3: "done",       # PcsDoneAfterCook
    4: "get_food",   # PcsGetFood
    5: "resting",    # PcsResting - the app's own post-target rest timer is running
}

# The firmware's own UI state machine ("Uim"). The grill keeps advertising
# this beacon even with its display fully off (UimPowerOff) - it only stops
# advertising while a GATT client holds a connection. Without this the
# powered-off and merely-idle-and-responsive cases are indistinguishable from
# the advertisement alone (both carry no cook phase), so a sleeping grill
# looked like a plain idle one and the card offered a Start the firmware
# would silently ignore. Live-validated 2026-11-06 over 600+ real frames:
# 3 == UimPowerOff, 4 == UimIdle, 7 == UimCookActive. A later test toggling
# the grill's own physical power button (only reachable that way, never via a
# remote Stop/Start/Wake) added 5 == UimHello (brief post-power-on boot step)
# and 9 == UimPoweringOff (brief pre-shutdown step): PowerOff -> Hello -> Idle
# -> PoweringOff -> PowerOff. PoweringOff is treated as "powered off" too -
# the grill is already on its way down and a Start would be as useless as
# during PowerOff itself, so the card should offer Wake right away rather
# than waiting for the sequence to finish. Hello is the opposite direction
# (coming up) and is deliberately not flagged. 6/8 were never observed.
_UIM = (16, 5)
_UIM_POWERED_OFF = 3
_UIM_POWERING_OFF = 9
_UIM_IDLE = 4

# Mapped against a full cook captured over the air (phase/prompt confirmed
# against the grill display): preheat->add food->cooking->flip->done->get food.
_PHASE_MAP = {2: "ignition", 3: "preheat", 4: "cooking", 5: "get_food", 8: "lid_open"}  # 0 == idle/done; off == no beacon
# 5 (live-validated 2026-11-06, bit-exact against the app's own decrypted
# log, 3 frames): this field is the firmware's own GCCookState enum - the
# names line up almost one-to-one with what was already mapped empirically
# (Infusion~ignition, Preheat, Heat~cooking, LidOpen). Raw 5 = CookStatePause,
# observed exactly when a probe reaches its target, ~2 s before the
# "get food" prompt (@98) actually appears - which it never did in these
# frames (prompt stayed None throughout the whole probe cook, the only
# "please remove the food" signal this grill mode ever sent in the beacon).
# Mapped straight to the existing "get_food" state rather than inventing a
# new one, since the card already shows "please remove food" for that case.
# 8 (live-validated 2026-11-06): the lid was opened during a wood-fire cook
# (firmware's own CookStateLidOpen). Re-closing it re-runs ignition (phase
# goes back to 2) rather than resuming straight into cooking - the grill
# re-lights, it doesn't just pick up where it left off. Duration/setpoint are
# untouched. The coordinator already derives the same "lid_open" display
# state from the lid bit directly, so this mapping is for completeness only.
_PROMPT_MAP = {1: "addfood", 4: "flipfood", 6: "done", 7: "getfood"}  # 0 == none

# Every frame this layout applies to carries 13 in its second byte (the
# firmware branches on it); other values are different layouts or noise.
_LAYOUT = (8, 8)
_LAYOUT_ID = 13
# The grill takes durations in whole minutes and up to 12 h. A "total" that is
# neither is a differently-used field (with a meat probe active the firmware
# repurposes those bits), so only the remaining time is trusted from such a frame.
_TIMER_MAX_S = 12 * 3600

# A 10-bit probe field reading its maximum looks like an open circuit / no
# probe; anything above a sane meat range is treated as "not connected".
_PROBE_MAX_C = 300

# The main advertisement's manufacturer data (mfg1) is 20 bytes; the scan
# response's (mfg2) is 23. mfg1 holds every field we decode except the
# per-probe detail record (ProbeState.state and friends), which only mfg2
# carries - decoded when we happen to have it, since not every HA Bluetooth
# backend hands us the scan response alongside the main advertisement.
_MFG1_LEN = 20
_MFG2_LEN = 23


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


def _timer(plain: bytes) -> tuple[int | None, int | None]:
    """(remaining, total) cook-timer seconds; None for whichever isn't a timer."""
    if _bitread(plain, *_LAYOUT) != _LAYOUT_ID:
        return None, None
    remaining = _bitread(plain, *_REMAINING)
    total = _bitread(plain, *_TOTAL)
    if not 0 < remaining <= _TIMER_MAX_S:
        return None, None
    if not 0 < total <= _TIMER_MAX_S or total % 60:
        return remaining, None
    if remaining > total:
        return None, None
    return remaining, total


def _all_mfg_ads(raw: bytes) -> list[bytes]:
    """Return the data of every company-0x0C4F manufacturer AD in a raw adv.

    Usually one (mfg1, from the main advertisement); some stacks (observed:
    Android's own merged ScanRecord) hand us both mfg1 and the scan
    response's mfg2 concatenated into a single raw blob as two separate ADs.
    """
    out = []
    i = 0
    while i + 1 < len(raw):
        length = raw[i]
        if length == 0:
            break
        ad = raw[i + 1 : i + 1 + length]
        if len(ad) >= 3 and ad[0] == 0xFF and ad[1] == 0x4F and ad[2] == 0x0C:
            out.append(bytes(ad[3:]))
        i += 1 + length
    return out


def mfg1_from_advertisement(
    manufacturer_data: dict[int, bytes] | None, raw: bytes | None = None
) -> bytes | None:
    """Pick the manufacturer ciphertext from HA adv data: mfg1 alone, or
    mfg1||mfg2 (43 bytes) when the scan response's mfg2 is also available.

    bleak may surface the raw advertisement or a per-company manufacturer_data
    map. In the raw adv, mfg1 is the first company-0x0C4F AD and a second one
    (if present) is mfg2; otherwise the map entry is already whatever bleak
    merged under that company ID (20, 23, or 43 bytes). A lone 23-byte mfg2
    with no mfg1 carries none of our fields and is rejected.
    """
    if raw:
        ads = _all_mfg_ads(raw)
        if ads and len(ads[0]) >= _MFG1_LEN:
            if len(ads) > 1 and len(ads[1]) >= _MFG2_LEN:
                return ads[0][:_MFG1_LEN] + ads[1][:_MFG2_LEN]
            return ads[0][:_MFG1_LEN]
    blob = (manufacturer_data or {}).get(COMPANY_ID)
    if blob and len(blob) >= _MFG1_LEN + _MFG2_LEN:
        return bytes(blob[: _MFG1_LEN + _MFG2_LEN])
    if blob and len(blob) >= _MFG1_LEN and len(blob) != _MFG2_LEN:
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
    remaining, total = _timer(plain)
    probe0_state = probe1_state = None
    if len(mfg) >= _MFG1_LEN + _MFG2_LEN:
        # mfg2 is a second, independent CTS segment - decrypted separately,
        # not just more bytes of mfg1's.
        plain += _decrypt_segment(bytes(mfg[_MFG1_LEN : _MFG1_LEN + _MFG2_LEN]), key, iv)
        probe0_state = _PROBE_STATE_MAP.get(_bitread(plain, *_PROBE0_STATE))
        probe1_state = _PROBE_STATE_MAP.get(_bitread(plain, *_PROBE1_STATE))
    return {
        "probe0_temp": _probe_c(_bitread(plain, *_PROBE0)),
        "probe1_temp": _probe_c(_bitread(plain, *_PROBE1)),
        "lid_open": bool(_bitread(plain, *_LID)),
        "phase": _PHASE_MAP.get(_bitread(plain, *_PHASE)),
        "prompt": _PROMPT_MAP.get(_bitread(plain, *_PROMPT)),
        "remaining_s": remaining,
        "total_s": total,
        "powered_off": _bitread(plain, *_UIM) in (_UIM_POWERED_OFF, _UIM_POWERING_OFF),
        # Live-observed 2026-11-06: a probe-driven cook's post-cook hold
        # ("get_food") can outlast the lid-cycle release path entirely - the
        # grill ran its own physical "Rest" countdown and returned to a
        # genuine UimIdle on its own, with no lid action at all. phase alone
        # can't tell "still resting" from "actually idle now" (both blank),
        # so Uim itself is the only reliable ground truth here.
        "uim_idle": _bitread(plain, *_UIM) == _UIM_IDLE,
        "probe0_state": probe0_state,
        "probe1_state": probe1_state,
    }
