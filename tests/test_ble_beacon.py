"""Passive advertisement beacon decoder tests.

The beacon is encrypted (AES-CBC with ciphertext-stealing). This package ships
no key material; `parse_beacon` takes the beacon slot (key, nonce) from the
caller. These tests use an obviously-fake key and self-made ciphertext, so they
exercise the algorithm and bit layout without any real device key.
"""
from __future__ import annotations

from custom_components.ninja_woodfire._lib.api import ble_beacon as bc

# Fake beacon slot — NOT a real device key.
KEY = bytes(range(32))
IV = bytes(range(16))

# mfg1 segments encrypted with the fake key above, carrying known fields:
CT_PROBE7_CLOSED = bytes.fromhex("67c6c0af68cac9d5fba9f0becd85f190573341d8")   # probe0 7°C, lid closed
CT_PROBE21 = bytes.fromhex("95b5e3a54c1b518ff0f8dd8055ded8503730ef9d")          # probe0 21°C
CT_PROBE53_CLOSED = bytes.fromhex("aaa391711626d18d8cad17032ef4ef4530e71a21")   # probe0 53°C, lid closed
CT_PROBE12_OPEN = bytes.fromhex("93209de2137ec14e3fee0f5c00142b49205a0941")      # probe0 12°C, lid open
CT_BOTH_PROBES = bytes.fromhex("6211bb701fd78400c356bb0589a8c8c7fb0cbbf0")        # probe0 40°C, probe1 65°C
CT_PREHEAT = bytes.fromhex("73c4dc4a674c82774ffcaf700815aefd6733cf4c")           # phase preheat
CT_COOK_ADDFOOD = bytes.fromhex("c770529bdb2180b0652924210adf66ddadb5a039")      # cooking + add-food
CT_COOK_FLIP = bytes.fromhex("db41a0ab6f1643d85727ec2a81830c8309448281")         # cooking + flip
CT_DONE_GETFOOD = bytes.fromhex("0120b4d1341b8b04380d54c403905d5aa5f4c628")       # done + get-food
CT_IGNITION = bytes.fromhex("92c646da704d49d745d00b51c19053516733cf4c")          # ignition (smoke)


def _p(ct):
    return bc.parse_beacon(ct, KEY, IV)


def test_probe0_temperature_confirmed():
    s = _p(CT_PROBE7_CLOSED)
    assert s["probe0_temp"] == 7.0
    assert s["probe1_temp"] is None
    assert _p(CT_PROBE21)["probe0_temp"] == 21.0
    assert _p(CT_PROBE53_CLOSED)["probe0_temp"] == 53.0


def test_both_probes():
    s = _p(CT_BOTH_PROBES)
    assert s["probe0_temp"] == 40.0
    assert s["probe1_temp"] == 65.0


def test_lid_bit():
    assert _p(CT_PROBE12_OPEN)["lid_open"] is True
    assert _p(CT_PROBE53_CLOSED)["lid_open"] is False


def test_phase_and_prompt():
    assert _p(CT_PREHEAT)["phase"] == "preheat"
    assert _p(CT_PREHEAT)["prompt"] is None
    assert _p(CT_IGNITION)["phase"] == "ignition"
    s = _p(CT_COOK_ADDFOOD)
    assert s["phase"] == "cooking" and s["prompt"] == "addfood"
    s = _p(CT_COOK_FLIP)
    assert s["phase"] == "cooking" and s["prompt"] == "flipfood"
    s = _p(CT_DONE_GETFOOD)
    assert s["phase"] is None and s["prompt"] == "getfood"
    s = _p(CT_PROBE7_CLOSED)
    assert s["phase"] is None and s["prompt"] is None


def test_mfg1_alone_is_enough():
    assert _p(CT_PROBE7_CLOSED[:20])["probe0_temp"] == 7.0


def test_rejects_short_or_empty():
    assert bc.parse_beacon(b"", KEY, IV) is None
    assert bc.parse_beacon(None, KEY, IV) is None
    assert bc.parse_beacon(bytes(19), KEY, IV) is None


def test_mfg1_from_advertisement_prefers_raw():
    raw = bytes.fromhex("0201060303bbfc") + bytes([0x17, 0xFF, 0x4F, 0x0C]) + CT_PROBE7_CLOSED
    assert bc.mfg1_from_advertisement({}, raw) == CT_PROBE7_CLOSED


def test_mfg1_from_advertisement_dict_variants():
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: CT_PROBE7_CLOSED}) == CT_PROBE7_CLOSED
    # A combined mfg1||mfg2 blob (scan response merged in) is kept whole now -
    # mfg2 carries the per-probe detail record parse_beacon can also decode.
    combined = CT_PROBE7_CLOSED + bytes(23)
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: combined}) == combined
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: bytes(23)}) is None
    assert bc.mfg1_from_advertisement({}) is None


def test_bitread_lsb_first():
    assert bc._bitread(bytes([0b00001010]), 0, 4) == 10
    assert bc._bitread(bytes([0xFF, 0x01]), 6, 4) == 0b0111


# --- timer / total duration (live-captured layout: @51 w17 remaining s, @104 w16 total s)

def _pack(fields, length=20):
    buf = bytearray(length)
    for off, width, value in fields:
        for i in range(width):
            if (value >> i) & 1:
                buf[(off + i) >> 3] |= 1 << ((off + i) & 7)
    return bytes(buf)


def _encrypt_segment(plain, key=KEY, iv=IV):
    """Inverse of ble_beacon._decrypt_segment, so tests can build frames with known fields."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def enc(block):
        return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(block)

    tail = len(plain) - 16
    x = enc(bc._xor(plain[tail:], iv))
    d0 = plain[:tail] + x[: 16 - tail]
    return enc(bc._xor(d0, iv)) + x[16 - tail :]


def _frame(remaining=0, total=0, phase=0, **extra):
    fields = [(8, 8, 13), (25, 5, phase), (51, 17, remaining), (104, 16, total)]
    fields += [(o, w, v) for o, (w, v) in extra.items()]
    return _encrypt_segment(_pack(fields))


def test_encrypt_helper_round_trips():
    assert bc._decrypt_segment(_encrypt_segment(bytes(range(20))), KEY, IV) == bytes(range(20))


def test_timer_and_total_are_decoded():
    s = _p(_frame(remaining=1234, total=1800, phase=4))
    assert s["remaining_s"] == 1234
    assert s["total_s"] == 1800


def test_preheat_holds_the_full_duration():
    s = _p(_frame(remaining=600, total=600, phase=3))
    assert (s["remaining_s"], s["total_s"]) == (600, 600)


def test_idle_has_no_timer():
    s = _p(_frame())
    assert s["remaining_s"] is None and s["total_s"] is None


def test_remaining_above_total_is_not_a_timer():
    s = _p(_frame(remaining=900, total=600, phase=4))
    assert s["remaining_s"] is None and s["total_s"] is None


def test_a_total_that_is_not_whole_minutes_keeps_only_the_remaining_time():
    # With a meat probe active the firmware repurposes the "total" bits.
    s = _p(_frame(remaining=600, total=25700, phase=3))
    assert (s["remaining_s"], s["total_s"]) == (600, None)


def test_other_layouts_carry_no_timer():
    plain = _pack([(8, 8, 165), (51, 17, 84699), (104, 16, 17770)])
    s = _p(_encrypt_segment(plain))
    assert s["remaining_s"] is None and s["total_s"] is None


def test_existing_fields_unchanged_by_timer_decode():
    s = _p(_frame(remaining=100, total=600, phase=4))
    assert s["phase"] == "cooking" and s["prompt"] is None and s["lid_open"] is False


# --- probe detail record (mfg2 only: ProbeState.state per probe) -----------
# Bit-exact live-validated 2026-11-06: raw advertisement + the app's own
# decrypted GrillCombinedState log correlated at the same millisecond, twice
# (0xC/0xD1 -> 1/4/0x6/0x7/0x8/... PcsDoneAfterCook, then PcsGetFood).

def _frame43(probe0_state=0, probe1_state=0, probe0_resting_s=0, probe1_resting_s=0, **mfg1_extra):
    """A combined mfg1||mfg2 frame: two independently CTS-encrypted segments."""
    fields = [(8, 8, 13)] + [(o, w, v) for o, (w, v) in mfg1_extra.items()]
    plain43 = bytearray(_pack(fields, length=43))
    for off, width, val in (
        (224, 4, probe0_state), (272, 4, probe1_state),
        (247, 17, probe0_resting_s), (295, 17, probe1_resting_s),
    ):
        for i in range(width):
            if (val >> i) & 1:
                plain43[(off + i) >> 3] |= 1 << ((off + i) & 7)
    plain43 = bytes(plain43)
    return _encrypt_segment(plain43[:20]) + _encrypt_segment(plain43[20:])


def test_mfg1_only_frame_reports_no_probe_state():
    # Without mfg2, there is nothing to decode it from - None, not a guess.
    s = _p(_frame())
    assert s["probe0_state"] is None and s["probe1_state"] is None


def test_probe_done_after_cook_is_decoded():
    s = _p(_frame43(probe1_state=3))
    assert s["probe1_state"] == "done"


def test_probe_get_food_is_decoded():
    s = _p(_frame43(probe1_state=4))
    assert s["probe1_state"] == "get_food"


def test_probe_resting_is_decoded():
    s = _p(_frame43(probe1_state=5))
    assert s["probe1_state"] == "resting"


def test_both_probes_decoded_independently():
    s = _p(_frame43(probe0_state=2, probe1_state=3))
    assert s["probe0_state"] == "cooking"
    assert s["probe1_state"] == "done"


def test_probe_por_and_start_both_read_as_none():
    assert _p(_frame43(probe0_state=0))["probe0_state"] == "none"
    assert _p(_frame43(probe0_state=1))["probe0_state"] == "none"


# --- ProbeState.seconds during PcsResting (@247/@295 w17, counts UP) -------
# Bit-exact live-validated 2026-11-06 against the app's own GrillCombinedState
# log at four independent values (20/70/160/299), correcting an earlier wrong
# guess (a 10-bit field one step earlier in the same record).

def test_resting_seconds_is_decoded_while_resting():
    s = _p(_frame43(probe1_state=5, probe1_resting_s=160))
    assert s["probe1_resting_s"] == 160


def test_resting_seconds_is_none_outside_resting():
    # The field exists in every frame's bit layout, but only means something
    # in PcsResting - treat it as noise/reused bits otherwise.
    s = _p(_frame43(probe1_state=2, probe1_resting_s=160))
    assert s["probe1_resting_s"] is None


def test_resting_seconds_decoded_independently_per_probe():
    s = _p(_frame43(probe0_state=5, probe0_resting_s=20, probe1_state=5, probe1_resting_s=299))
    assert s["probe0_resting_s"] == 20
    assert s["probe1_resting_s"] == 299


# --- Uim power state (live-captured layout: @16 w5)
# 3=PowerOff 4=Idle 5=Hello 7=CookActive 9=PoweringOff (live power-button toggle, 2026-11-06:
# PowerOff -> Hello -> Idle -> PoweringOff -> PowerOff). Hello/PoweringOff only reachable via the
# grill's own physical power button, never observed from a remote Stop/Start/Wake.

def test_uim_power_off_is_flagged():
    plain = _pack([(8, 8, 13), (16, 5, 3)])
    s = _p(_encrypt_segment(plain))
    assert s["powered_off"] is True


def test_uim_idle_is_not_flagged_powered_off():
    plain = _pack([(8, 8, 13), (16, 5, 4)])
    s = _p(_encrypt_segment(plain))
    assert s["powered_off"] is False


def test_uim_cook_active_is_not_flagged_powered_off():
    plain = _pack([(8, 8, 13), (16, 5, 7), (25, 5, 4)])
    s = _p(_encrypt_segment(plain))
    assert s["powered_off"] is False
    assert s["phase"] == "cooking"


def test_uim_powering_off_is_flagged_too():
    # The grill is on its way down, not yet fully off: the card should offer
    # Wake here already rather than waiting for the sequence to finish.
    plain = _pack([(8, 8, 13), (16, 5, 9)])
    s = _p(_encrypt_segment(plain))
    assert s["powered_off"] is True


def test_uim_hello_is_not_flagged_powered_off():
    # The brief post-power-on boot step: the grill is coming up, not asleep.
    plain = _pack([(8, 8, 13), (16, 5, 5)])
    s = _p(_encrypt_segment(plain))
    assert s["powered_off"] is False


def test_uim_idle_is_flagged_as_idle():
    # Ground truth used to release a stuck post-cook hold (see coordinator.py)
    # even with no lid cycle - the grill's own physical Rest countdown can
    # outlast the "get_food" phase/prompt signals by a long time.
    plain = _pack([(8, 8, 13), (16, 5, 4)])
    s = _p(_encrypt_segment(plain))
    assert s["uim_idle"] is True


def test_uim_cook_active_is_not_flagged_as_idle():
    plain = _pack([(8, 8, 13), (16, 5, 7), (25, 5, 4)])
    s = _p(_encrypt_segment(plain))
    assert s["uim_idle"] is False


def test_uim_powered_off_is_not_flagged_as_idle():
    plain = _pack([(8, 8, 13), (16, 5, 3)])
    s = _p(_encrypt_segment(plain))
    assert s["uim_idle"] is False


def test_phase_8_is_lid_open_during_a_wood_fire_cook():
    # Live-validated 2026-11-06: opening the lid mid-cook (wood-fire mode)
    # reports this phase; closing it re-runs ignition (phase 2), not a
    # straight resume into cooking.
    plain = _pack([(8, 8, 13), (25, 5, 8)])
    s = _p(_encrypt_segment(plain))
    assert s["phase"] == "lid_open"


def test_phase_5_is_get_food_when_a_probe_target_is_reached():
    # Bit-exact live-validated 2026-11-06, 3 frames: this field is the
    # firmware's own GCCookState enum (names line up with the already-mapped
    # values - Infusion/Preheat/Heat/LidOpen). Raw 5 = CookStatePause, fired
    # right as the probe hit its target, a couple seconds before (and this
    # grill mode never actually sent) the @98 prompt field.
    plain = _pack([(8, 8, 13), (25, 5, 5)])
    s = _p(_encrypt_segment(plain))
    assert s["phase"] == "get_food"
