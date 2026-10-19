from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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
# These two update on every single poll (every 3-10s while active), which
# spams the logbook/history for anyone who hasn't asked for that level of
# detail. Disabled by default; still available for anyone who enables them.
DISABLED_BY_DEFAULT = {("sensor", "last_report"), ("sensor", "last_successful_poll")}
SURVIVE_FAILED_POLL = {
    "failed_polls", "last_successful_poll", "last_report", "cloud_backend", "cloud_connected",
}


async def _enable_and_reload(hass, entry, client, aws, *keys):
    """Flip disabled-by-default entities on, then reload so they get added.

    Keeps using the same mocked clients the test already set up — a reload
    re-runs async_setup_entry, which would otherwise hit real cloud clients.
    """
    reg = er.async_get(hass)
    for platform, key in keys:
        entity_id = reg.async_get_entity_id(platform, DOMAIN, f"{DSN}_{key}")
        reg.async_update_entity(entity_id, disabled_by=None)
    with patch("custom_components.ninja_woodfire.AylaCloudClient", return_value=client), \
         patch("custom_components.ninja_woodfire.AwsCloudClient", return_value=aws or mock_aws()):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()


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
        expect_disabled = (platform, key) in DISABLED_BY_DEFAULT
        assert (entity.disabled_by is not None) is expect_disabled, key


async def test_values_on_aws(hass, entry):
    ayla, aws = _ayla(), _aws(aws_device())
    await setup_integration(hass, entry, ayla, aws)
    await _enable_and_reload(hass, entry, ayla, aws, ("sensor", "last_report"))
    assert _state(hass, "wifi_signal").state == "-65"
    assert _state(hass, "wifi_signal").attributes["unit_of_measurement"] == "dBm"
    assert datetime.fromisoformat(_state(hass, "last_report").state) == REPORTED
    assert _state(hass, "cloud_backend").state == "aws"


async def test_values_on_ayla(hass, entry):
    state = mk_state()
    state.last_updated_at = REPORTED.replace(microsecond=666000)
    ayla = _ayla(state)
    await setup_integration(hass, entry, ayla)
    await _enable_and_reload(
        hass, entry, ayla, None, ("sensor", "last_report"), ("sensor", "last_successful_poll")
    )
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
    ayla, aws = _ayla(state), _aws(aws_device(connected=False))
    await setup_integration(hass, entry, ayla, aws)
    await _enable_and_reload(
        hass, entry, ayla, aws, ("sensor", "last_report"), ("sensor", "last_successful_poll")
    )
    for platform, key in DIAGNOSTICS:
        assert _state(hass, key, platform).state != STATE_UNAVAILABLE, key


async def test_failed_polls_count_and_reset(hass, entry, freezer):
    client = _ayla()
    await setup_integration(hass, entry, client)
    await _enable_and_reload(
        hass, entry, client, None, ("sensor", "last_successful_poll"), ("sensor", "last_report")
    )
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
    # Recovered: the resolved failure must not keep showing as the last error.
    assert _state(hass, "failed_polls").attributes["last_error"] is None
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
