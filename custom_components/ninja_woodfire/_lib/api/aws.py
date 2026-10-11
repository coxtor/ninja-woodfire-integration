"""Cloud transport for SharkNinja's AWS backend.

Grills migrated by the Ninja app report and take commands only here; Ayla
keeps serving their last snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..const import COOK_MODES, CloudRegion
from ..models import CombinedState, CookState, GrillState, ProbeState, _parse_value
from .auth0 import Auth0Session
from .commands import cook_command, stop_command
from .errors import TransportError
from .http import send

_START_ID = 1001

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
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def state_from_device(dsn: str, device: dict[str, Any]) -> CombinedState:
    """Build the shared state model from one AWS device record."""
    telemetry = device.get("telemetry") or {}
    connected = (device.get("connectivityStatus") or {}).get("connected")

    def blob(name: str) -> dict[str, Any]:
        return normalise(_parse_value(telemetry.get(name)))

    return CombinedState(
        dsn=dsn,
        grill=GrillState.from_property_value(blob("GrillState")),
        cook=CookState.from_property_value(blob("CookState")),
        probes=ProbeState.from_property_value(blob("ProbeState")),
        online=bool(connected),
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
            raise TransportError(f"{method} {path} {status}: {data}")

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

    async def find_device(self, dsn: str) -> dict[str, Any] | None:
        """The grill's device record, matched by Ayla DSN or appliance serial."""
        household = await self._household_id()
        if household is None:
            return None
        data = await self._api(
            "GET",
            f"/devicesEndUserController/{household}/users/{await self._user_id()}",
            params={"includeRegistry": "true", "includeConnectivityStatus": "true"},
        )
        items = data.get("items") if isinstance(data, dict) else None
        for device in items if isinstance(items, list) else []:
            if not isinstance(device, dict):
                continue
            registry = device.get("registry") or {}
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

    async def _send_cook_command(self, dsn: str, command: dict[str, Any]) -> Any:
        if dsn not in self._device_ids:
            await self.find_device(dsn)
        if (device_id := self._device_ids.get(dsn)) is None:
            raise TransportError(f"device {dsn} not found on the AWS backend")
        path = f"/devicesEndUserController/{self._household}/devices/{device_id}"
        body = {"shadow": {"properties": {"desired": {"Cook_Command": command}}}}
        return await self._api("PATCH", path, json=body)

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
        return await self._send_cook_command(dsn, {"id": _START_ID, **payload})

    async def stop_cook(self, dsn: str) -> Any:
        return await self._send_cook_command(dsn, stop_command())
