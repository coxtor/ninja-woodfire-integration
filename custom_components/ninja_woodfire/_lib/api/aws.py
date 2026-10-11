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

from ..const import COOK_MODES, CloudRegion
from ..models import CombinedState, CookState, GrillState, ProbeState, _parse_value
from .auth0 import Auth0Session
from .commands import cook_command, stop_command
from .errors import TransportError
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
    return (device.get("connectivityStatus") or {}).get("connected")


def is_connected(device: dict[str, Any]) -> bool:
    """Whether AWS explicitly reports the grill connected."""
    return _connected(device) is True


def _reported_command(device: Any) -> Any:
    shadow = device.get("shadow") if isinstance(device, dict) else None
    shadow = shadow.get("properties", shadow) if isinstance(shadow, dict) else None
    reported = shadow.get("reported") if isinstance(shadow, dict) else None
    return reported.get("Cook_Command") if isinstance(reported, dict) else None


def state_from_device(dsn: str, device: dict[str, Any]) -> CombinedState:
    """Build the shared state model from one AWS device record."""
    telemetry = device.get("telemetry") or {}
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
    )


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

    async def _acknowledged(self, dsn: str, before: Any, telemetry: Any) -> bool:
        """Whether the grill itself reacted: a new reported command or new telemetry."""
        for _ in range(_ACK_TIMEOUT_S // _ACK_POLL_S):
            await _sleep(_ACK_POLL_S)
            try:
                if _reported_command(await self._device_api(dsn, "GET")) != before:
                    return True
                device = await self.find_device(dsn)
            except TransportError as err:
                _LOGGER.debug("reading back the cook command failed: %s", err)
                continue
            if device is not None and device.get("telemetry") != telemetry:
                return True
        return False

    async def _withdraw(self, dsn: str) -> None:
        await self._send_cook_command(dsn, stop_command())

    async def _start(self, dsn: str, command: dict[str, Any]) -> Any:
        """Send a start only to a connected grill and withdraw it unless acknowledged."""
        device = await self.find_device(dsn)
        if device is None:
            raise TransportError(f"device {dsn} not found on the AWS backend")
        if not is_connected(device):
            raise TransportError("grill is not connected to the AWS cloud")
        before = _reported_command(await self._device_api(dsn, "GET"))
        result = await self._send_cook_command(dsn, command)
        try:
            acknowledged = await self._acknowledged(dsn, before, device.get("telemetry"))
        except asyncio.CancelledError:
            await asyncio.shield(self._withdraw(dsn))
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
