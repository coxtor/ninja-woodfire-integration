import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
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


async def test_setup_uses_aws_when_its_report_is_newer(hass, entry):
    now = datetime.now(tz=timezone.utc)
    device = aws_device(connected=False, updated_at=now.isoformat().replace("+00:00", "Z"))
    await setup_integration(hass, entry, _ayla(_ayla_reported(timedelta(days=2))), _aws(device))
    assert entry.runtime_data.backend == "aws"


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
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "email": "a@b.c", "password": "x", "aws_api_base": " https://aws.invalid ", "aws_api_key": "",
        })
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"dsn": DSN})
    assert r["result"].data["aws_api_base"] == "https://aws.invalid"
    assert "aws_api_key" not in r["result"].data


async def test_without_aws_client_the_backend_is_ayla(coordinator):
    coordinator.client.get_combined_state.return_value = mk_state()
    await coordinator._async_update_data()
    assert coordinator.backend == "ayla"
