"""BLE transport logic tests with a fake radio link."""
from __future__ import annotations

import pytest

from custom_components.ninja_woodfire._lib.api import ble_proto as bp
from custom_components.ninja_woodfire._lib.api.ble import BleTransport
from custom_components.ninja_woodfire._lib.api.errors import TransportError

TX = (bytes(range(32)), bytes(range(16)))
RX = (bytes(range(32, 64)), bytes(range(16, 32)))
TOKEN = 0x11223344

# A real live grill-cook state body (grill, heat level 2, 300 s remaining, ~51 °C).
STATE_BODY = bytes.fromhex(
    "0f8aa5a5a5a5a5a5a5a5a5a5070000000100ea007c232b5c2c0100002c0100000000000000000000000001000000000004000000000000000000000000000000000000000000000000000000000000000100690000000000000000000000000000000000000000000000000070fefe01fe010000000090ff1010660cc50f0000fb01fcfffe01fb01fcff"
)


def _rx_frame(command: bytes, token: int) -> bytes:
    return bp.encode_frame(command, RX[0], RX[1], token=token)


class FakeLink:
    def __init__(self, batches: list[list[bytes]]):
        self._batches = batches
        self.writes: list[bytes] = []
        self.connected = False
        self.b001_reads = 0

    async def connect(self):
        self.connected = True

    async def read_b001(self):
        self.b001_reads += 1
        return bytes(96)

    async def write_b002(self, frame):
        self.writes.append(frame)

    async def collect(self, seconds):
        return self._batches.pop(0) if self._batches else []

    async def disconnect(self):
        self.connected = False


@pytest.mark.asyncio
async def test_get_combined_state_from_live_frames():
    info_body = bytes.fromhex("0c3a41433030305730333630323534343400000000000000c0ff22040300b300010103000000534e443030303030353335393635300000000000")
    link = FakeLink([
        [_rx_frame(b"\x10" + bytes(22), TOKEN)],   # handshake -> token
        [_rx_frame(STATE_BODY, TOKEN), _rx_frame(info_body, TOKEN)],  # state + grill-info
    ])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    state = await t.get_combined_state("SND1")
    assert t.info.get("serial") == "SND000000000000"
    assert link.b001_reads == 1          # b001 read triggers the stream
    assert state.online is True and state.connection_status == "Online"
    assert state.grill.mode == "grill"
    assert state.grill.seconds_left == 300
    assert state.grill.state == "cooking"
    assert state.grill.end_time_utc is not None  # derived from seconds_left for the card
    assert state.last_updated_at is not None


@pytest.mark.asyncio
async def test_start_cook_sends_encoded_command_with_token():
    link = FakeLink([[_rx_frame(b"\x10" + bytes(22), TOKEN)]])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    await t.start_cook("SND1", mode="grill", seconds=600, temp=2)
    assert len(link.writes) == 2         # CONNECT + command
    d = bp.decode_frame(link.writes[-1], TX[0], TX[1])
    assert d is not None and d["token"] == TOKEN
    assert d["command"] == bp.build_cook(mode="grill", temp=2, seconds=600)


@pytest.mark.asyncio
async def test_stop_cook_sends_stop():
    link = FakeLink([[_rx_frame(b"\x10" + bytes(22), TOKEN)]])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    await t.stop_cook("SND1")
    d = bp.decode_frame(link.writes[-1], TX[0], TX[1])
    assert d["command"] == bp.build_stop()


@pytest.mark.asyncio
async def test_no_token_raises_transport_error():
    link = FakeLink([[_rx_frame(b"\x10" + bytes(22), 0)]])  # token stays 0
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    with pytest.raises(TransportError):
        await t.get_combined_state("SND1")


@pytest.mark.asyncio
async def test_start_cook_rejects_bad_mode():
    link = FakeLink([[_rx_frame(b"\x10" + bytes(22), TOKEN)]])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    with pytest.raises(ValueError):
        await t.start_cook("SND1", mode="nonsense", seconds=600, temp=2)
