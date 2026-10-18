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


def _sign_in_replies(aioclient_mock, statuses):
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    statuses, tokens = list(statuses), []

    async def reply(method, url, data):
        tokens.append(data["token"])
        status = statuses.pop(0)
        body = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    aioclient_mock.post(SIGN_IN, side_effect=reply)
    return tokens


def _distinct_grants(aioclient_mock):
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    count = iter(range(100))

    async def reply(method, url, data):
        return AiohttpClientMockResponse(method, url, json={"id_token": jwt(f"auth0|u{next(count)}")})

    aioclient_mock.post(AUTH0, side_effect=reply)


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_shared_token_is_refreshed_once(hass, aioclient_mock, status):
    _distinct_grants(aioclient_mock)
    tokens = _sign_in_replies(aioclient_mock, [status, 200])
    auth, http = _session(), async_get_clientsession(hass)
    stale = await auth.id_token(http)
    await AylaCloudClient("a@b.c", "pw", session=http, auth=auth).login()
    assert _grants(aioclient_mock) == 2
    assert tokens[0] == stale
    assert tokens[1] == await auth.id_token(http) != stale


async def test_rejected_refreshed_token_is_an_auth_error(hass, aioclient_mock):
    _distinct_grants(aioclient_mock)
    tokens = _sign_in_replies(aioclient_mock, [401, 403])
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass), auth=_session())
    with pytest.raises(AuthError):
        await client.login()
    assert len(tokens) == 2
    assert _grants(aioclient_mock) == 2


async def test_token_without_expiry_lasts_an_hour(hass, aioclient_mock, freezer):
    token = jwt().rsplit(".", 2)
    token[1] = "eyJzdWIiOiAiYXV0aDB8dXNlcjEifQ"  # {"sub": "auth0|user1"}
    aioclient_mock.post(AUTH0, json={"id_token": ".".join(token)})
    auth, http = _session(), async_get_clientsession(hass)
    await auth.id_token(http)
    freezer.tick(3000)
    await auth.id_token(http)
    assert _grants(aioclient_mock) == 1
    freezer.tick(600)
    await auth.id_token(http)
    assert _grants(aioclient_mock) == 2


# ---------------------------------------------------------------- NA region
# login.sharkninja.com (NA) flags the plain password grant as a bot
# ("requires_verification") for some accounts; the fallback is Auth0's
# cross-origin /co/authenticate + PKCE flow (issue #2). All requests are
# mocked — nothing here touches the real Auth0 tenant.

NA_AUTH0 = "https://login.sharkninja.com/oauth/token"
NA_COAUTH = "https://login.sharkninja.com/co/authenticate"
NA_AUTHORIZE_PREFIX = "https://login.sharkninja.com/authorize"


def _na_session():
    return Auth0Session("a@b.c", "pw", make_region("NA"))


async def test_na_login_goes_straight_through_pkce(hass, aioclient_mock):
    # NA never tries the plain password grant first (that's what trips the
    # tenant's bot detection) — it goes straight through co/authenticate ->
    # /authorize (-> redirect carrying ?code=) -> code exchange.
    aioclient_mock.post(NA_COAUTH, json={"login_ticket": "tkt-123"})
    aioclient_mock.get(
        NA_AUTHORIZE_PREFIX, status=302,
        headers={"Location": "com.sharkninja.ninja://login.sharkninja.com/ios/com.sharkninja.ninja/callback?code=abc123"},
    )
    aioclient_mock.post(NA_AUTH0, json={"id_token": jwt()})

    token = await _na_session().id_token(async_get_clientsession(hass))
    assert token == jwt()
    assert sum(1 for _, url, _, _ in aioclient_mock.mock_calls if str(url) == NA_COAUTH) == 1
    code_exchange = next(data for _, url, data, _ in aioclient_mock.mock_calls if str(url) == NA_AUTH0)
    assert code_exchange["grant_type"] == "authorization_code"
    assert code_exchange["code"] == "abc123"


async def test_na_wrong_password_is_an_auth_error(hass, aioclient_mock):
    # co/authenticate itself rejects bad credentials; the flow must stop there
    # (no call to /authorize or the token endpoint).
    aioclient_mock.post(NA_COAUTH, status=401, text="Wrong email or password.")
    with pytest.raises(AuthError):
        await _na_session().id_token(async_get_clientsession(hass))
    assert sum(1 for _, url, _, _ in aioclient_mock.mock_calls if str(url) == NA_AUTHORIZE_PREFIX) == 0
