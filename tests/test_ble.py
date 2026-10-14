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
    def __init__(self, batches: list[list[bytes]], *,
                 connect_errors: int = 0, collect_errors: int = 0):
        self._batches = batches
        self.writes: list[bytes] = []
        self.connected = False
        self.b001_reads = 0
        self.disconnects = 0
        self._connect_errors = connect_errors
        self._collect_errors = collect_errors

    async def connect(self):
        if self._connect_errors > 0:
            self._connect_errors -= 1
            raise Exception(
                "Bluetooth GATT Error address=AA:BB:CC:DD:EE:FF handle=14 error=133"
            )
        self.connected = True

    async def read_b001(self):
        assert self.connected, "read before connect"
        self.b001_reads += 1
        return bytes(96)

    async def write_b002(self, frame):
        assert self.connected, "write before connect"   # mirrors HaBleLink
        self.writes.append(frame)

    async def collect(self, seconds):
        if self._collect_errors > 0:
            self._collect_errors -= 1
            raise Exception("disconnected")
        return self._batches.pop(0) if self._batches else []

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1


@pytest.mark.asyncio
async def test_get_combined_state_from_live_frames():
    info_body = bytes.fromhex("0c3a41433030305730333630323534343400000000000000c0ff22040300b300010103000000534e443030303030303030303030300000000000")
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
async def test_get_combined_state_sends_no_cook_command_and_releases_link():
    # Reading state must never send a cook/stop command (that would cancel a
    # running cook); it only CONNECTs and listens. Afterwards it disconnects so
    # the grill resumes advertising its beacon.
    link = FakeLink([
        [_rx_frame(b"\x10" + bytes(22), TOKEN)],   # handshake -> token
        [_rx_frame(STATE_BODY, TOKEN)],            # grill pushes state
    ])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    await t.get_combined_state("SND1")
    commands = [d["command"] for w in link.writes
                if (d := bp.decode_frame(w, TX[0], TX[1]))]
    assert commands == [bp.CONNECT_CMD]   # only CONNECT, nothing else
    assert link.disconnects >= 1          # link released for the beacon


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
    t = BleTransport(TX, RX, link, listen_seconds=0.01, retry_backoff=0)
    with pytest.raises(TransportError):
        await t.get_combined_state("SND1")


@pytest.mark.asyncio
async def test_get_combined_state_retries_after_transient_error():
    # A flaky connect (GATT 133) on the first attempt should be retried, and the
    # slot freed (disconnect) before the retry succeeds.
    link = FakeLink([
        [_rx_frame(b"\x10" + bytes(22), TOKEN)],
        [_rx_frame(STATE_BODY, TOKEN)],
    ], connect_errors=1)
    t = BleTransport(TX, RX, link, listen_seconds=0.01, read_attempts=3, retry_backoff=0)
    state = await t.get_combined_state("SND1")
    assert state.grill.state == "cooking"
    assert state.grill.seconds_left == 300
    assert link.disconnects >= 1          # freed the slot before retrying


@pytest.mark.asyncio
async def test_state_frame_arriving_with_the_token_is_not_discarded():
    # The 0x0f state frame streams in the *same* listen window as the CONNECT
    # token reply; it must be kept, not thrown away when the handshake returns.
    link = FakeLink([
        [_rx_frame(STATE_BODY, TOKEN), _rx_frame(b"\x10" + bytes(22), TOKEN)],
    ])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    state = await t.get_combined_state("SND1")
    assert state.grill.seconds_left == 300
    assert state.grill.state == "cooking"


@pytest.mark.asyncio
async def test_get_combined_state_never_sends_a_command():
    # Reading state must never write a cook/stop command (that beeps and would
    # cancel a cook); it only CONNECTs and listens.
    link = FakeLink([[_rx_frame(b"\x10" + bytes(22), TOKEN)], [_rx_frame(STATE_BODY, TOKEN)]])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    await t.get_combined_state("SND1")
    commands = [d["command"] for w in link.writes
                if (d := bp.decode_frame(w, TX[0], TX[1]))]
    assert commands == [bp.CONNECT_CMD]   # only CONNECT, nothing else


@pytest.mark.asyncio
async def test_get_combined_state_waits_for_later_state_frame():
    # The handshake token arrives first; the 0x0f state frame only shows up a
    # couple of listen rounds later (slow low-and-slow smoke cadence).
    link = FakeLink([
        [_rx_frame(b"\x10" + bytes(22), TOKEN)],   # handshake -> token
        [],                                         # listen round 1: nothing
        [_rx_frame(STATE_BODY, TOKEN)],             # listen round 2: state
    ])
    t = BleTransport(TX, RX, link, listen_seconds=0.001, state_rounds=4)
    state = await t.get_combined_state("SND1")
    assert state.grill.seconds_left == 300


@pytest.mark.asyncio
async def test_persistent_failure_frees_slot_and_raises():
    link = FakeLink([], connect_errors=99)
    t = BleTransport(TX, RX, link, listen_seconds=0.001, read_attempts=3, retry_backoff=0)
    with pytest.raises(TransportError):
        await t.get_combined_state("SND1")
    assert link.disconnects == 3          # one slot-release per failed attempt


@pytest.mark.asyncio
async def test_command_after_disconnecting_poll_reconnects():
    # get_combined_state releases the link; a following command must reconnect
    # (and never write while disconnected).
    link = FakeLink([
        [_rx_frame(STATE_BODY, TOKEN)],            # poll handshake (token + state)
        [_rx_frame(b"\x10" + bytes(22), TOKEN)],   # command handshake
    ])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    await t.get_combined_state("SND1")
    assert link.connected is False                 # poll released the link
    await t.stop_cook("SND1")                       # must reconnect, not assert
    assert link.connected is True
    assert bp.decode_frame(link.writes[-1], TX[0], TX[1])["command"] == bp.build_stop()


@pytest.mark.asyncio
async def test_start_cook_rejects_bad_mode():
    link = FakeLink([[_rx_frame(b"\x10" + bytes(22), TOKEN)]])
    t = BleTransport(TX, RX, link, listen_seconds=0.01)
    with pytest.raises(ValueError):
        await t.start_cook("SND1", mode="nonsense", seconds=600, temp=2)
