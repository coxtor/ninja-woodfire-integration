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
    combined = CT_PROBE7_CLOSED + bytes(23)
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: combined}) == CT_PROBE7_CLOSED
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: bytes(23)}) is None
    assert bc.mfg1_from_advertisement({}) is None


def test_bitread_lsb_first():
    assert bc._bitread(bytes([0b00001010]), 0, 4) == 10
    assert bc._bitread(bytes([0xFF, 0x01]), 6, 4) == 0b0111
