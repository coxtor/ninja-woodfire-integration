"""Passive advertisement beacon decoder tests, pinned to real on-grill captures.

Fixtures are real manufacturer payloads (docs/captures/live/beacon_bitfields.json);
the probe-0 field was confirmed against the grill display (field value == °C).
"""
from __future__ import annotations

from custom_components.ninja_woodfire._lib.api import ble_beacon as bc

# Full 43-byte payloads (mfg1||mfg2) as broadcast; the fields we read all live in
# the first 20 bytes (the main advertisement's manufacturer data).
PAYLOAD_PROBE7 = bytes.fromhex(
    "000d27082000ed0fd0d70100009402ccf9f019340123456789ab0e0800000000000000000000001d8e2021"
)
PAYLOAD_PROBE21 = bytes.fromhex(
    "000d27082000b51050580500009402cc8aba29340123456789ab0e080000000000000000000000ba009021"
)


def test_probe0_temperature_confirmed():
    # Probe 0 reading = raw °C; probe 1 not plugged (field 0 -> None).
    s = bc.parse_beacon(PAYLOAD_PROBE7)
    assert s["probe0_temp"] == 7.0
    assert s["probe1_temp"] is None

    s = bc.parse_beacon(PAYLOAD_PROBE21)
    assert s["probe0_temp"] == 21.0


def test_mfg1_alone_is_enough():
    # Home Assistant delivers the main-advertisement manufacturer data (20 bytes);
    # the probe field sits within it, so a 20-byte payload must still decode.
    mfg1 = PAYLOAD_PROBE7[:20]
    assert bc.parse_beacon(mfg1)["probe0_temp"] == 7.0


def test_rejects_short_or_empty():
    assert bc.parse_beacon(b"") is None
    assert bc.parse_beacon(None) is None
    assert bc.parse_beacon(bytes(19)) is None


def test_mfg1_from_advertisement_prefers_raw():
    # Raw adv: 02 01 06 | 03 03 bb fc | 17 ff 4f 0c <20B mfg1> ...
    mfg1 = PAYLOAD_PROBE7[:20]
    raw = bytes.fromhex("0201060303bbfc") + bytes([0x17, 0xFF, 0x4F, 0x0C]) + mfg1
    assert bc.mfg1_from_advertisement({}, raw) == mfg1


def test_mfg1_from_advertisement_dict_variants():
    mfg1 = PAYLOAD_PROBE7[:20]
    # Plain mfg1 under the company id (type tag 0x0d at byte 1).
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: mfg1}) == mfg1
    # Combined mfg1||mfg2 -> take the first 20 bytes.
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: PAYLOAD_PROBE7}) == mfg1
    # Only the scan-response (mfg2, starts with the MAC) -> cannot use it.
    mfg2 = PAYLOAD_PROBE7[20:]
    assert bc.mfg1_from_advertisement({bc.COMPANY_ID: mfg2}) is None
    assert bc.mfg1_from_advertisement({}) is None


def test_bitread_lsb_first():
    # 0b1010 at bit 0, width 4 -> 0b1010 = 10 (LSB-first).
    assert bc._bitread(bytes([0b00001010]), 0, 4) == 10
    # read across a byte boundary
    assert bc._bitread(bytes([0xFF, 0x01]), 6, 4) == 0b0111
