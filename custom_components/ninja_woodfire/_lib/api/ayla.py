"""Cloud transport client for the Ninja Woodfire grill."""
from __future__ import annotations

import asyncio
import logging
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from ..const import (
    ALL_READ_PROPERTIES,
    CloudRegion,
    COOK_MODES,
    PROBE_MODES,
    PROP_COOK_STATE,
    PROP_GRILL_STATE,
    PROP_PROBE_STATE,
    PROP_SET_COOK,
    make_region,
)
from ..models import CombinedState, CookState, GrillState, ProbeState

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_DEVICE_CACHE_S = 30


class NinjaCloudError(Exception):
    """Base exception."""


class AuthError(NinjaCloudError):
    """Bad credentials or expired session."""


class TransportError(NinjaCloudError):
    """Network / 5xx / unexpected payload."""


@dataclass
class AylaSession:
    access_token: str
    refresh_token: str
    expires_at: float  # unix epoch seconds


class AylaCloudClient:
    """Async client for the Ayla-cloud-backed Ninja grills."""

    def __init__(
        self,
        email: str,
        password: str,
        region: CloudRegion | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._email = email
        self._password = password
        # Default to EU region using bundled credentials when no
        # explicit region is provided.
        self._region = region if region is not None else make_region("EU")
        self._http = session
        self._owns_session = session is None
        self._auth0_id_token: str | None = None
        self._ayla: AylaSession | None = None
        self._lock = asyncio.Lock()
        self._devices: dict[str, tuple[float, dict[str, Any]]] = {}

    async def __aenter__(self) -> "AylaCloudClient":
        if self._owns_session and self._http is None:
            self._http = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_session and self._http is not None:
            await self._http.close()
            self._http = None

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        """Run the full token-exchange flow."""
        async with self._lock:
            await self._login()

    async def _login(self) -> None:
        await self._auth0_password_grant()
        await self._ayla_token_sign_in()

    async def _auth0_password_grant(self) -> None:
        url = f"{self._region.auth0_base}/oauth/token"
        body = {
            "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
            "username": self._email,
            "password": self._password,
            "audience": self._region.auth0_audience,
            "scope": "openid profile email read:current_user offline_access",
            "client_id": self._region.auth0_client_id,
            "realm": "Username-Password-Authentication",
        }
        status, data = await self._send("POST", url, json=body)
        if status in (401, 403):
            raise AuthError(f"Auth0 rejected credentials: {data}")
        if status != 200 or not isinstance(data, dict):
            raise TransportError(f"Auth0 {status}: {data}")
        self._auth0_id_token = data.get("id_token")
        if not self._auth0_id_token:
            raise TransportError("Auth0 returned no id_token")

    async def _ayla_token_sign_in(self) -> None:
        url = f"{self._region.ayla_user_base}/api/v1/token_sign_in"
        body = {
            "token": self._auth0_id_token,
            "app_id": self._region.ayla_app_id,
            "app_secret": self._region.ayla_app_secret,
        }
        status, data = await self._send("POST", url, json=body)
        if status in (401, 403):
            raise AuthError(f"Ayla token_sign_in {status}: {data}")
        if status != 200 or not isinstance(data, dict):
            raise TransportError(f"Ayla token_sign_in {status}: {data}")
        self._store_session(data)

    async def _refresh(self) -> None:
        url = f"{self._region.ayla_user_base}/users/refresh_token.json"
        body = {"user": {"refresh_token": self._ayla.refresh_token}}
        status, data = await self._send("POST", url, json=body)
        if status != 200 or not isinstance(data, dict):
            _LOGGER.info("token refresh failed (%s) — re-authenticating", status)
            await self._login()
            return
        self._store_session(data)

    def _store_session(self, data: dict[str, Any]) -> None:
        self._ayla = AylaSession(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token")
            or (self._ayla.refresh_token if self._ayla else ""),
            expires_at=time.time() + float(data.get("expires_in", 0)) - 60,
        )

    async def _ensure_session(self) -> str:
        async with self._lock:
            if self._ayla is None:
                await self._login()
            elif self._ayla.expires_at <= time.time():
                await self._refresh()
            assert self._ayla is not None
            return self._ayla.access_token

    # ------------------------------------------------------------------ http

    async def _send(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        try:
            async with self._client().request(
                method, url, timeout=_TIMEOUT, **kwargs
            ) as r:
                if r.status in (200, 201):
                    return r.status, await r.json(content_type=None)
                return r.status, await r.text()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise TransportError(f"{method} {url}: {err!r}") from err

    async def _api(self, method: str, url: str, **kwargs: Any) -> Any:
        """Authenticated request; re-authenticates once on 401."""
        for attempt in range(2):
            token = await self._ensure_session()
            status, data = await self._send(
                method, url, headers=self._auth_headers(token), **kwargs
            )
            if status in (200, 201):
                return data
            if status == 401 and attempt == 0:
                async with self._lock:
                    if self._ayla is not None and self._ayla.access_token == token:
                        self._ayla = None
                continue
            raise TransportError(f"{method} {url} {status}: {data}")

    # ------------------------------------------------------------------ data

    async def get_devices(self) -> list[dict[str, Any]]:
        url = f"{self._region.ayla_device_base}/apiv1/devices.json"
        return self._unwrap_list(await self._api("GET", url), "device")

    async def get_device(self, dsn: str) -> dict[str, Any]:
        url = f"{self._region.ayla_device_base}/apiv1/dsns/{dsn}.json"
        data = await self._api("GET", url)
        device = data.get("device") if isinstance(data, dict) else None
        if not isinstance(device, dict):
            raise TransportError(f"GET {url}: no device record")
        return device

    async def _cached_device(self, dsn: str) -> dict[str, Any] | None:
        cached = self._devices.get(dsn)
        if cached and time.monotonic() - cached[0] < _DEVICE_CACHE_S:
            return cached[1]
        try:
            device = await self.get_device(dsn)
        except TransportError:
            return cached[1] if cached else None
        self._devices[dsn] = (time.monotonic(), device)
        return device

    async def get_properties(
        self, dsn: str, names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        url = f"{self._region.ayla_device_base}/apiv1/dsns/{dsn}/properties.json"
        params: list[tuple[str, str]] | None = (
            [("names[]", n) for n in names] if names else None
        )
        return self._unwrap_list(await self._api("GET", url, params=params), "property")

    async def get_combined_state(self, dsn: str) -> CombinedState:
        """Read GET_GrillState + GET_CookState + GET_ProbeState in one round-trip."""
        props = await self.get_properties(dsn, names=ALL_READ_PROPERTIES)
        state = CombinedState(dsn=dsn)
        latest_update: datetime | None = None
        for p in props:
            name = p.get("name")
            value = p.get("value")
            updated_at_raw = p.get("data_updated_at")
            if updated_at_raw:
                try:
                    ts = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
                    if latest_update is None or ts > latest_update:
                        latest_update = ts
                except ValueError:
                    pass
            if name == PROP_GRILL_STATE:
                state.grill = GrillState.from_property_value(value)
            elif name == PROP_COOK_STATE:
                state.cook = CookState.from_property_value(value)
            elif name == PROP_PROBE_STATE:
                state.probes = ProbeState.from_property_value(value)
        state.last_updated_at = latest_update
        if (device := await self._cached_device(dsn)) is not None:
            state.connection_status = device.get("connection_status")
            state.online = str(state.connection_status).casefold() != "offline"
        return state

    async def set_property_datapoint(
        self, dsn: str, name: str, value: Any
    ) -> dict[str, Any]:
        """Write a datapoint to a settable property (e.g. SET_Cook_Command)."""
        url = (
            f"{self._region.ayla_device_base}/apiv1/dsns/{dsn}"
            f"/properties/{name}/datapoints.json"
        )
        return await self._api("POST", url, json={"datapoint": {"value": value}})

    # ---------------------------------------------------------- cook commands

    async def get_device_key(self, dsn: str) -> int:
        """Look up the device key required in the cook payload."""
        for d in await self.get_devices():
            if d.get("dsn") == dsn:
                key = d.get("key")
                if key is None:
                    raise NinjaCloudError(f"device {dsn} has no device key")
                return int(key)
        raise NinjaCloudError(f"device {dsn} not found in account")

    # ------------------------------------------------------------------ cook

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
    ) -> dict[str, Any]:
        """Start (or update) a cook.

        Args:
            mode: one of COOK_MODES (e.g. "grill", "smoker", "air crisp")
            seconds: cook duration in seconds
            temp: target. Heat-level 1/2/3 for "grill" mode, °C otherwise.
            smoke: True to enable smoke addition.
            skip_preheat: True to skip the preheat phase.
            probe_0: optional probe-1 target dict, e.g. {"mode":"manual","setpoint":79}
            probe_1: optional probe-2 target dict.
            device_key: optional override; fetched via get_devices() if None.
        """
        if mode not in COOK_MODES:
            raise ValueError(f"unknown cook mode {mode!r}; expected one of {COOK_MODES}")
        for label, p in (("probe_0", probe_0), ("probe_1", probe_1)):
            if p is None:
                continue
            if p.get("mode") not in PROBE_MODES:
                raise ValueError(
                    f"{label}.mode {p.get('mode')!r} invalid; expected one of {PROBE_MODES}"
                )
        if device_key is None:
            device_key = await self.get_device_key(dsn)

        payload: dict[str, Any] = {
            "id": device_key,
            "mode": mode,
            "seconds set": int(seconds),
            "temp": int(temp),
            "smoke": 1 if smoke else 0,
            "skip preheat": 1 if skip_preheat else 0,
        }
        if probe_0 is not None:
            payload["probe0"] = probe_0
        if probe_1 is not None:
            payload["probe1"] = probe_1

        value = json.dumps(payload, separators=(",", ":"))
        return await self.set_property_datapoint(dsn, PROP_SET_COOK, value)

    async def stop_cook(self, dsn: str) -> dict[str, Any]:
        """Stop the currently running cook."""
        stop_payload = {
            "id": 1000,
            "mode": "grill",
            "temp": 0,
            "seconds set": 0,
            "smoke": 0,
            "skip preheat": 0,
        }
        value = json.dumps(stop_payload, separators=(",", ":"))
        return await self.set_property_datapoint(dsn, PROP_SET_COOK, value)

    async def set_grill_name(self, dsn: str, new_name: str) -> dict[str, Any]:
        """Rename the grill in the user's account."""
        url = f"{self._region.ayla_device_base}/apiv1/dsns/{dsn}.json"
        return await self._api("PUT", url, json={"device": {"product_name": new_name}})

    # ------------------------------------------------------------------ helpers

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
            self._owns_session = True
        return self._http

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"auth_token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _unwrap_list(payload: Any, inner_key: str) -> list[dict[str, Any]]:
        """Cloud responses come as [{key: {...}}, ...]; peel one level."""
        if not isinstance(payload, list):
            return []
        out = []
        for item in payload:
            if isinstance(item, dict) and inner_key in item:
                out.append(item[inner_key])
            elif isinstance(item, dict):
                out.append(item)
        return out
