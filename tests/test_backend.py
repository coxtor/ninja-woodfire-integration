import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ninja_woodfire._lib.api.aws import state_from_device
from custom_components.ninja_woodfire._lib.api.errors import TransportError

from .conftest import DEVICE, DOMAIN, DSN, aws_device, mk_state, mock_aws, setup_integration


def _ayla(state=None):
    client = AsyncMock()
    client.get_devices = AsyncMock(return_value=[DEVICE])
    client.get_combined_state = AsyncMock(return_value=state or mk_state())
    return client


def _aws(device):
    aws = mock_aws(device)
    aws.get_combined_state = AsyncMock(side_effect=lambda dsn: state_from_device(dsn, device))
    return aws


def _ayla_reported(age):
    state = mk_state()
    state.last_updated_at = datetime.now(tz=timezone.utc) - age
    return state


def _backend_sensor(hass):
    eid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DSN}_cloud_backend")
    return hass.states.get(eid).state


async def test_setup_uses_aws_when_grill_is_connected_there(hass, entry):
    ayla, aws = _ayla(), _aws(aws_device())
    await setup_integration(hass, entry, ayla, aws)
    coordinator = entry.runtime_data
    assert coordinator.backend == "aws"
    assert coordinator.data.grill.mode == "air crisp"
    assert _backend_sensor(hass) == "aws"
    await coordinator.async_refresh()
    assert aws.get_combined_state.await_count == 1
    assert ayla.get_combined_state.await_count == 1


async def test_setup_ignores_a_newer_aws_report_while_disconnected(hass, entry):
    now = datetime.now(tz=timezone.utc)
    device = aws_device(connected=False, updated_at=now.isoformat().replace("+00:00", "Z"))
    await setup_integration(hass, entry, _ayla(_ayla_reported(timedelta(days=2))), _aws(device))
    assert entry.runtime_data.backend == "ayla"


async def test_setup_needs_an_explicit_aws_connection(hass, entry):
    device = aws_device()
    del device["connectivityStatus"]
    await setup_integration(hass, entry, _ayla(), _aws(device))
    assert entry.runtime_data.backend == "ayla"


async def test_setup_stays_on_ayla_when_aws_is_stale(hass, entry):
    device = aws_device(connected=False)
    await setup_integration(hass, entry, _ayla(_ayla_reported(timedelta(0))), _aws(device))
    assert entry.runtime_data.backend == "ayla"
    assert _backend_sensor(hass) == "ayla"


async def test_setup_stays_on_ayla_when_grill_not_on_aws(hass, entry):
    ayla, aws = _ayla(), _aws(None)
    await setup_integration(hass, entry, ayla, aws)
    assert entry.runtime_data.backend == "ayla"
    await entry.runtime_data.async_refresh()
    assert ayla.get_combined_state.await_count == 2
    assert aws.get_combined_state.await_count == 0


async def test_setup_stays_on_ayla_when_aws_fails(hass, entry, caplog):
    caplog.set_level(logging.INFO)
    aws = mock_aws()
    aws.find_device.side_effect = TransportError("403 Forbidden")
    await setup_integration(hass, entry, _ayla(), aws)
    assert entry.runtime_data.backend == "ayla"
    assert entry.runtime_data.last_update_success
    assert any(r.levelno == logging.INFO and "403 Forbidden" in r.getMessage() for r in caplog.records)


async def test_commands_go_through_aws_when_selected(hass, entry):
    ayla, aws = _ayla(), _aws(aws_device())
    await setup_integration(hass, entry, ayla, aws)
    coordinator = entry.runtime_data
    coordinator._burst_refresh = AsyncMock()
    coordinator.cook_setting_mode = "bake"
    await coordinator.async_start_cook()
    await coordinator.async_modify_cook(temp=180)
    await coordinator.async_stop_cook()
    await coordinator.async_set_grill_name("Patio")
    assert aws.start_cook.await_count == 2
    assert aws.stop_cook.await_count == 1
    assert ayla.start_cook.await_count == ayla.stop_cook.await_count == 0
    ayla.set_grill_name.assert_awaited_once_with(DSN, "Patio")


async def test_aws_overrides_reach_the_client(hass, entry):
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "aws_api_base": "https://aws.invalid", "aws_api_key": "k"}
    )
    with patch("custom_components.ninja_woodfire.AylaCloudClient", return_value=_ayla()), \
         patch("custom_components.ninja_woodfire.AwsCloudClient", return_value=mock_aws()) as aws_cls:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    region = aws_cls.call_args.args[1]
    assert (region.aws_rest_base, region.aws_api_key) == ("https://aws.invalid", "k")


async def test_user_flow_stores_aws_overrides(hass):
    with patch("custom_components.ninja_woodfire.config_flow.AylaCloudClient", return_value=_ayla()), \
         patch("custom_components.ninja_woodfire.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "cloud"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "email": "a@b.c", "password": "x", "aws_api_base": " https://aws.invalid ", "aws_api_key": "",
        })
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"dsn": DSN})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"ble_slots": ""})
    assert r["result"].data["aws_api_base"] == "https://aws.invalid"
    assert "aws_api_key" not in r["result"].data


async def test_without_aws_client_the_backend_is_ayla(coordinator):
    coordinator.client.get_combined_state.return_value = mk_state()
    await coordinator._async_update_data()
    assert coordinator.backend == "ayla"


def _offline(state):
    state.online = False
    state.connection_status = "Offline"
    return state


async def test_offline_on_ayla_rechecks_aws_every_ten_minutes(hass, entry, freezer, caplog):
    caplog.set_level(logging.INFO)
    aws = _aws(None)
    await setup_integration(hass, entry, _ayla(_offline(mk_state())), aws)
    coordinator = entry.runtime_data
    for tick, checks in ((599, 1), (1, 2), (300, 2)):
        freezer.tick(tick)
        await coordinator.async_refresh()
        assert aws.find_device.await_count == checks
    aws.find_device.return_value = aws_device()
    freezer.tick(300)
    await coordinator.async_refresh()
    assert coordinator.backend == "aws"
    assert coordinator.data.online is True
    freezer.tick(3600)
    await coordinator.async_refresh()
    assert aws.find_device.await_count == 3
    switched = [r for r in caplog.records if "AWS backend; using it" in r.getMessage()]
    assert [r.levelno for r in switched] == [logging.INFO]


async def test_online_on_ayla_does_not_recheck_aws(hass, entry, freezer):
    aws = _aws(None)
    await setup_integration(hass, entry, _ayla(), aws)
    freezer.tick(3600)
    await entry.runtime_data.async_refresh()
    assert aws.find_device.await_count == 1


async def test_failing_recheck_is_logged_once(hass, entry, freezer, caplog):
    caplog.set_level(logging.INFO)
    aws = mock_aws()
    aws.find_device.side_effect = TransportError("down")
    await setup_integration(hass, entry, _ayla(_offline(mk_state())), aws)
    freezer.tick(600)
    await entry.runtime_data.async_refresh()
    assert aws.find_device.await_count == 2
    assert entry.runtime_data.last_update_success
    assert sum("down" in r.getMessage() for r in caplog.records) == 1


async def test_refused_aws_start_is_a_home_assistant_error(hass, entry):
    aws = _aws(aws_device())
    aws.start_cook.side_effect = TransportError("grill is not connected to the AWS cloud")
    await setup_integration(hass, entry, _ayla(), aws)
    coordinator = entry.runtime_data
    coordinator._burst_refresh = AsyncMock()
    with pytest.raises(HomeAssistantError, match="not connected"):
        await coordinator.async_start_cook()


def _online(state):
    state.connection_status = "Online"
    return state


async def test_disconnected_on_aws_switches_back_to_ayla(hass, entry, freezer, caplog):
    caplog.set_level(logging.INFO)
    device = aws_device()
    ayla, aws = _ayla(_offline(mk_state())), _aws(device)
    await setup_integration(hass, entry, ayla, aws)
    coordinator = entry.runtime_data
    assert coordinator.backend == "aws"
    device["connectivityStatus"]["connected"] = False
    freezer.tick(600)
    await coordinator.async_refresh()
    assert ayla.get_combined_state.await_count == 2
    assert coordinator.backend == "aws"
    ayla.get_combined_state.return_value = _online(mk_state())
    for tick, checks in ((599, 2), (1, 3)):
        freezer.tick(tick)
        await coordinator.async_refresh()
        assert ayla.get_combined_state.await_count == checks
    assert coordinator.backend == "ayla"
    assert coordinator.data.connection_status == "Online"
    await coordinator.async_refresh()
    assert coordinator.backend == "ayla"
    switched = [r for r in caplog.records if "using Ayla" in r.getMessage()]
    assert [r.levelno for r in switched] == [logging.INFO]


async def test_connected_on_aws_does_not_check_ayla(hass, entry, freezer):
    ayla, aws = _ayla(_online(mk_state())), _aws(aws_device())
    await setup_integration(hass, entry, ayla, aws)
    freezer.tick(3600)
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.backend == "aws"
    assert ayla.get_combined_state.await_count == 1


async def test_failed_first_aws_lookup_is_retried_after_a_minute(hass, entry, freezer):
    aws = mock_aws()
    aws.find_device.side_effect = TransportError("down")
    await setup_integration(hass, entry, _ayla(), aws)
    coordinator = entry.runtime_data
    for tick, checks in ((59, 1), (1, 2), (599, 2), (1, 3)):
        freezer.tick(tick)
        await coordinator.async_refresh()
        assert aws.find_device.await_count == checks
    aws.find_device.side_effect = None
    aws.find_device.return_value = aws_device()
    freezer.tick(600)
    await coordinator.async_refresh()
    assert coordinator.backend == "aws"


async def test_ble_is_preferred_when_configured(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    client = AsyncMock()
    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, client, DSN, WOODFIRE_PRO_XL, 42, ble=ble)
    await c._async_update_data()
    assert c.backend == "ble"
    client.get_combined_state.assert_not_called()


async def test_ble_failure_falls_back_to_cloud(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.api.errors import TransportError
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    client = AsyncMock()
    client.get_combined_state.return_value = mk_state()
    ble = AsyncMock()
    ble.get_combined_state.side_effect = TransportError("out of range")
    c = NinjaWoodfireCoordinator(hass, entry, client, DSN, WOODFIRE_PRO_XL, 42, ble=ble)
    await c._async_update_data()
    assert c.backend == "ayla"
    client.get_combined_state.assert_awaited()


async def test_local_only_coordinator_has_no_cloud(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
    from homeassistant.exceptions import HomeAssistantError

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble)
    await c._async_update_data()
    assert c.backend == "ble"
    with pytest.raises(HomeAssistantError):
        await c.async_set_grill_name("Patio")
