import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ninja_woodfire._lib.api.ayla import AylaCloudClient, AylaSession, TransportError
from custom_components.ninja_woodfire._lib.models import CombinedState, GrillState
from custom_components.ninja_woodfire.const import SCAN_INTERVAL_IDLE
from custom_components.ninja_woodfire.number import CookTempNumber
from custom_components.ninja_woodfire.sensor import _plausible_probe_temp
from custom_components.ninja_woodfire.switch import _maybe_skip_preheat_now

from .conftest import DEVICE, DSN, cooking, mk_state, probe, setup_integration

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://ads-eu.aylanetworks.com/apiv1/dsns/X"
PROPS = f"{BASE}/properties.json"
DEVICE_URL = f"{BASE}.json"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fresh(name, online=True):
    props = fixture(name)["properties"]
    return CombinedState(
        dsn=DSN,
        grill=GrillState.from_property_value(props["GET_GrillState"]["value"]),
        online=online,
        last_updated_at=datetime.now(tz=timezone.utc),
    )


def offline(state):
    state.online = False
    state.connection_status = "Offline"
    return state


def _client(hass):
    client = AylaCloudClient("a@b.c", "pw", session=async_get_clientsession(hass))
    client._ayla = AylaSession("tok", "rt", 9e12)
    return client


def _serve(aioclient_mock, name, device=None):
    fx = fixture(name)
    aioclient_mock.get(PROPS, json=[
        {"property": {"name": k, **v}} for k, v in fx["properties"].items()
    ])
    if device is not False:
        aioclient_mock.get(DEVICE_URL, json={"device": device or fx["device"]})


def _device_calls(aioclient_mock):
    return sum(1 for _, url, _, _ in aioclient_mock.mock_calls if str(url) == DEVICE_URL)


# ---------------------------------------------------------------- transport


async def test_bug_report_snapshot_is_reported_offline(hass, aioclient_mock):
    _serve(aioclient_mock, "idle_offline")
    state = await _client(hass).get_combined_state("X")
    assert state.grill.state == "idle"
    assert state.connection_status == "Offline"
    assert state.online is False
    assert state.state_age_seconds() > 20 * 3600
    for name in ("grill", "air", "smoke"):
        assert state.temp_is_plausible(getattr(state.grill.temps, name), name) is False


@pytest.mark.parametrize(("status", "online"), [("Online", True), ("online", True), ("Offline", False), (None, True)])
async def test_connection_status_maps_to_online(hass, aioclient_mock, status, online):
    _serve(aioclient_mock, "idle_offline", {"connection_status": status})
    state = await _client(hass).get_combined_state("X")
    assert state.online is online
    assert state.connection_status == status


async def test_without_device_record_stays_online(hass, aioclient_mock):
    _serve(aioclient_mock, "idle_offline", device=False)
    aioclient_mock.get(DEVICE_URL, status=500)
    state = await _client(hass).get_combined_state("X")
    assert state.online is True
    assert state.connection_status is None


async def test_device_record_is_cached(hass, aioclient_mock, freezer):
    _serve(aioclient_mock, "idle_offline")
    client = _client(hass)
    await client.get_combined_state("X")
    freezer.tick(20)
    await client.get_combined_state("X")
    assert _device_calls(aioclient_mock) == 1
    freezer.tick(11)
    await client.get_combined_state("X")
    assert _device_calls(aioclient_mock) == 2


async def test_failed_device_refresh_keeps_previous_record(hass, aioclient_mock, freezer):
    _serve(aioclient_mock, "idle_offline")
    client = _client(hass)
    await client.get_combined_state("X")
    aioclient_mock.clear_requests()
    _serve(aioclient_mock, "idle_offline", device=False)
    aioclient_mock.get(DEVICE_URL, status=500)
    freezer.tick(31)
    state = await client.get_combined_state("X")
    assert _device_calls(aioclient_mock) == 1
    assert state.online is False
    assert state.connection_status == "Offline"


async def test_failed_device_refresh_drops_record_after_five_minutes(hass, aioclient_mock, freezer):
    _serve(aioclient_mock, "idle_offline")
    client = _client(hass)
    await client.get_combined_state("X")
    aioclient_mock.clear_requests()
    _serve(aioclient_mock, "idle_offline", device=False)
    aioclient_mock.get(DEVICE_URL, status=500)
    freezer.tick(301)
    state = await client.get_combined_state("X")
    assert state.online is True
    assert state.connection_status is None


# ---------------------------------------------------------------- plausibility


def test_offline_snapshot_suppresses_temperatures():
    state = offline(mk_state(cooking(mode="air crisp", inputs={"temps": {"grill": 180, "air": 175}})))
    state.last_updated_at = datetime.now(tz=timezone.utc)
    assert state.temp_is_plausible(180, "grill") is False
    assert state.temp_is_plausible(175, "air") is False


@pytest.mark.parametrize("name", ["powered_off", "zc_loss"])
def test_powered_off_is_covered_by_idle_cap(name):
    state = fresh(name)
    assert state.grill.state == "powered OFF"
    assert state.temp_is_plausible(120.0, "grill") is False


def test_idle_cap_keeps_plausible_ambient_and_finished_cook():
    state = fresh("idle_offline")
    assert state.temp_is_plausible(25.0, "grill") is True
    state.grill.state = "done"
    assert state.temp_is_plausible(220.0, "grill") is True


@pytest.mark.parametrize("grill_state", ["Idle", "powered OFF", "unknown"])
def test_probe_idle_cap_uses_same_states(grill_state):
    state = mk_state({"state": grill_state}, probes={"probes": [probe(temp=80, active=0)]})
    assert _plausible_probe_temp(state, 0) is None
    state.probes.probes[0].temp = 25
    assert _plausible_probe_temp(state, 0) == 25


# ---------------------------------------------------------------- coordinator


async def test_offline_cook_does_not_poll_fast(coordinator):
    state = offline(mk_state(cooking()))
    state.last_updated_at = datetime.now(tz=timezone.utc)
    coordinator.client.get_combined_state.return_value = state
    await coordinator._async_update_data()
    assert coordinator.update_interval == SCAN_INTERVAL_IDLE


async def test_no_events_across_offline_period(hass, coordinator, events):
    for state in (
        mk_state(cooking(left=1700)),
        offline(mk_state(cooking(left=1700))),
        offline(mk_state()),
        mk_state(),
    ):
        coordinator.client.get_combined_state.return_value = state
        coordinator.data = await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_offline_transition_logged_once(coordinator, caplog):
    caplog.set_level(logging.INFO)
    for state in (mk_state(), offline(mk_state()), offline(mk_state()), mk_state()):
        coordinator.client.get_combined_state.return_value = state
        coordinator.data = await coordinator._async_update_data()
    ours = [r for r in caplog.records if r.name == "custom_components.ninja_woodfire.coordinator"]
    assert [r.levelno for r in ours] == [logging.WARNING, logging.INFO]


async def test_start_raises_when_offline(coordinator):
    coordinator.data = offline(mk_state())
    with pytest.raises(HomeAssistantError):
        await coordinator.async_start_cook()
    assert coordinator.client.start_cook.await_count == 0


async def test_skip_preheat_raises_when_offline(coordinator):
    coordinator.data = offline(mk_state(cooking(state="preheat")))
    with pytest.raises(HomeAssistantError):
        await coordinator.async_skip_preheat()
    assert coordinator.client.start_cook.await_count == 0


async def test_skip_preheat_switch_while_offline_only_stages(coordinator):
    coordinator.data = offline(mk_state(cooking(state="preheat")))
    await _maybe_skip_preheat_now(coordinator, True)
    assert coordinator.client.start_cook.await_count == 0


async def test_number_set_while_offline_only_stages(coordinator):
    coordinator.data = offline(mk_state(cooking()))
    assert coordinator.is_cook_active is False
    await CookTempNumber(coordinator).async_set_native_value(210)
    assert coordinator.client.start_cook.await_count == 0
    assert coordinator.cook_setting_temp == 210


# ---------------------------------------------------------------- entities

STAYS_AVAILABLE = {
    "cook_duration", "cook_temp", "probe0_target_temp", "probe1_target_temp",
    "cook_mode", "cook_smoke", "cook_skip_preheat", "probe0_enabled", "probe1_enabled",
    "firmware_version", "oem_model", "dsn", "stop_cook", "cloud_connected", "cloud_backend",
    "last_report", "wifi_signal", "last_successful_poll", "failed_polls",
    # Pressable ad-hoc even while offline: skip-preheat and wake-the-grill.
    "skip_preheat", "wake",
}


async def _setup(hass, entry, state):
    client = AsyncMock()
    client.get_devices = AsyncMock(return_value=[DEVICE])
    client.get_combined_state = AsyncMock(return_value=state)
    await setup_integration(hass, entry, client)


def _available_keys(hass):
    reg = er.async_get(hass)
    return {
        reg.async_get(s.entity_id).unique_id.removeprefix(f"{DSN}_")
        for s in hass.states.async_all()
        if s.state != STATE_UNAVAILABLE
    }


async def test_offline_grill_entities_unavailable(hass, entry):
    state = offline(mk_state())
    state.last_updated_at = datetime.now(tz=timezone.utc) - timedelta(hours=22)
    await _setup(hass, entry, state)
    assert _available_keys(hass) == STAYS_AVAILABLE
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("binary_sensor", "ninja_woodfire", f"{DSN}_cloud_connected")
    conn = hass.states.get(eid)
    assert conn.state == "off"
    assert conn.attributes["connection_status"] == "Offline"
    assert "last_report_age_seconds" not in conn.attributes


async def test_online_grill_offers_start_and_cloud_connected(hass, entry):
    state = mk_state()
    state.connection_status = "Online"
    await _setup(hass, entry, state)
    keys = _available_keys(hass)
    assert {"start_cook", "skip_preheat", "grill_state", "cloud_connected"} <= keys


async def test_stop_stays_available_after_failed_poll(hass, entry):
    await _setup(hass, entry, mk_state())
    coordinator = entry.runtime_data
    coordinator.client.get_combined_state.side_effect = TransportError("boom")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    keys = _available_keys(hass)
    assert "stop_cook" in keys
    assert "start_cook" not in keys
