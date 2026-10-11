from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory

from custom_components.ninja_woodfire._lib.api.aws import state_from_device
from custom_components.ninja_woodfire._lib.api.errors import AuthError, TransportError

from .conftest import DEVICE, DOMAIN, DSN, aws_device, mk_state, mock_aws, setup_integration

# HA renders timestamp states without microseconds.
REPORTED = datetime(2026, 8, 19, 18, 39, 58, tzinfo=timezone.utc)
DIAGNOSTICS = {
    ("sensor", "last_report"),
    ("sensor", "wifi_signal"),
    ("sensor", "last_successful_poll"),
    ("sensor", "failed_polls"),
    ("sensor", "cloud_backend"),
    ("binary_sensor", "cloud_connected"),
}
SURVIVE_FAILED_POLL = {
    "failed_polls", "last_successful_poll", "last_report", "cloud_backend", "cloud_connected",
}


def _ayla(state=None):
    client = AsyncMock()
    client.get_devices = AsyncMock(return_value=[DEVICE])
    client.get_combined_state = AsyncMock(return_value=state or mk_state())
    return client


def _aws(device):
    aws = mock_aws(device)
    aws.get_combined_state = AsyncMock(side_effect=lambda dsn: state_from_device(dsn, device))
    return aws


def _state(hass, key, platform="sensor"):
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DSN}_{key}")
    return hass.states.get(eid)


def test_aws_telemetry_carries_rssi():
    assert state_from_device(DSN, aws_device()).rssi == -65


def test_rssi_missing_from_telemetry_is_none():
    device = aws_device()
    device["telemetry"] = {k: v for k, v in device["telemetry"].items() if k != "RSSI"}
    assert state_from_device(DSN, device).rssi is None


async def test_entities_are_enabled_diagnostics(hass, entry):
    await setup_integration(hass, entry, _ayla())
    reg = er.async_get(hass)
    for platform, key in DIAGNOSTICS:
        entity = reg.async_get(reg.async_get_entity_id(platform, DOMAIN, f"{DSN}_{key}"))
        assert entity.entity_category is EntityCategory.DIAGNOSTIC, key
        assert entity.disabled_by is None, key


async def test_values_on_aws(hass, entry):
    await setup_integration(hass, entry, _ayla(), _aws(aws_device()))
    assert _state(hass, "wifi_signal").state == "-65"
    assert _state(hass, "wifi_signal").attributes["unit_of_measurement"] == "dBm"
    assert datetime.fromisoformat(_state(hass, "last_report").state) == REPORTED
    assert _state(hass, "cloud_backend").state == "aws"


async def test_values_on_ayla(hass, entry):
    state = mk_state()
    state.last_updated_at = REPORTED.replace(microsecond=666000)
    await setup_integration(hass, entry, _ayla(state))
    assert _state(hass, "wifi_signal").state == "unknown"
    assert datetime.fromisoformat(_state(hass, "last_report").state) == REPORTED
    assert _state(hass, "failed_polls").state == "0"
    polled = datetime.fromisoformat(_state(hass, "last_successful_poll").state)
    assert datetime.now(tz=timezone.utc) - polled < timedelta(minutes=1)


async def test_readable_while_grill_offline(hass, entry):
    state = mk_state()
    state.online = False
    state.connection_status = "Offline"
    state.last_updated_at = REPORTED
    await setup_integration(hass, entry, _ayla(state), _aws(aws_device(connected=False)))
    for platform, key in DIAGNOSTICS:
        assert _state(hass, key, platform).state != STATE_UNAVAILABLE, key


async def test_failed_polls_count_and_reset(hass, entry, freezer):
    client = _ayla()
    await setup_integration(hass, entry, client)
    coordinator = entry.runtime_data
    polled = _state(hass, "last_successful_poll").state

    client.get_combined_state.side_effect = TransportError("503 Service Unavailable")
    await coordinator.async_refresh()
    client.get_combined_state.side_effect = AuthError("token rejected")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    failed = _state(hass, "failed_polls")
    assert failed.state == "2"
    assert failed.attributes["last_error"] == "token rejected"
    assert _state(hass, "last_successful_poll").state == polled
    for platform, key in DIAGNOSTICS:
        unavailable = _state(hass, key, platform).state == STATE_UNAVAILABLE
        assert unavailable is (key not in SURVIVE_FAILED_POLL), key
    assert _state(hass, "grill_state").state == STATE_UNAVAILABLE

    freezer.tick(60)
    client.get_combined_state.side_effect = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "failed_polls").state == "0"
    assert _state(hass, "failed_polls").attributes["last_error"] == "token rejected"
    assert _state(hass, "last_successful_poll").state != polled


async def test_transport_error_message_is_recorded(hass, entry):
    client = _ayla()
    await setup_integration(hass, entry, client)
    client.get_combined_state.side_effect = TransportError("503 Service Unavailable")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "failed_polls").attributes["last_error"] == (
        "transport: 503 Service Unavailable"
    )
