"""SharkNinja Auth0 login, shared by the Ayla and AWS clients.

Local patch (NA fix):
- Sends a mobile-app User-Agent. SharkNinja's US Auth0 tenant flags the
  default aiohttp User-Agent as a bot ("Suspicious request requires
  verification"), which the config flow shows as "invalid email or password".
- If the direct password grant is still refused for bot/verification
  reasons, falls back to Auth0's cross-origin /co/authenticate + PKCE flow,
  the same fix the Shark IQ library uses for the same login server.
- Logs Auth0's actual error so a failed login is diagnosable.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from typing import Any

import aiohttp

from ..const import CloudRegion
from .errors import AuthError, TransportError
from .http import send

_LOGGER = logging.getLogger(__name__)

# Assumed lifetime when neither the token nor the response states one.
_DEFAULT_LIFETIME_S = 3600

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
)
_SCOPE = "openid profile email read:current_user offline_access"
# Captured from the real Ninja iOS app (v1.28.0) sign-in, Oct 2026.
_NA_REDIRECT_URI = "com.sharkninja.ninja://login.sharkninja.com/ios/com.sharkninja.ninja/callback"
_NA_SCOPE = ("openid profile email offline_access read:users "
             "read:current_user read:user_idp_tokens")
_NA_APP_VERSION = "1.28.0"
_IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/27.0.1 Mobile/15E148 Safari/604.1")
_REALM = "Username-Password-Authentication"

# Auth0 errors that mean "blocked as a bot / flow not allowed", not
# "wrong password" -- only these trigger the PKCE fallback, so a genuinely
# wrong password costs one login attempt, not two.
_FALLBACK_MARKERS = (
    "requires_verification",
    "Suspicious request",
    "unauthorized_client",
    "Grant type",
    "access_denied",
)


def _jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's claims without verifying it (the API does that)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


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
        if self._region.name == "NA":
            self._store(await self._pkce_login(http))
            return
        base = self._region.auth0_base
        body = {
            "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
            "username": self._email,
            "password": self._password,
            "audience": self._region.auth0_audience,
            "scope": _SCOPE,
            "client_id": self._region.auth0_client_id,
            "realm": _REALM,
        }
        status, data = await send(
            http, "POST", f"{base}/oauth/token", json=body,
            headers={"User-Agent": _MOBILE_UA},
        )
        if status == 200 and isinstance(data, dict):
            self._store(data)
            return

        text = str(data)
        _LOGGER.warning("Auth0 password grant (%s) failed: %s %s",
                        self._region.name, status, text[:500])
        if status in (401, 403) and any(m in text for m in _FALLBACK_MARKERS):
            _LOGGER.warning("Retrying login via Auth0 cross-origin + PKCE flow")
            self._store(await self._pkce_login(http))
            return
        if status in (401, 403):
            raise AuthError(f"Auth0 rejected credentials: {text}")
        raise TransportError(f"Auth0 {status}: {text}")

    async def _pkce_login(self, http: aiohttp.ClientSession) -> dict[str, Any]:
        """Cross-origin login, as used by Shark IQ to avoid bot detection."""
        base = self._region.auth0_base
        client_id = self._region.auth0_client_id
        headers = {
            "User-Agent": _IOS_UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base,
            "Referer": base + "/",
        }
        verifier, challenge = _pkce_pair()
        timeout = aiohttp.ClientTimeout(total=20)

        try:
            redirect_uri = _NA_REDIRECT_URI

            # 1. Credentials -> login_ticket
            async with http.post(
                f"{base}/co/authenticate", headers=headers, timeout=timeout,
                data={
                    "client_id": client_id,
                    "credential_type": "http://auth0.com/oauth/grant-type/password-realm",
                    "username": self._email,
                    "password": self._password,
                    "realm": _REALM,
                },
            ) as r:
                text = await r.text()
                if r.status != 200:
                    _LOGGER.warning("Auth0 /co/authenticate failed: %s %s", r.status, text[:500])
                    raise AuthError(f"/co/authenticate {r.status}: {text}")
                ticket = json.loads(text).get("login_ticket")
            if not ticket:
                raise TransportError("Auth0 /co/authenticate returned no login_ticket")

            # 2. /authorize with ticket + PKCE -> redirect carrying ?code=
            query = urllib.parse.urlencode({
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": _NA_SCOPE,
                "login_ticket": ticket,
                "mobile_ninja_app_version": _NA_APP_VERSION,
                "ui_locales": "en",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            })
            location = await self._location(http, f"{base}/authorize?{query}", headers, timeout)

            if "/u/consent" in location:
                url = location if location.startswith("http") else base + location
                async with http.get(url, headers=headers, timeout=timeout,
                                    allow_redirects=False) as r:
                    m = re.search(r'name="state"\s+value="([^"]+)"', await r.text())
                if not m:
                    raise TransportError("Auth0 consent page had no state")
                async with http.post(url, headers=headers, timeout=timeout,
                                     allow_redirects=False,
                                     data={"state": m.group(1), "action": "accept"}) as r:
                    location = r.headers.get("Location", "")

            if location.startswith("/authorize/resume"):
                location = await self._location(http, base + location, headers, timeout)

            code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
            if not code:
                _LOGGER.warning("Auth0 PKCE login gave no code; last redirect: %s", location[:300])
                raise TransportError(f"Auth0 gave no authorization code ({location[:200]})")

            # 3. code + verifier -> tokens
            async with http.post(
                f"{base}/oauth/token", timeout=timeout,
                headers={"User-Agent": _IOS_UA},
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            ) as r:
                text = await r.text()
                if r.status != 200:
                    _LOGGER.warning("Auth0 code exchange failed: %s %s", r.status, text[:500])
                    raise TransportError(f"Auth0 code exchange {r.status}")
                return json.loads(text)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise TransportError(f"Auth0 PKCE login: {err!r}") from err

    @staticmethod
    async def _location(http, url, headers, timeout) -> str:
        async with http.get(url, headers=headers, timeout=timeout,
                            allow_redirects=False) as r:
            loc = r.headers.get("Location", "")
            if not loc:
                body = (await r.text())[:300]
                _LOGGER.warning("Auth0 %s returned %s with no redirect: %s",
                                url.split("?")[0], r.status, body)
            return loc

    def _store(self, data: dict[str, Any]) -> None:
        token = data.get("id_token")
        if not token:
            raise TransportError("Auth0 returned no id_token")
        claims = _jwt_claims(token)
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            exp = time.time() + float(data.get("expires_in") or _DEFAULT_LIFETIME_S)
        self._id_token = token
        self.user_id = str(claims.get("sub", "")).removeprefix("auth0|")
        self.expires_at = float(exp) - 60
