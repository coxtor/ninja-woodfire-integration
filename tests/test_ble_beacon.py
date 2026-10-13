"""Passive advertisement beacon decoder tests.

The beacon is encrypted (AES-CBC with ciphertext-stealing, global slot-0 key);
the decoder was reversed from the firmware and validated byte-exact against live
on-grill captures (probe field == displayed °C, lid bit toggles with the lid).

Fixtures are synthetic: chosen plaintext fields (no real device data — the MAC
lives in the scan-response segment we never decode) encrypted with the slot-0
key, so decrypting them must recover the fields. Generated with the inverse of
_decrypt_segment; see tools/beacon/decrypt_beacon.py in the research repo.
"""
from __future__ import annotations

from custom_components.ninja_woodfire._lib.api import ble_beacon as bc

# Encrypted 20-byte mfg1 segments with known fields:
CT_PROBE7_CLOSED = bytes.fromhex("0000000000000000000000000000000000000000")   # probe0 7°C, lid closed
CT_PROBE21 = bytes.fromhex("0000000000000000000000000000000000000000")          # probe0 21°C
CT_PROBE53_CLOSED = bytes.fromhex("0000000000000000000000000000000000000000")   # probe0 53°C, lid closed
CT_PROBE12_OPEN = bytes.fromhex("0000000000000000000000000000000000000000")      # probe0 12°C, lid open
CT_BOTH_PROBES = bytes.fromhex("0000000000000000000000000000000000000000")        # probe0 40°C, probe1 65°C


def test_probe0_temperature_confirmed():
    # Probe 0 reading = raw °C; probe 1 not plugged (field 0 -> None).
    s = bc.parse_beacon(CT_PROBE7_CLOSED)
    assert s["probe0_temp"] == 7.0
    assert s["probe1_temp"] is None

    assert bc.parse_beacon(CT_PROBE21)["probe0_temp"] == 21.0
    assert bc.parse_beacon(CT_PROBE53_CLOSED)["probe0_temp"] == 53.0


def test_both_probes():
    s = bc.parse_beacon(CT_BOTH_PROBES)
    assert s["probe0_temp"] == 40.0
    assert s["probe1_temp"] == 65.0


def test_lid_bit():
    assert bc.parse_beacon(CT_PROBE12_OPEN)["lid_open"] is True
    assert bc.parse_beacon(CT_PROBE53_CLOSED)["lid_open"] is False


def test_mfg1_alone_is_enough():
    # Home Assistant delivers the main-advertisement manufacturer data (20 bytes);
    # every field sits within it, so a 20-byte ciphertext must still decode.
    assert bc.parse_beacon(CT_PROBE7_CLOSED[:20])["probe0_temp"] == 7.0


def test_rejects_short_or_empty():
    assert bc.parse_beacon(b"") is None
    assert bc.parse_beacon(None) is None
    assert bc.parse_beacon(bytes(19)) is None


def test_mfg1_from_advertisement_prefers_raw():
    # Raw adv: 02 01 06 | 03 03 bb fc | 17 ff 4f 0c <20B mfg1> ...
    raw = bytes.fromhex("0201060303bbfc") + bytes([0x17, 0xFF, 0x4F, 0x0C]) + CT_PROBE7_CLOSED
    assert bc.mfg1_from_advertisement({}, raw) == CT_PROBE7_CLOSED


def test_mfg1_from_advertisement_dict_variants():
    # Plain mfg1 under the company id.
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: CT_PROBE7_CLOSED}) == CT_PROBE7_CLOSED
    # Combined mfg1||mfg2 -> take the first 20 bytes.
    combined = CT_PROBE7_CLOSED + bytes(23)
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: combined}) == CT_PROBE7_CLOSED
    # Only the scan-response (mfg2, 23 bytes) -> none of our fields, rejected.
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: bytes(23)}) is None
    assert bc.mfg1_from_advertisement({}) is None


def test_bitread_lsb_first():
    # 0b1010 at bit 0, width 4 -> 0b1010 = 10 (LSB-first).
    assert bc._bitread(bytes([0b00001010]), 0, 4) == 10
    # read across a byte boundary
    assert bc._bitread(bytes([0xFF, 0x01]), 6, 4) == 0b0111
