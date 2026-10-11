"""SharkNinja Auth0 login, shared by the Ayla and AWS clients."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import aiohttp

from ..const import CloudRegion
from .errors import AuthError, TransportError
from .http import send


def _jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's claims without verifying it (the API does that)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


class Auth0Session:
    """One cached Auth0 id_token per account."""

    def __init__(self, email: str, password: str, region: CloudRegion) -> None:
        self._email = email
        self._password = password
        self._region = region
        self._lock = asyncio.Lock()
        self._id_token: str | None = None
        self.user_id = ""
        self.expires_at = 0.0

    async def id_token(self, http: aiohttp.ClientSession) -> str:
        """Return the cached id_token, signing in when it is missing or expiring."""
        async with self._lock:
            if self._id_token is None or self.expires_at <= time.time():
                await self._grant(http)
            assert self._id_token is not None
            return self._id_token

    async def refresh(self, http: aiohttp.ClientSession, stale: str) -> str:
        """Replace a token the API rejected, unless another caller already did."""
        async with self._lock:
            if self._id_token in (None, stale):
                await self._grant(http)
            assert self._id_token is not None
            return self._id_token

    async def _grant(self, http: aiohttp.ClientSession) -> None:
        body = {
            "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
            "username": self._email,
            "password": self._password,
            "audience": self._region.auth0_audience,
            "scope": "openid profile email read:current_user offline_access",
            "client_id": self._region.auth0_client_id,
            "realm": "Username-Password-Authentication",
        }
        url = f"{self._region.auth0_base}/oauth/token"
        status, data = await send(http, "POST", url, json=body)
        if status in (401, 403):
            raise AuthError(f"Auth0 rejected credentials: {data}")
        if status != 200 or not isinstance(data, dict):
            raise TransportError(f"Auth0 {status}: {data}")
        token = data.get("id_token")
        if not token:
            raise TransportError("Auth0 returned no id_token")
        claims = _jwt_claims(token)
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            exp = time.time() + float(data.get("expires_in") or 0)
        self._id_token = token
        self.user_id = str(claims.get("sub", "")).removeprefix("auth0|")
        self.expires_at = float(exp) - 60
