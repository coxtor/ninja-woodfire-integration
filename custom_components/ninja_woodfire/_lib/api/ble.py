"""Local BLE transport.

Implements the GrillTransport protocol over the grill's BLE GATT service using
the wire codec in `ble_proto`. The actual radio I/O is abstracted behind
`BleLink` so the protocol logic is testable and the HA-bluetooth/bleak glue
(BT-proxy capable) lives in the integration layer.

Protocol, as reversed in docs/BLE_ESTABLISH_SESSION.md:
  connect -> read characteristic b001 (kicks off the grill's notify stream)
          -> write CONNECT (b002), grill assigns a session token on b004
          -> send commands (b002) carrying that token
State frames (type 0x0f) stream on b004 while a cook is active; a grill-info
burst (type 0x0c) arrives on connect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from ..models import CombinedState, CookState, GrillState, GrillTemps, ProbeState
from . import ble_proto as bp
from .commands import cook_command  # reused only for validation/shape parity
from .errors import TransportError

_STATE_TYPE = 0x0F
_INFO_TYPE = 0x0C


class BleLink(Protocol):
    """Thin BLE I/O used by the transport; the HA layer supplies the real one."""

    async def connect(self) -> None: ...
    async def read_b001(self) -> bytes: ...
    async def write_b002(self, frame: bytes) -> None: ...
    async def collect(self, seconds: float) -> list[bytes]: ...
    async def disconnect(self) -> None: ...


class BleTransport:
    """GrillTransport over BLE for a single grill."""

    def __init__(self, tx_slot: tuple[bytes, bytes], rx_slot: tuple[bytes, bytes],
                 link: BleLink, *, listen_seconds: float = 3.0,
                 connect_attempts: int = 3) -> None:
        self._tx_key, self._tx_nonce = tx_slot
        self._rx_key, self._rx_nonce = rx_slot
        self._link = link
        self._listen = listen_seconds
        self._attempts = max(1, connect_attempts)
        self._token: int | None = None
        self.info: dict[str, str] = {}   # grill-info (serial, model) from 0c frames

    # -- helpers ------------------------------------------------------------

    def _decode(self, frame: bytes) -> dict | None:
        return bp.decode_frame(frame, self._rx_key, self._rx_nonce)

    async def _handshake(self) -> int:
        """Connect, read b001, send CONNECT, return the assigned session token.

        Reading b001 kicks off the grill's notify stream. The grill assigns a
        session token in its reply to CONNECT. A sleeping grill can take a
        moment or miss the first CONNECT, so retry a few times with a listen
        window each (robust over Bluetooth proxies, which add latency).
        """
        await self._link.connect()
        await self._link.read_b001()
        connect = bp.encode_frame(bp.CONNECT_CMD, self._tx_key, self._tx_nonce, token=0)
        for _ in range(self._attempts):
            await self._link.write_b002(connect)
            for frame in await self._link.collect(self._listen):
                d = self._decode(frame)
                if d and d["token"]:
                    self._token = d["token"]
                    return d["token"]
        raise TransportError("no session token from grill (CONNECT not accepted)")

    async def _send_command(self, command: bytes) -> None:
        token = await self._handshake()
        await self._link.write_b002(
            bp.encode_frame(command, self._tx_key, self._tx_nonce, token=token)
        )

    # -- GrillTransport -----------------------------------------------------

    async def get_combined_state(self, dsn: str) -> CombinedState:
        try:
            await self._handshake()
            frames = [self._decode(f) for f in await self._link.collect(self._listen)]
        except TransportError:
            raise
        except Exception as err:  # noqa: BLE001 - normalise radio errors
            raise TransportError(f"BLE read failed: {err}") from err
        bodies = [d["command"] for d in frames if d and d["command"]]
        for body in bodies:
            if body[:1] == bytes([_INFO_TYPE]):
                parsed = bp.parse_grill_info(body)
                if parsed:
                    self.info = parsed
        state = _latest(bodies, _STATE_TYPE)
        return _combined_state(dsn, state)

    async def start_cook(self, dsn: str, *, mode: str, seconds: int, temp: int,
                         smoke: bool = False, skip_preheat: bool = False,
                         probe_0: dict[str, Any] | None = None,
                         probe_1: dict[str, Any] | None = None,
                         device_key: int | None = None) -> None:
        # Validate against the shared command spec (raises ValueError on bad input).
        cook_command(mode=mode, seconds=seconds, temp=temp, smoke=smoke,
                     skip_preheat=skip_preheat, probe_0=probe_0, probe_1=probe_1)
        command = bp.build_cook(
            mode=mode, temp=temp, seconds=seconds, smoke=smoke,
            skip_preheat=skip_preheat,
            probe0=(probe_0 or {}).get("setpoint"),
            probe1=(probe_1 or {}).get("setpoint"),
        )
        await self._send_command(command)

    async def stop_cook(self, dsn: str) -> None:
        await self._send_command(bp.build_stop())

    async def close(self) -> None:
        try:
            await self._link.disconnect()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


def _latest(bodies: list[bytes], frame_type: int) -> dict | None:
    for body in reversed(bodies):
        if body[:1] == bytes([frame_type]):
            return bp.parse_grill_state(body)
    return None


# Modes whose chamber reading is the convection/air sensor (mirrors the cloud
# model's per-mode temperature semantics).
_AIR_MODES = frozenset({"air crisp", "bake", "roast", "reheat", "dehydrate"})


def _chamber_temps(mode: str | None, chamber: float | None) -> GrillTemps:
    """Put the single chamber reading on the sensor the mode uses."""
    if chamber is None:
        return GrillTemps()
    if mode in _AIR_MODES:
        return GrillTemps(air=chamber)
    return GrillTemps(grill=chamber)


def _combined_state(dsn: str, state: dict | None) -> CombinedState:
    """Build CombinedState from a parsed GrillState frame.

    The chamber temperature is decoded and placed on the grill or air sensor to
    match the mode (as the cloud model does). The meat-probe reading is not in
    this frame (firmware ships it only in the encrypted advertisement beacon),
    so probes stay empty and the card hides them.
    """
    now = datetime.now(tz=timezone.utc)
    grill = GrillState()
    cook = CookState()
    if state:
        active = state.get("active")
        left = state.get("seconds_left")
        # Derive an end timestamp from the remaining seconds so the card's
        # countdown + progress bar work over BLE (the firmware's endtimeutc is
        # cloud-only; the card keys the whole progress UI off it).
        end_utc = int(now.timestamp()) + left if active and left else None
        grill = GrillState(
            state="cooking" if active else "idle",
            mode=state.get("mode"),
            setpoint=state.get("setpoint"),
            seconds_set=state.get("seconds_set"),
            seconds_left=left,
            end_time_utc=end_utc,
            temps=_chamber_temps(state.get("mode"), state.get("chamber")),
        )
        cook = CookState(state="cooking" if active else "none")
    # The lid state is not decoded over the local path; report it as unknown
    # rather than defaulting to "closed".
    grill.lid_open = None
    return CombinedState(
        dsn=dsn,
        grill=grill,
        cook=cook,
        probes=ProbeState(),
        online=True,
        connection_status="Online",
        last_updated_at=now,
    )
