import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ninja_woodfire._lib.api import aws as aws_module
from custom_components.ninja_woodfire._lib.api.auth0 import Auth0Session
from custom_components.ninja_woodfire._lib.api.aws import (
    AwsCloudClient,
    normalise,
    state_from_device,
)
from custom_components.ninja_woodfire._lib.api.ayla import AylaCloudClient
from custom_components.ninja_woodfire._lib.api.errors import AuthError, TransportError
from custom_components.ninja_woodfire._lib.const import make_region

from .conftest import DSN, aws_device, jwt

AUTH0 = "https://logineu.sharkninja.com/oauth/token"
SIGN_IN = "https://user-field-eu.aylanetworks.com/api/v1/token_sign_in"
AWS = "https://stakra.rannsaka.thor.skegox.com"
HOUSEHOLDS = f"{AWS}/householdsEndUser"
DEVICES = f"{AWS}/devicesEndUserController/hh1/users/user1"
DEVICE = f"{AWS}/devicesEndUserController/hh1/devices/SND1"


def _state(name, **kw):
    return state_from_device(DSN, aws_device(name, **kw))


def _calls(aioclient_mock, url, method=None):
    return [
        c for c in aioclient_mock.mock_calls
        if str(c[1]).split("?")[0] == url and (method is None or c[0] == method)
    ]


@pytest.fixture(autouse=True)
def sleep(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(aws_module, "_sleep", sleep)
    return sleep


def _shadow(cook_command=None, updated_at="2026-08-19T18:39:58.666Z"):
    """The single-device record, as read back after a command."""
    return {
        "deviceId": "SND1",
        "updatedAt": updated_at,
        "shadow": {"properties": {"desired": {}, "reported": {"Cook_Command": cook_command}}},
    }


def _replies(aioclient_mock, url, method, replies):
    """Serve `replies` in order (a dict is JSON, an int a bare status); the last repeats."""
    replies = list(replies)

    async def reply(m, u, data):
        r = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(r, int):
            return AiohttpClientMockResponse(m, u, status=r, text="nope")
        return AiohttpClientMockResponse(m, u, json=r)

    getattr(aioclient_mock, method)(url, side_effect=reply)


def _patches(aioclient_mock):
    return [
        c[2]["shadow"]["properties"]["desired"]["Cook_Command"]
        for c in _calls(aioclient_mock, DEVICE, "PATCH")
    ]


@pytest.fixture
def backend(aioclient_mock):
    """A grill that reports every command it is sent back in its shadow."""
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": ["hh1"]})
    aioclient_mock.get(DEVICES, json={"items": [aws_device()]})
    desired: list[dict] = []

    async def patched(method, url, data):
        desired.append(data["shadow"]["properties"]["desired"]["Cook_Command"])
        return AiohttpClientMockResponse(method, url, json={})

    async def shadow(method, url, data):
        applied = desired[-1] if desired else {"id": 1000}
        return AiohttpClientMockResponse(method, url, json=_shadow(applied))

    aioclient_mock.get(DEVICE, side_effect=shadow)
    aioclient_mock.patch(DEVICE, side_effect=patched)
    return aioclient_mock


def _client(hass, region=None):
    region = region or make_region("EU")
    return AwsCloudClient(Auth0Session("a@b.c", "pw", region), region, async_get_clientsession(hass))


async def _sent(hass, backend, call):
    await call(_client(hass))
    return _patches(backend)[0]


# ---------------------------------------------------------------- normalisation


def test_keys_are_rewritten_to_ayla_spelling():
    out = normalise({
        "secondsset": 180,
        "secondsleft": 42,
        "probesactive": 1,
        "inputs": {"io": {"lidopen": 1}},
        "probes": [{"pluggedin": 1, "mode": {"presetindex": 3}}],
    })
    assert out["seconds set"] == 180
    assert out["seconds left"] == 42
    assert out["probes active"] == 1
    assert out["inputs"]["io"]["lid open"] == 1
    assert out["probes"][0]["plugged in"] == 1
    assert out["probes"][0]["mode"]["preset_index"] == 3


@pytest.mark.parametrize(("key", "raw", "canonical"), [
    ("mode", "aircrisp", "air crisp"),
    ("mode", "air crisp", "air crisp"),
    ("mode", "sousvide", "sousvide"),
    ("state", "poweredOFF", "powered OFF"),
    ("state", "getfood", "get food"),
    ("state", "lidopen", "lid open"),
    ("state", "zcloss", "zc loss"),
    ("state", "idle", "idle"),
])
def test_mode_and_state_values_are_canonicalised(key, raw, canonical):
    assert normalise({key: raw})[key] == canonical


def test_only_mode_and_state_values_are_touched():
    assert normalise({"message": "7:getfood", "eventmask": "0xC0"}) == {
        "message": "7:getfood", "eventmask": "0xC0",
    }


# ---------------------------------------------------------------- state mapping


def test_preheat():
    state = _state("aws_preheat")
    assert state.grill.state == "cooking"
    assert state.cook.state == "preheat"
    assert 0 <= state.cook.progress <= 100
    assert state.grill.mode == "air crisp"
    assert state.grill.setpoint == 150
    assert state.grill.seconds_set == 180
    assert state.grill.end_time_utc is not None


def test_active_cook():
    state = _state("aws_cooking")
    assert state.cook.state == "heat"
    assert state.grill.seconds_left == 180
    assert state.grill.probes_active == 0
    assert state.grill.temps.air > 0
    assert [p.name for p in state.probes.probes] == ["probe0", "probe1"]


def test_air_temperature_is_shown_in_air_crisp():
    state = _state("aws_cooking")
    state.last_updated_at = None
    assert state.temp_is_plausible(state.grill.temps.air, "air") is True


def test_cook_done():
    state = _state("aws_cook_done")
    assert state.grill.message == "6:done"
    assert state.grill.event_mask == "0x40"
    assert state.grill.seconds_left == 0


def test_remove_food_prompt():
    state = _state("aws_get_food")
    assert state.grill.message == "7:getfood"
    assert state.grill.event_mask == "0xC0"


def test_plugged_in_probe():
    probe = _state("aws_bake_probe_ambient").probes.probes[0]
    assert probe.plugged_in is True
    assert probe.temp == 26.6


def test_powered_off_with_lid_open():
    state = _state("aws_powered_off_lid_open")
    assert state.grill.state == "powered OFF"
    assert state.grill.lid_open is True
    assert state.grill.mode is None
    state.last_updated_at = None
    assert state.grill.temps.grill > 50
    assert state.temp_is_plausible(state.grill.temps.grill, "grill") is False


@pytest.mark.parametrize(("connected", "online", "status"), [
    (True, True, "Online"), (False, False, "Offline"), (None, True, None),
])
def test_connectivity(connected, online, status):
    state = state_from_device(DSN, {"connectivityStatus": {"connected": connected}})
    assert state.online is online
    assert state.connection_status == status


def test_missing_telemetry():
    state = state_from_device(DSN, {})
    assert state.grill.state == "unknown"
    assert state.online is True
    assert state.connection_status is None
    assert state.last_updated_at is None


def test_updated_at_is_the_last_report():
    state = _state("aws_cooking")
    assert state.last_updated_at.isoformat() == "2026-08-19T18:39:58.666000+00:00"


# ---------------------------------------------------------------- client


@pytest.mark.parametrize("identifier", [DSN, "SND1"])
async def test_find_device_by_dsn_or_serial(hass, backend, identifier):
    device = await _client(hass).find_device(identifier)
    assert device["deviceId"] == "SND1"


async def test_find_device_requests(hass, backend):
    assert await _client(hass).find_device("AC000OTHER") is None
    (_, url, _, headers), = _calls(backend, DEVICES)
    assert url.query == {"includeRegistry": "true", "includeConnectivityStatus": "true"}
    assert _calls(backend, HOUSEHOLDS)[0][1].query == {"userId": "user1"}
    assert headers["authorization"].startswith("Bearer ")
    assert headers["x-api-key"] == make_region("EU").aws_api_key
    assert headers["x-iotn-caller"] == "ENDUSER_MOBILEAPP"
    assert headers["x-sn-nonce"]
    assert headers["x-sn-date"].endswith("Z") and len(headers["x-sn-date"]) == 24


async def test_account_without_household_has_no_devices(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": []})
    assert await _client(hass).find_device(DSN) is None


async def test_get_combined_state(hass, backend):
    state = await _client(hass).get_combined_state(DSN)
    assert state.dsn == DSN
    assert state.grill.mode == "air crisp"
    assert state.online is True


async def test_get_combined_state_of_unknown_grill_fails(hass, backend):
    with pytest.raises(TransportError):
        await _client(hass).get_combined_state("AC000OTHER")


async def test_401_renews_the_token_once(hass, backend):
    backend.clear_requests()
    backend.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    backend.get(HOUSEHOLDS, json={"households": ["hh1"]})
    responses = iter([401, 200])

    async def devices(method, url, data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        return AiohttpClientMockResponse(method, url, status=next(responses), json={"items": []})

    backend.get(DEVICES, side_effect=devices)
    assert await _client(hass).find_device(DSN) is None
    assert len(_calls(backend, AUTH0)) == 2
    assert len(_calls(backend, DEVICES)) == 2


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_api_rejection_is_a_transport_error(hass, aioclient_mock, status):
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, status=status, text="Forbidden")
    with pytest.raises(TransportError):
        await _client(hass).find_device(DSN)
    assert len(_calls(aioclient_mock, AUTH0)) == (2 if status == 401 else 1)


async def test_one_grant_serves_both_clients(hass, backend):
    backend.post(SIGN_IN, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    region, http = make_region("EU"), async_get_clientsession(hass)
    auth = Auth0Session("a@b.c", "pw", region)
    await AylaCloudClient("a@b.c", "pw", region, http, auth=auth).login()
    await AwsCloudClient(auth, region, http).find_device(DSN)
    assert len(_calls(backend, AUTH0)) == 1


async def test_endpoints_follow_the_region(hass, aioclient_mock):
    region = make_region("EU", aws_rest_base="https://aws.invalid", aws_api_key="k")
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get("https://aws.invalid/householdsEndUser", json={"households": []})
    await _client(hass, region).find_device(DSN)
    assert aioclient_mock.mock_calls[-1][3]["x-api-key"] == "k"


def test_region_defaults():
    for name in ("EU", "NA"):
        region = make_region(name)
        assert region.aws_rest_base.startswith("https://")
        assert region.aws_api_key


# ---------------------------------------------------------------- commands


async def test_start_cook_uses_the_spaced_firmware_spelling(hass, backend):
    sent = await _sent(hass, backend, lambda c: c.start_cook(
        DSN, mode="air crisp", seconds=300, temp=200, smoke=True,
        probe_0={"mode": "manual", "setpoint": 74}, device_key=42,
    ))
    assert sent == {
        "id": 1001, "mode": "air crisp", "seconds set": 300, "temp": 200,
        "smoke": 1, "skip preheat": 0, "probe0": {"mode": "manual", "setpoint": 74},
    }


async def test_stop_cook_payload(hass, backend):
    sent = await _sent(hass, backend, lambda c: c.stop_cook(DSN))
    assert sent == {
        "id": 1000, "mode": "grill", "temp": 0, "seconds set": 0, "smoke": 0, "skip preheat": 0,
    }


async def test_commands_reuse_the_resolved_device(hass, backend):
    client = _client(hass)
    await client.find_device(DSN)
    await client.stop_cook(DSN)
    await client.stop_cook(DSN)
    assert len(_calls(backend, DEVICES)) == 1
    assert len(_calls(backend, HOUSEHOLDS)) == 1
    assert len(_calls(backend, DEVICE, "PATCH")) == 2


async def test_command_for_unknown_grill_fails(hass, backend):
    with pytest.raises(TransportError):
        await _client(hass).stop_cook("AC000OTHER")
    assert _calls(backend, DEVICE, "PATCH") == []


@pytest.mark.parametrize("kwargs", [
    {"mode": "teleport"},
    {"mode": "bake", "probe_0": {"mode": "guess", "setpoint": 60}},
])
async def test_invalid_command_is_rejected_before_sending(hass, backend, kwargs):
    with pytest.raises(ValueError):
        await _client(hass).start_cook(DSN, seconds=60, temp=100, **kwargs)
    assert _calls(backend, DEVICE, "PATCH") == []


async def test_ayla_payload_is_unchanged(hass, aioclient_mock):
    url = "https://ads-eu.aylanetworks.com/apiv1/dsns/X/properties/SET_Cook_Command/datapoints.json"
    aioclient_mock.post(url, json={})
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    client._ensure_session = lambda: _token()
    await client.start_cook("X", mode="bake", seconds=60, temp=180, skip_preheat=True, device_key=42)
    value = json.loads(aioclient_mock.mock_calls[-1][2]["datapoint"]["value"])
    assert value == {"id": 42, "mode": "bake", "seconds set": 60, "temp": 180, "smoke": 0, "skip preheat": 1}


async def _token():
    return "tok"


# ---------------------------------------------------------------- start acknowledgement


START = {"mode": "bake", "seconds": 600, "temp": 180}


def _listing(aioclient_mock, connectivity):
    device = aws_device()
    device["connectivityStatus"] = connectivity
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": ["hh1"]})
    aioclient_mock.get(DEVICES, json={"items": [device]})


async def test_start_acknowledged_by_reported_command(hass, backend, sleep):
    await _client(hass).start_cook(DSN, **START)
    assert [p and p["id"] for p in _patches(backend)] == [1001, None]
    assert len(_calls(backend, DEVICE, "GET")) == 2
    sleep.assert_awaited_with(aws_module._ACK_POLL_S)


async def test_start_acknowledged_by_new_telemetry(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": ["hh1"]})
    before, applied = aws_device(), _cooking_device()
    _replies(aioclient_mock, DEVICES, "get",
             [{"items": [before]}, {"items": [before]}, {"items": [applied]}])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    await _client(hass).start_cook(DSN, **START)
    assert [p and p["id"] for p in _patches(aioclient_mock)] == [1001, None]


async def test_server_timestamp_alone_does_not_acknowledge(hass, aioclient_mock):
    _listing(aioclient_mock, {"connected": True})
    later = (datetime.now(tz=timezone.utc) + timedelta(minutes=1)).isoformat()
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000}), _shadow({"id": 1000}, later)])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="withdrawn"):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_cancelled_wait_withdraws_the_start(hass, aioclient_mock, sleep):
    _listing(aioclient_mock, {"connected": True})
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    sleep.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_unacknowledged_start_is_withdrawn(hass, aioclient_mock, sleep):
    _listing(aioclient_mock, {"connected": True})
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge the command; it was withdrawn"):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]
    polls = aws_module._ACK_TIMEOUT_S // aws_module._ACK_POLL_S
    assert len(_calls(aioclient_mock, DEVICE, "GET")) == 1 + polls
    assert sleep.await_count == polls


async def test_failed_withdrawal_is_reported(hass, aioclient_mock):
    _listing(aioclient_mock, {"connected": True})
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    _replies(aioclient_mock, DEVICE, "patch", [{}, 500])
    with pytest.raises(TransportError, match="withdrawing it failed"):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_failing_polls_count_as_unacknowledged(hass, aioclient_mock):
    _listing(aioclient_mock, {"connected": True})
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000}), 500])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="withdrawn"):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_clearing_the_queued_command_may_fail(hass, aioclient_mock):
    _listing(aioclient_mock, {"connected": True})
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000}), _shadow(SENT)])
    _replies(aioclient_mock, DEVICE, "patch", [{}, 400])
    await _client(hass).start_cook(DSN, **START)
    assert [p and p["id"] for p in _patches(aioclient_mock)] == [1001, None]


@pytest.mark.parametrize("connectivity", [{"connected": False}, {}, None])
async def test_start_requires_a_connected_grill(hass, aioclient_mock, connectivity):
    _listing(aioclient_mock, connectivity)
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="grill is not connected to the AWS cloud"):
        await _client(hass).start_cook(DSN, **START)
    assert _calls(aioclient_mock, DEVICE) == []


async def test_start_checks_connectivity_fresh(hass, backend):
    client = _client(hass)
    await client.find_device(DSN)
    await client.start_cook(DSN, **START)
    assert len(_calls(backend, DEVICES)) == 2


async def test_unreadable_shadow_sends_nothing(hass, aioclient_mock):
    _listing(aioclient_mock, {"connected": True})
    aioclient_mock.get(DEVICE, status=500, text="nope")
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError):
        await _client(hass).start_cook(DSN, **START)
    assert _calls(aioclient_mock, DEVICE, "PATCH") == []


async def test_stop_waits_for_nothing(hass, backend, sleep):
    await _client(hass).stop_cook(DSN)
    assert [p["id"] for p in _patches(backend)] == [1000]
    assert _calls(backend, DEVICE, "GET") == []
    sleep.assert_not_awaited()


def test_timestamp_without_zone_is_utc():
    state = state_from_device(DSN, {"updatedAt": "2026-08-19T18:39:58.666"})
    assert state.last_updated_at == datetime(2026, 8, 19, 18, 39, 58, 666000, tzinfo=timezone.utc)


async def test_device_with_malformed_registry_is_skipped(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": ["hh1"]})
    aioclient_mock.get(DEVICES, json={"items": [{"deviceId": "SND0", "registry": "?"}, aws_device()]})
    assert (await _client(hass).find_device(DSN))["deviceId"] == "SND1"


async def test_404_from_the_device_re_resolves_it_once(hass, backend):
    backend.clear_requests()
    backend.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    backend.get(HOUSEHOLDS, json={"households": ["hh1"]})
    backend.get(DEVICES, json={"items": [aws_device()]})
    _replies(backend, DEVICE, "patch", [404, {}])
    client = _client(hass)
    await client.stop_cook(DSN)
    assert len(_calls(backend, DEVICE, "PATCH")) == 2
    assert len(_calls(backend, DEVICES)) == 2
    assert len(_calls(backend, HOUSEHOLDS)) == 2


async def test_repeated_404_from_the_device_fails(hass, backend):
    backend.clear_requests()
    backend.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    backend.get(HOUSEHOLDS, json={"households": ["hh1"]})
    backend.get(DEVICES, json={"items": [aws_device()]})
    backend.patch(DEVICE, status=404, text="gone")
    with pytest.raises(TransportError):
        await _client(hass).stop_cook(DSN)
    assert len(_calls(backend, DEVICE, "PATCH")) == 2


async def test_404_from_the_device_list_re_resolves_the_household(hass, aioclient_mock):
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": ["hh1"]})
    _replies(aioclient_mock, DEVICES, "get", [404, {"items": [aws_device()]}])
    assert (await _client(hass).find_device(DSN))["deviceId"] == "SND1"
    assert len(_calls(aioclient_mock, HOUSEHOLDS)) == 2


def test_waiting_for_lid_state_is_mapped_back():
    assert normalise({"state": "waitlidclosedbeforecook"}) == {"state": "wait lid closed before cook"}


# ------------------------------------------------- I-03: acknowledgement correlation


def _listing_sequence(aioclient_mock, devices):
    aioclient_mock.post(AUTH0, json={"id_token": jwt("auth0|user1")})
    aioclient_mock.get(HOUSEHOLDS, json={"households": ["hh1"]})
    _replies(aioclient_mock, DEVICES, "get", [{"items": [d]} for d in devices])


def _device(**telemetry):
    device = aws_device()
    device["connectivityStatus"] = {"connected": True}
    device["telemetry"] = {**device["telemetry"], **telemetry}
    return device


async def test_rssi_change_alone_does_not_acknowledge(hass, aioclient_mock, sleep):
    before, after = _device(RSSI=-51), _device(RSSI=-50)
    _listing_sequence(aioclient_mock, [before, before, after, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_connectivity_change_alone_does_not_acknowledge(hass, aioclient_mock, sleep):
    before = _device()
    after = aws_device()
    after["connectivityStatus"] = {"connected": True, "lastSeen": "later"}
    after["telemetry"] = before["telemetry"]
    _listing_sequence(aioclient_mock, [before, before, after, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)


async def test_a_foreign_shadow_command_does_not_acknowledge(hass, aioclient_mock, sleep):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1234, "mode": "grill"})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)


async def test_a_missing_reported_command_does_not_acknowledge(hass, aioclient_mock, sleep):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [{"deviceId": "SND1", "shadow": {}}])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)


async def test_a_real_cook_transition_acknowledges(hass, aioclient_mock, sleep):
    before, after = _device(), _cooking_device(state="preheat")
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    aioclient_mock.patch(DEVICE, json={})
    await _client(hass).start_cook(DSN, **START)
    assert [p and p["id"] for p in _patches(aioclient_mock)] == [1001, None]


# ------------------------------------------------- I-04: the write phase is transactional


def _raise_once(error, then):
    calls = []

    async def reply(method, url, data):
        calls.append(url)
        if len(calls) == 1:
            raise error
        return AiohttpClientMockResponse(method, url, json=then)

    return reply


async def test_a_lost_patch_response_withdraws_the_start(hass, aioclient_mock, sleep):
    # The server may well have stored the start before the response was lost.
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    _replies(aioclient_mock, DEVICE, "patch", [500, {}])
    with pytest.raises(TransportError):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_cancellation_during_the_patch_withdraws_the_start(hass, aioclient_mock, sleep):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    aioclient_mock.patch(DEVICE, side_effect=_raise_once(asyncio.CancelledError(), {}))
    with pytest.raises(asyncio.CancelledError):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_an_auth_failure_while_waiting_withdraws_the_start(hass, aioclient_mock, sleep):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    aioclient_mock.patch(DEVICE, json={})
    client = _client(hass)
    client._acknowledged = AsyncMock(side_effect=AuthError("session expired"))
    with pytest.raises(AuthError):
        await client.start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_a_failed_withdrawal_after_a_failed_patch_is_not_reported_as_withdrawn(
    hass, aioclient_mock, sleep
):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow()])
    _replies(aioclient_mock, DEVICE, "patch", [500])
    with pytest.raises(TransportError, match="withdrawing it also failed"):
        await _client(hass).start_cook(DSN, **START)


# ------------------------------------------------- V-01: the ack must correlate with the command

SENT = {"id": 1001, "mode": "bake", "seconds set": 600, "temp": 180,
        "smoke": 0, "skip preheat": 0}


def _cooking_device(mode="bake", setpoint=180, left=600, total=600, state="cooking", smoke=0):
    """A connected grill whose telemetry shows the given cook."""
    device = _device()
    device["telemetry"] = {
        **device["telemetry"],
        "GrillState": json.dumps({
            "state": state, "mode": mode, "setpoint": setpoint, "smoke": smoke,
            "secondsset": total, "secondsleft": left, "probesactive": 0,
        }),
        "CookState": json.dumps({"state": {"state": "heat", "progress": 10}}),
    }
    return device


async def test_an_id_only_reported_command_does_not_acknowledge(hass, aioclient_mock, sleep):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000}), _shadow({"id": 1001})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_an_unchanged_identical_reported_command_does_not_acknowledge(
    hass, aioclient_mock, sleep
):
    # The very same command was already in the shadow before we wrote: reading
    # it back proves nothing about *this* order.
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow(SENT)])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)


async def test_a_matching_new_reported_command_acknowledges(hass, aioclient_mock, sleep):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000}), _shadow(SENT)])
    aioclient_mock.patch(DEVICE, json={})
    await _client(hass).start_cook(DSN, **START)
    assert [p and p["id"] for p in _patches(aioclient_mock)] == [1001, None]


async def test_the_countdown_of_a_running_cook_does_not_acknowledge(
    hass, aioclient_mock, sleep
):
    # A cook that keeps running unchanged while its timer ticks down is not the
    # grill applying our temperature change.
    before = _cooking_device(mode="roast", setpoint=180, left=500)
    after = _cooking_device(mode="roast", setpoint=180, left=498)
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, mode="roast", seconds=600, temp=200)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_telemetry_of_a_different_cook_does_not_acknowledge(hass, aioclient_mock, sleep):
    # Someone started another cook on the grill: active, changed - but not ours.
    before = _device()
    after = _cooking_device(mode="roast", setpoint=220)
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, **START)


# ------------------------------------------- R-01: telemetry only confirms what it shows


async def test_a_phase_change_does_not_acknowledge_an_unapplied_duration(
    hass, aioclient_mock, sleep
):
    # Preheat ends on its own; the requested 20 min never reached the grill.
    before = _cooking_device(mode="roast", state="preheat", left=600, total=600)
    after = _cooking_device(mode="roast", state="cooking", left=600, total=600)
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, mode="roast", seconds=1200, temp=180)
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_an_applied_duration_still_acknowledges(hass, aioclient_mock, sleep):
    before = _cooking_device(mode="roast", state="preheat", left=600, total=600)
    after = _cooking_device(mode="roast", state="cooking", left=1200, total=1200)
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    await _client(hass).start_cook(DSN, mode="roast", seconds=1200, temp=180)
    assert [p and p["id"] for p in _patches(aioclient_mock)] == [1001, None]


async def test_a_probe_target_is_not_confirmed_by_telemetry(hass, aioclient_mock, sleep):
    # Telemetry never shows the probe target, so a phase change proves nothing.
    before = _cooking_device(mode="roast", state="preheat")
    after = _cooking_device(mode="roast", state="cooking")
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(
            DSN, mode="roast", seconds=600, temp=180,
            probe_0={"mode": "manual", "setpoint": 75},
        )


async def test_a_probe_command_is_acknowledged_by_the_echo(hass, backend, sleep):
    await _client(hass).start_cook(
        DSN, **START, probe_0={"mode": "manual", "setpoint": 75}
    )
    assert [p and p["id"] for p in _patches(backend)] == [1001, None]


async def test_a_grill_heat_level_is_not_confirmed_by_telemetry(hass, aioclient_mock, sleep):
    # Grill mode sends a heat level; the °C the grill reports can't confirm it.
    before = _cooking_device(mode="grill", setpoint=230, state="preheat")
    after = _cooking_device(mode="grill", setpoint=230, state="cooking")
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(DSN, mode="grill", seconds=600, temp=2)


async def test_a_skipped_preheat_is_not_confirmed_by_telemetry(hass, aioclient_mock, sleep):
    before = _cooking_device(mode="roast", state="preheat")
    after = _cooking_device(mode="roast", state="cooking")
    _listing_sequence(aioclient_mock, [before, before, after])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    aioclient_mock.patch(DEVICE, json={})
    with pytest.raises(TransportError, match="did not acknowledge"):
        await _client(hass).start_cook(
            DSN, mode="roast", seconds=600, temp=180, skip_preheat=True
        )


# ------------------------------------------------- V-06: the withdrawal owns its cleanup


def _holding_withdrawal(aioclient_mock, release, events):
    """Patch handler that blocks the withdrawal (id 1000) until released."""

    async def patched(method, url, data):
        command = data["shadow"]["properties"]["desired"]["Cook_Command"]
        if command and command.get("id") == 1000:
            events.append("withdraw_started")
            await release.wait()
            events.append("withdraw_finished")
        return AiohttpClientMockResponse(method, url, json={})

    aioclient_mock.patch(DEVICE, side_effect=patched)


async def test_a_second_cancellation_still_waits_for_the_withdrawal(
    hass, aioclient_mock, sleep
):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    release, events = asyncio.Event(), []
    _holding_withdrawal(aioclient_mock, release, events)
    client = _client(hass)
    waiting = asyncio.Event()

    async def never_acknowledges(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()

    client._acknowledged = never_acknowledges
    task = asyncio.create_task(client.start_cook(DSN, **START))
    await waiting.wait()

    task.cancel()
    while "withdraw_started" not in events:
        await asyncio.sleep(0)
    task.cancel()                              # a second Stop press, mid-withdrawal
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()                     # the transaction is not over yet

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["withdraw_started", "withdraw_finished"]
    assert [p["id"] for p in _patches(aioclient_mock)] == [1001, 1000]


async def test_a_failing_withdrawal_after_cancellation_is_logged(
    hass, aioclient_mock, sleep, caplog
):
    _listing_sequence(aioclient_mock, [_device()])
    _replies(aioclient_mock, DEVICE, "get", [_shadow({"id": 1000})])
    _replies(aioclient_mock, DEVICE, "patch", [{}, 500])
    client = _client(hass)
    sleep.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await client.start_cook(DSN, **START)
    assert "withdraw" in caplog.text.casefold()
