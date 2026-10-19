"""BLE wire-protocol codec tests.

This package ships no key material; the codec takes the session slot (key,
nonce) from the caller. These tests use an obviously-fake key and self-made
frames, so they exercise the framing/codec without any real device key.
"""
from __future__ import annotations

import base64
import struct

import pytest

from custom_components.ninja_woodfire._lib.api import ble_proto as bp

# Fake session slot — NOT a real device key.
FAKE_KEY = bytes(range(32))
FAKE_IV = bytes(range(16))


def test_cbc_roundtrip_known_block():
    # AES-256-CBC decrypt of a block we encrypted ourselves with the fake key.
    ct = bytes.fromhex("799383df109c2f0844027f6b2efb1fd1")
    want = bytes.fromhex("0123456789ab0e696fea099a6ce2fe4f")
    assert bp.decrypt(ct, FAKE_KEY, FAKE_IV) == want
    assert bp.encrypt(want, FAKE_KEY, FAKE_IV) == ct


def test_frame_roundtrip_byte_exact():
    cmd = bytes([0x10]) + bytes(22)  # CONNECT-shaped, 23 bytes
    frame = bp.encode_frame(cmd, FAKE_KEY, FAKE_IV,
                            session_id=0x11223344, token=0x55667788, field2=0, pad=bytes(8))
    d = bp.decode_frame(frame, FAKE_KEY, FAKE_IV)
    assert d is not None and d["command"] == cmd
    assert d["token"] == 0x55667788 and d["session_id"] == 0x11223344
    pad = d["payload"][17 + len(cmd):]
    again = bp.encode_frame(d["command"], FAKE_KEY, FAKE_IV,
                            session_id=d["session_id"], token=d["token"],
                            field2=d["field2"], pad=pad)
    assert again == frame


def test_decode_frame_rejects_wrong_key():
    frame = bp.encode_frame(bytes([0x10]) + bytes(22), FAKE_KEY, FAKE_IV, pad=bytes(8))
    assert bp.decode_frame(frame, bytes(32), bytes(16)) is None


def test_decode_frame_rejects_implausible_length():
    # Magic matches but the declared command length can't fit the buffer.
    head = (struct.pack("<I", 1) + struct.pack("<I", bp.FRAME_MAGIC)
            + struct.pack("<I", 0) + struct.pack("<I", 0) + bytes([200]))
    head += bytes((-len(head)) % 16)
    frame = bp.encrypt(bp._mask(head), FAKE_KEY, FAKE_IV)
    assert bp.decode_frame(frame, FAKE_KEY, FAKE_IV) is None


def test_build_cook_matches_capture():
    # Real OTA cook: grill, heat level 3, 600 s -> command bytes 0a0001000300 5802...
    cmd = bp.build_cook(mode="grill", temp=3, seconds=600)
    assert cmd.hex() == "0a00010003005802000000000000000000000000000000"


def test_build_cook_modes_and_probes():
    assert bp.build_cook(mode="air crisp", temp=200, seconds=600)[2] == 3
    assert bp.build_cook(mode="reheat", temp=150, seconds=600)[2] == 8
    # probe targets 65 / 75 °C encode as the captured 6-byte slots
    cmd = bp.build_cook(mode="grill", temp=3, seconds=600, probe0=65, probe1=75)
    assert cmd[10:16].hex() == "410000630001"
    assert cmd[16:22].hex() == "4b0000630001"


def test_build_cook_rejects_unknown_mode():
    with pytest.raises(ValueError):
        bp.build_cook(mode="broil", temp=200, seconds=600)


def test_build_cook_rejects_out_of_range():
    # A heating-element command must reject out-of-range values, not wrap them.
    with pytest.raises(ValueError):
        bp.build_cook(mode="bake", temp=600, seconds=600)
    with pytest.raises(ValueError):
        bp.build_cook(mode="grill", temp=-1, seconds=600)
    with pytest.raises(ValueError):
        bp.build_cook(mode="grill", temp=3, seconds=100000)


def test_build_stop():
    assert bp.build_stop().hex() == "0a00010000000000000000000000000000000000000000"


def test_parse_grill_state_prefix_grill():
    # Live grill cook, heat level 2, 300 s remaining (clean, no probe).
    f = bytes.fromhex(
        "0f8aa5a5a5a5a5a5a5a5a5a5070000000100ea007c232b5c2c0100002c0100000000000000000000000001000000000004000000000000000000000000000000000000000000000000000000000000000100690000000000000000000000000000000000000000000000000070fefe01fe010000000090ff1010660cc50f0000fb01fcfffe01fb01fcff"
    )
    s = bp.parse_grill_state(f)
    assert s["mode"] == "grill"
    assert s["seconds_set"] == 300 and s["seconds_left"] == 300
    assert s["active"] is True
    # Chamber reading ×20: raw 510 @110 -> 25.5 °C; no probe plugged.
    assert s["chamber"] == 25.5
    assert s["probe0_target"] is None


def test_parse_grill_state_temps_bake_smoker_probe():
    bake = bytes.fromhex(
        "0f8aa5a5a5a5a5a5a5a5a5a5070000000400b40023fecd6a2c010000000000000000000000"
        "0000000000000100000000000400640001000b0000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000070fe00000000"
        "5401540190ff6d0db60b8c0c0000fcff5501fcfffcff5201"
    )
    s = bp.parse_grill_state(bake)
    assert s["mode"] == "bake" and s["setpoint"] == 180
    assert s["chamber"] == 17.0  # raw 340 @114 / 20
    assert s["probe0_target"] is None

    probe = bytes.fromhex(
        "0f8aa5a5a5a5a5a5a5a5a5a5070000000100ea00ebfccd6a2c0100002c010000010000000000"
        "0000000001000000000004005700010116000000000046000000010000000000000000000000"
        "030000000000000000000000000000000000000000000000000000000000000070fe00000000"
        "6801680190fff212cf0cac110000fcff6701fcfffcff6501"
    )
    s = bp.parse_grill_state(probe)
    assert s["mode"] == "grill" and s["setpoint"] == 234
    assert s["chamber"] == 18.0  # raw 360 @114 / 20
    assert s["probe0_target"] == 70


def test_parse_grill_state_chamber_hot():
    hot = bytes.fromhex(
        "0f8aa5a5a5a5a5a5a5a5a5a50700000001000801cd10ce6a2c0100002c0100000100000000"
        "000000010001000000000005003e0001015b070000000046000000010000000000000000000000"
        "640000000000000000000000000000000000000000000000000000000000000070fe000000007e"
        "0e7e0e90ff2011930c57090000fcff7d0efcfffcff910e"
    )
    s = bp.parse_grill_state(hot)
    assert s["mode"] == "grill"
    assert s["chamber"] == 185.5


def test_parse_grill_state_prefix_aircrisp():
    f1 = bytes.fromhex(
        "0f8aa5a5a5a5a5a5a5a5a5a5070000000300c800d1172b5c580200005802000000000000"
        "00000000000001000000000000000c000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000000070fe"
        "000000000000000090ffc712cb0c07110000fcfffcfffcfffcfffcff"
    )
    s = bp.parse_grill_state(f1)
    assert s["mode"] == "air crisp" and s["setpoint"] == 200
    assert s["seconds_set"] == 600


def test_parse_slots_blob_hex_and_base64():
    tx = (bytes(range(0, 32)), bytes(range(32, 48)))
    rx = (bytes(range(48, 80)), bytes(range(80, 96)))
    beac = (bytes(range(96, 128)), bytes(range(128, 144)))
    raw = b"".join((tx[0], tx[1], rx[0], rx[1], beac[0], beac[1]))
    assert len(raw) == 144
    hexform = " ".join(x.hex() for x in (tx[0], tx[1], rx[0], rx[1], beac[0], beac[1]))
    for blob in (hexform, base64.b64encode(raw).decode()):
        atx, arx, abeac = bp.parse_slots_blob(blob)
        assert atx == tx and arx == rx and abeac == beac


def test_parse_slots_blob_rejects_wrong_size():
    with pytest.raises(ValueError):
        bp.parse_slots_blob("deadbeef")


def test_parse_grill_info():
    body = bytes.fromhex(
        "0c3a41433030305730333630323534343400000000000000c0ff22040300b3000101"
        "03000000534e443030303030303030303030300000000000"
    )
    info = bp.parse_grill_info(body)
    assert info["model"] == "AC000W036025444"
    assert info["serial"] == "SND000000000000"
    assert bp.parse_grill_info(b"\x0f\x00") == {}


def test_short_frames_are_rejected_without_an_exception():
    # I-09: a 16-byte frame passes the magic check but has no length byte, so
    # reading it raised IndexError. Every under-sized frame must be rejected.
    key, iv = bytes(range(32)), bytes(range(16))
    pt = bytearray(16)
    pt[4:8] = struct.pack("<I", bp.FRAME_MAGIC)
    forged = bp.encrypt(bp._mask(bytes(pt)), key, iv)
    assert len(forged) == 16
    assert bp.decode_frame(forged, key, iv) is None

    for size in range(32):
        assert bp.decode_frame(bytes(size), key, iv) is None


def test_a_full_frame_still_decodes():
    key, iv = bytes(range(32)), bytes(range(16))
    frame = bp.encode_frame(bp.CONNECT_CMD, key, iv, token=7)
    assert bp.decode_frame(frame, key, iv)["token"] == 7
