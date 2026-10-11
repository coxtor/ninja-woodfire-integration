import asyncio
import time

import aiohttp
import pytest

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ninja_woodfire._lib.api.ayla import (
    AylaCloudClient,
    AylaSession,
    TransportError,
)

AUTH0 = "https://logineu.sharkninja.com/oauth/token"
SIGN_IN = "https://user-field-eu.aylanetworks.com/api/v1/token_sign_in"
REFRESH = "https://user-field-eu.aylanetworks.com/users/refresh_token.json"
PROPS = "https://ads-eu.aylanetworks.com/apiv1/dsns/X/properties.json"


def _login_mocks(aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": "idt"})
    aioclient_mock.post(SIGN_IN, json={"access_token": "new", "refresh_token": "rt", "expires_in": 3600})


async def test_expired_token_with_failing_refresh_relogs_in(hass, aioclient_mock):
    _login_mocks(aioclient_mock)
    aioclient_mock.post(REFRESH, status=401)
    aioclient_mock.get(PROPS, json=[])
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    client._ayla = AylaSession("old", "rt", time.time() - 1)

    await asyncio.wait_for(client.get_combined_state("X"), timeout=3)

    assert client._ayla.access_token == "new"


async def test_401_on_read_reauthenticates_and_retries(hass, aioclient_mock):
    _login_mocks(aioclient_mock)
    calls = []

    async def props(method, url, data):
        calls.append(1)
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        status = 401 if len(calls) == 1 else 200
        return AiohttpClientMockResponse(method, url, status=status, json=[])

    aioclient_mock.get(PROPS, side_effect=props)
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    client._ayla = AylaSession("revoked", "rt", time.time() + 3600)

    await client.get_combined_state("X")

    assert len(calls) == 2
    assert client._ayla.access_token == "new"


async def test_network_error_is_transport_error(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, exc=aiohttp.ClientConnectionError("dns"))
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    with pytest.raises(TransportError):
        await client.login()


async def test_timeout_is_transport_error(hass, aioclient_mock):
    _login_mocks(aioclient_mock)
    aioclient_mock.get(PROPS, exc=TimeoutError())
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    with pytest.raises(TransportError):
        await client.get_combined_state("X")


async def test_rate_limited_sign_in_is_not_an_auth_error(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": "idt"})
    aioclient_mock.post(SIGN_IN, status=429, text="slow down")
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    with pytest.raises(TransportError):
        await client.login()
