import asyncio
import time

import pytest

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ninja_woodfire._lib.api.auth0 import Auth0Session
from custom_components.ninja_woodfire._lib.api.ayla import AylaCloudClient
from custom_components.ninja_woodfire._lib.api.errors import AuthError, TransportError
from custom_components.ninja_woodfire._lib.const import make_region

from .conftest import jwt

AUTH0 = "https://logineu.sharkninja.com/oauth/token"
SIGN_IN = "https://user-field-eu.aylanetworks.com/api/v1/token_sign_in"


def _grants(aioclient_mock):
    return sum(1 for _, url, _, _ in aioclient_mock.mock_calls if str(url) == AUTH0)


def _session():
    return Auth0Session("a@b.c", "pw", make_region("EU"))


async def test_user_id_and_expiry_come_from_the_jwt(hass, aioclient_mock):
    exp = int(time.time()) + 3600
    token = jwt("auth0|abc123", exp)
    aioclient_mock.post(AUTH0, json={"id_token": token})
    auth = _session()
    assert await auth.id_token(async_get_clientsession(hass)) == token
    assert auth.user_id == "abc123"
    assert auth.expires_at == exp - 60


async def test_token_is_cached_until_shortly_before_expiry(hass, aioclient_mock, freezer):
    aioclient_mock.post(AUTH0, json={"id_token": jwt(exp=int(time.time()) + 600)})
    auth, http = _session(), async_get_clientsession(hass)
    await auth.id_token(http)
    freezer.tick(530)
    await auth.id_token(http)
    assert _grants(aioclient_mock) == 1
    freezer.tick(20)
    await auth.id_token(http)
    assert _grants(aioclient_mock) == 2


async def test_concurrent_callers_share_one_grant(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": jwt()})
    auth, http = _session(), async_get_clientsession(hass)
    await asyncio.gather(auth.id_token(http), auth.id_token(http))
    assert _grants(aioclient_mock) == 1


async def test_refresh_replaces_only_the_stale_token(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": jwt()})
    auth, http = _session(), async_get_clientsession(hass)
    token = await auth.id_token(http)
    await auth.refresh(http, "someone-elses-old-token")
    assert _grants(aioclient_mock) == 1
    await auth.refresh(http, token)
    assert _grants(aioclient_mock) == 2


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_are_auth_errors(hass, aioclient_mock, status):
    aioclient_mock.post(AUTH0, status=status, text="invalid_grant")
    with pytest.raises(AuthError):
        await _session().id_token(async_get_clientsession(hass))


@pytest.mark.parametrize(("status", "body"), [(500, None), (429, None), (200, {"access_token": "x"})])
async def test_other_failures_are_transport_errors(hass, aioclient_mock, status, body):
    aioclient_mock.post(AUTH0, status=status, json=body)
    with pytest.raises(TransportError):
        await _session().id_token(async_get_clientsession(hass))


async def test_ayla_signs_in_with_the_shared_token(hass, aioclient_mock):
    token = jwt()
    aioclient_mock.post(AUTH0, json={"id_token": token})
    aioclient_mock.post(SIGN_IN, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    auth, http = _session(), async_get_clientsession(hass)
    client = AylaCloudClient("a@b.c", "pw", session=http, auth=auth)
    await client.login()
    assert await auth.id_token(http) == token
    assert _grants(aioclient_mock) == 1
    sign_in = next(data for _, url, data, _ in aioclient_mock.mock_calls if str(url) == SIGN_IN)
    assert sign_in["token"] == token
