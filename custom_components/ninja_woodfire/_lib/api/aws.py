"""Cloud transport for SharkNinja's AWS backend.

Grills migrated by the Ninja app report and take commands only here; Ayla
keeps serving their last snapshot.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..const import ACTIVE_STATES, COOK_MODES, CloudRegion
from ..models import (
    CombinedState,
    CookState,
    GrillState,
    ProbeState,
    _int,
    _map,
    _parse_value,
)
from .auth0 import Auth0Session
from .commands import cook_command, stop_command
from .errors import NinjaCloudError, TransportError
from .http import send

_LOGGER = logging.getLogger(__name__)

_START_ID = 1001
# A start is only trusted once the grill acknowledges it; an unacknowledged
# one stays queued in the shadow and would fire when the grill next connects.
_ACK_TIMEOUT_S = 20
_ACK_POLL_S = 2
_sleep = asyncio.sleep


class _NotFound(TransportError):
    """A household or device id the API no longer knows."""

# AWS serves the Ayla payloads with every space stripped, from keys and
# enum values alike. Reads are mapped back; commands use the spaced spelling.
_KEYS = {
    "secondsset": "seconds set",
    "secondsleft": "seconds left",
    "probesactive": "probes active",
    "lidopen": "lid open",
    "pluggedin": "plugged in",
    "presetindex": "preset_index",
}
_VALUES = {
    "mode": {m.replace(" ", ""): m for m in COOK_MODES},
    "state": {
        "poweredoff": "powered OFF",
        "getfood": "get food",
        "lidopen": "lid open",
        "zcloss": "zc loss",
        "waitlidclosedbeforecook": "wait lid closed before cook",
    },
}


def normalise(node: Any, key: str | None = None) -> Any:
    """Rewrite an AWS payload into the Ayla spelling."""
    if isinstance(node, dict):
        return {_KEYS.get(k, k): normalise(v, _KEYS.get(k, k)) for k, v in node.items()}
    if isinstance(node, list):
        return [normalise(item, key) for item in node]
    if isinstance(node, str) and key in _VALUES:
        return _VALUES[key].get(node.replace(" ", "").casefold(), node)
    return node


def _timestamp(raw: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _connected(device: dict[str, Any]) -> Any:
    return _map(device.get("connectivityStatus")).get("connected")


def is_connected(device: dict[str, Any]) -> bool:
    """Whether AWS explicitly reports the grill connected."""
    return _connected(device) is True


def _reported_command(device: Any) -> Any:
    shadow = device.get("shadow") if isinstance(device, dict) else None
    shadow = shadow.get("properties", shadow) if isinstance(shadow, dict) else None
    reported = shadow.get("reported") if isinstance(shadow, dict) else None
    return reported.get("Cook_Command") if isinstance(reported, dict) else None


def _command_matches(reported: Any, command: dict[str, Any]) -> bool:
    """Whether the shadow reports back exactly the command we sent.

    Every field we sent has to be there and match: an id alone (or any subset)
    would equally fit a foreign or much older order.
    """
    if not isinstance(reported, dict) or "id" not in reported:
        return False
    reported, sent = normalise(reported), normalise(command)
    return all(key in reported and reported[key] == value for key, value in sent.items())


def state_from_device(dsn: str, device: dict[str, Any]) -> CombinedState:
    """Build the shared state model from one AWS device record."""
    device = _map(device)
    telemetry = _map(device.get("telemetry"))
    connected = _connected(device)

    def blob(name: str) -> dict[str, Any]:
        return normalise(_parse_value(telemetry.get(name)))

    return CombinedState(
        dsn=dsn,
        grill=GrillState.from_property_value(blob("GrillState")),
        cook=CookState.from_property_value(blob("CookState")),
        probes=ProbeState.from_property_value(blob("ProbeState")),
        online=connected is not False,
        connection_status=None if connected is None else "Online" if connected else "Offline",
        last_updated_at=_timestamp(device.get("updatedAt")),
        rssi=_int(telemetry.get("RSSI")),
    )


def _cook_progress(device: Any) -> tuple[dict[str, Any], bool]:
    """(cook-relevant telemetry, whether a cook is running).

    Deliberately ignores RSSI, connectivity and server timestamps: those move
    on their own and must never pass for the grill applying our command.
    """
    state = state_from_device("", device if isinstance(device, dict) else {})
    g, c = state.grill, state.cook
    return (
        {
            "state": g.state,
            "mode": g.mode,
            "setpoint": g.setpoint,
            "smoke": g.smoke,
            "seconds set": g.seconds_set,
            "seconds left": g.seconds_left,
            "cook state": c.state,
            "cook progress": c.progress,
        },
        g.state.casefold() in ACTIVE_STATES,
    )


# Fields a running cook moves on its own; a change in them alone is the timer
# ticking, not the grill reacting to a command.
_COUNTDOWN_FIELDS = ("seconds left", "cook progress")


def _verifiable(sent: dict[str, Any]) -> bool:
    """Whether telemetry can show every field of this command at all.

    Probe targets never appear in the telemetry, grill mode sends a heat level
    instead of the °C the grill reports back, and a skipped preheat is not
    distinguishable from a preheat that simply ended. Commands carrying any of
    those can only be confirmed by the shadow echoing them back.
    """
    if sent.get("probe0") is not None or sent.get("probe1") is not None:
        return False
    if sent.get("skip preheat"):
        return False
    return sent.get("mode") != "grill" or sent.get("temp") is None


def _telemetry_confirms(before: dict[str, Any], after: dict[str, Any],
                        command: dict[str, Any]) -> bool:
    """Whether the telemetry shows *this* command applied.

    Every field we sent has to be reflected, and something beyond the running
    countdown has to have changed: a cook that merely moves from preheat to
    cooking on its own would otherwise pass for an applied duration change.
    """
    sent = normalise(command)
    if not _verifiable(sent):
        return False
    if not any(before.get(key) != value for key, value in after.items()
               if key not in _COUNTDOWN_FIELDS):
        return False
    mode = sent.get("mode")
    if mode is not None and after["mode"] != mode:
        return False
    if after["smoke"] is not None and int(bool(after["smoke"])) != int(bool(sent.get("smoke"))):
        return False
    if sent.get("temp") is not None and after["setpoint"] != sent["temp"]:
        return False
    if sent.get("seconds set") is not None and after["seconds set"] != sent["seconds set"]:
        return False
    return True


class AwsCloudClient:
    """Async client for grills on SharkNinja's AWS backend."""

    def __init__(
        self, auth: Auth0Session, region: CloudRegion, session: aiohttp.ClientSession
    ) -> None:
        self._auth = auth
        self._region = region
        self._http = session
        self._household: str | None = None
        self._device_ids: dict[str, str] = {}

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        """Authenticated request; renews the id_token once on 401."""
        url = f"{self._region.aws_rest_base}{path}"
        token = await self._auth.id_token(self._http)
        for attempt in range(2):
            status, data = await send(
                self._http, method, url, headers=self._headers(token), **kwargs
            )
            if status in (200, 201):
                return data
            if status == 401 and attempt == 0:
                token = await self._auth.refresh(self._http, token)
                continue
            # 403 means a bad API key or a foreign resource, not bad credentials.
            error = _NotFound if status == 404 else TransportError
            raise error(f"{method} {path} {status}: {data}")

    def _headers(self, token: str) -> dict[str, str]:
        now = datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")
        return {
            "authorization": f"Bearer {token}",
            "x-api-key": self._region.aws_api_key,
            "x-iotn-caller": "ENDUSER_MOBILEAPP",
            "x-sn-nonce": "12345",
            "x-sn-date": now.replace("+00:00", "Z"),
            "content-type": "application/json",
        }

    async def _user_id(self) -> str:
        await self._auth.id_token(self._http)
        if not self._auth.user_id:
            raise TransportError("Auth0 id_token carries no user id")
        return self._auth.user_id

    async def _household_id(self) -> str | None:
        if self._household is None:
            data = await self._api(
                "GET", "/householdsEndUser", params={"userId": await self._user_id()}
            )
            households = data.get("households") if isinstance(data, dict) else None
            if households:
                self._household = str(households[0])
        return self._household

    async def _resolving(self, request: Callable[[], Awaitable[Any]]) -> Any:
        """Run `request`, resolving household and device ids afresh once on a 404."""
        try:
            return await request()
        except _NotFound:
            self._household = None
            self._device_ids.clear()
            return await request()

    async def _list_devices(self) -> Any:
        household = await self._household_id()
        if household is None:
            return None
        return await self._api(
            "GET",
            f"/devicesEndUserController/{household}/users/{await self._user_id()}",
            params={"includeRegistry": "true", "includeConnectivityStatus": "true"},
        )

    async def find_device(self, dsn: str) -> dict[str, Any] | None:
        """The grill's device record, matched by Ayla DSN or appliance serial."""
        data = await self._resolving(self._list_devices)
        items = data.get("items") if isinstance(data, dict) else None
        for device in items if isinstance(items, list) else []:
            if not isinstance(device, dict):
                continue
            registry = device.get("registry") or {}
            if not isinstance(registry, dict):
                continue
            ids = {
                str(device.get("deviceId")),
                str(registry.get("serialNumber")),
                *str(registry.get("WiFiModuleSerialNumber", "")).split("-"),
            }
            if dsn in ids:
                if device_id := device.get("deviceId"):
                    self._device_ids[dsn] = str(device_id)
                return device
        return None

    async def get_combined_state(self, dsn: str) -> CombinedState:
        device = await self.find_device(dsn)
        if device is None:
            raise TransportError(f"device {dsn} not found on the AWS backend")
        return state_from_device(dsn, device)

    async def _device_api(self, dsn: str, method: str, **kwargs: Any) -> Any:
        async def request() -> Any:
            if dsn not in self._device_ids:
                await self.find_device(dsn)
            if (device_id := self._device_ids.get(dsn)) is None:
                raise TransportError(f"device {dsn} not found on the AWS backend")
            path = f"/devicesEndUserController/{self._household}/devices/{device_id}"
            return await self._api(method, path, **kwargs)

        return await self._resolving(request)

    async def _send_cook_command(self, dsn: str, command: dict[str, Any] | None) -> Any:
        body = {"shadow": {"properties": {"desired": {"Cook_Command": command}}}}
        return await self._device_api(dsn, "PATCH", json=body)

    async def _acknowledged(
        self,
        dsn: str,
        command: dict[str, Any],
        before: dict[str, Any],
        before_command: Any,
    ) -> bool:
        """Whether the grill itself applied *this* command.

        Only two things count: the shadow newly reporting our own command back,
        or the cook telemetry showing exactly this command applied. A command
        that was already reported before we wrote, and a cook that merely keeps
        counting down, confirm nothing.
        """
        echoed_before = _command_matches(before_command, command)
        for _ in range(_ACK_TIMEOUT_S // _ACK_POLL_S):
            await _sleep(_ACK_POLL_S)
            try:
                reported = _reported_command(await self._device_api(dsn, "GET"))
                if not echoed_before and _command_matches(reported, command):
                    return True
                device = await self.find_device(dsn)
            except TransportError as err:
                _LOGGER.debug("reading back the cook command failed: %s", err)
                continue
            after, cooking = _cook_progress(device)
            if cooking and _telemetry_confirms(before, after, command):
                return True
        return False

    async def _withdraw(self, dsn: str) -> None:
        await self._send_cook_command(dsn, stop_command())

    async def _withdraw_cancelled(self, dsn: str) -> None:
        """Pull a queued start back while this call is being cancelled.

        The withdrawal has to finish before we return: the caller releases the
        command lock on return, and a Stop landing after the next command would
        cancel that cook. Further cancellations (a second Stop press) therefore
        do not end the wait.
        """
        task = asyncio.ensure_future(self._withdraw(dsn))
        while True:
            try:
                await asyncio.shield(task)
                return
            except asyncio.CancelledError:
                if task.done():
                    raise
            except NinjaCloudError as err:
                _LOGGER.warning(
                    "Withdrawing the cancelled start for %s failed: %s", dsn, err
                )
                return

    async def _start(self, dsn: str, command: dict[str, Any]) -> Any:
        """Send a start only to a connected grill and withdraw it unless acknowledged."""
        device = await self.find_device(dsn)
        if device is None:
            raise TransportError(f"device {dsn} not found on the AWS backend")
        if not is_connected(device):
            raise TransportError("grill is not connected to the AWS cloud")
        before, _cooking = _cook_progress(device)
        # Refuse to write a start we would not be able to verify afterwards, and
        # keep what the shadow already reported as the comparison baseline.
        before_command = _reported_command(await self._device_api(dsn, "GET"))
        # From here on the start may have reached the shadow, so every failure
        # has to pull it back — an un-withdrawn start fires whenever the grill
        # next connects.
        try:
            result = await self._send_cook_command(dsn, command)
            acknowledged = await self._acknowledged(dsn, command, before, before_command)
        except asyncio.CancelledError:
            await self._withdraw_cancelled(dsn)
            raise
        except NinjaCloudError as err:
            try:
                await self._withdraw(dsn)
            except NinjaCloudError as undo:
                raise TransportError(
                    f"{err}; the start may have been queued and withdrawing it "
                    f"also failed: {undo}"
                ) from err
            raise
        if not acknowledged:
            try:
                await self._withdraw(dsn)
            except TransportError as err:
                raise TransportError(
                    f"grill did not acknowledge the command and withdrawing it failed: {err}"
                ) from err
            raise TransportError("grill did not acknowledge the command; it was withdrawn")
        try:
            await self._send_cook_command(dsn, None)
        except TransportError as err:
            _LOGGER.debug("clearing the delivered cook command failed: %s", err)
        return result

    async def start_cook(
        self,
        dsn: str,
        *,
        mode: str,
        seconds: int,
        temp: int,
        smoke: bool = False,
        skip_preheat: bool = False,
        probe_0: dict[str, Any] | None = None,
        probe_1: dict[str, Any] | None = None,
        device_key: int | None = None,
    ) -> Any:
        """Start (or update) a cook; same arguments as the Ayla client."""
        payload = cook_command(
            mode=mode,
            seconds=seconds,
            temp=temp,
            smoke=smoke,
            skip_preheat=skip_preheat,
            probe_0=probe_0,
            probe_1=probe_1,
        )
        return await self._start(dsn, {"id": _START_ID, **payload})

    async def stop_cook(self, dsn: str) -> Any:
        return await self._send_cook_command(dsn, stop_command())
