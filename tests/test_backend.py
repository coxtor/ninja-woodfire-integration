import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ninja_woodfire._lib.api.aws import state_from_device
from custom_components.ninja_woodfire._lib.api.errors import TransportError

from .conftest import DEVICE, DOMAIN, DSN, aws_device, cooking, mk_state, mock_aws, setup_integration


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
            "email": "a@b.c", "password": "x",
            "advanced_options": {"aws_api_base": " https://aws.invalid ", "aws_api_key": ""},
        })
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"dsn": DSN})
    assert r["result"].data["aws_api_base"] == "https://aws.invalid"
    assert "aws_api_key" not in r["result"].data


async def test_cloud_step_collapses_advanced_fields_into_a_section(hass):
    """The six vendor-override fields live in a collapsed section, not flat
    on the form — no reliance on the deprecated show_advanced_options."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "cloud"})
    schema = r["data_schema"].schema
    (section_key,) = (k for k in schema if str(k) == "advanced_options")
    section_validator = schema[section_key]
    assert section_validator.options["collapsed"] is True
    assert set(section_validator.schema.schema) == {
        "auth0_audience", "auth0_client_id", "ayla_app_id",
        "ayla_app_secret", "aws_api_base", "aws_api_key",
    }


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


# Encrypted mfg1 beacon segments (synthetic; see test_ble_beacon). probe0 53°C
# lid closed; lid open with no probe.
_BEACON_SLOT = (bytes(range(32)), bytes(range(16)))  # fake beacon slot (no real key)
_BEACON_PROBE53_CLOSED = bytes.fromhex("aaa391711626d18d8cad17032ef4ef4530e71a21")
_BEACON_NOPROBE_OPEN = bytes.fromhex("cbe858bf3f3e8e3df896c29992902dc8e3af90f1")


async def test_beacon_merges_probe_and_lid_into_local_state(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                    beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    assert c.backend == "ble"

    c.update_beacon(_BEACON_PROBE53_CLOSED)
    assert c.data.probes.probes[0].temp == 53.0
    assert c.data.probes.probes[0].plugged_in is True
    assert c.data.grill.lid_open is False


async def test_beacon_lid_without_probe(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                    beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    c.update_beacon(_BEACON_NOPROBE_OPEN)
    assert c.data.grill.lid_open is True
    assert all(p.temp == 0.0 for p in c.data.probes.probes) or not c.data.probes.probes


async def test_wake_sends_stop_to_wake_an_idle_grill(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()  # idle
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble)
    await c.async_refresh()
    c.async_request_refresh = AsyncMock()
    await c.async_wake()
    # A bare connect won't wake the grill; Stop does.
    ble.stop_cook.assert_awaited_once()


async def test_wake_never_stops_an_active_cook(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
    from .conftest import cooking

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state(cooking(mode="grill", setpoint=260))
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble)
    await c.async_refresh()
    c.async_request_refresh = AsyncMock()
    await c.async_wake()
    ble.stop_cook.assert_not_called()


_BEACON_PREHEAT = bytes.fromhex("73c4dc4a674c82774ffcaf700815aefd6733cf4c")
_BEACON_ADDFOOD = bytes.fromhex("c770529bdb2180b0652924210adf66ddadb5a039")


async def test_beacon_phase_and_prompt_merge_locally(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator, is_preheating
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                    beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    # Preheat: the beacon is the only local preheat signal, so Skip preheat works.
    c.update_beacon(_BEACON_PREHEAT)
    assert c.data.grill.state == "preheat"
    assert is_preheating(c.data) is True

    # Add-food prompt arrives -> prompt set, preheat is over.
    c.update_beacon(_BEACON_ADDFOOD)
    assert c.data.grill.prompt == "addfood"
    assert is_preheating(c.data) is False


@pytest.mark.asyncio
async def test_beacon_prompt_fires_a_prompt_event(hass, entry):
    from unittest.mock import AsyncMock
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire.const import EVENT_PROMPT
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state()
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    c._cook_seen = True                     # a cook is in progress
    events = async_capture_events(hass, EVENT_PROMPT)
    c.update_beacon(_BEACON_ADDFOOD)        # transient prompt via the fast beacon
    await hass.async_block_till_done()
    assert any(e.data.get("prompt") == "addfood" for e in events)


@pytest.mark.asyncio
async def test_stale_prompt_without_a_cook_is_ignored(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state()
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    # No cook seen + an idle beacon (phase None) carrying a left-over "get food"
    # prompt must NOT fabricate a prompt/active state (previously fired spurious
    # prompt + cook-done events with no cook running).
    import time as _time
    assert c._cook_seen is False
    c.beacon_phase = None
    c.beacon_prompt = "getfood"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.prompt is None
    assert c.data.grill.state != "get_food"
    assert c._cook_seen is False


@pytest.mark.asyncio
async def test_beacon_cooking_phase_shows_grilling_with_configured_mode(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()   # idle, no mode from GATT
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    # The grill is cooking (per the beacon) but the GATT state frame is absent:
    # the card must show "cooking" + the configured mode, not Unknown.
    c.cook_setting_mode = "smoker"
    c.beacon_phase = "cooking"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.state == "cooking"
    assert c.data.grill.mode == "smoker"


@pytest.mark.asyncio
async def test_asleep_grill_shows_powered_off_not_idle(hass, entry):
    # A BLE connect can succeed while the grill is asleep (it just won't
    # advertise a beacon or report a GATT state) -> grill.state "unknown".
    # That must map to "powered_off" so the card offers Wake, not a dead Start.
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state(grill={"state": "unknown"})
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()           # no beacon ever seen -> stale from the start
    assert c.data.grill.state == "powered_off"


@pytest.mark.asyncio
async def test_lid_flap_mid_cook_does_not_fire_spurious_cook_events(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire.const import EVENT_COOK_STARTED, EVENT_COOK_DONE
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.cook_setting_seconds = 1800

    # Get into an active cook (seed prev state so the next poll is a real
    # transition) and let the timer start.
    c.beacon_phase = "cooking"; c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    c._prev_grill_state = "cooking"
    c._cook_seen = True
    assert c.data.grill.seconds_set == 1800
    local_end_before = c._local_cook_end

    # Lid opens mid-cook: the beacon phase bits read blank while paused.
    c.beacon_phase = None
    c.beacon_lid = True
    c._merge_beacon(c.data)
    assert c.data.grill.state == "lid_open"      # not "idle"
    assert c._local_cook_end == local_end_before  # timer NOT reset

    started = async_capture_events(hass, EVENT_COOK_STARTED)
    done = async_capture_events(hass, EVENT_COOK_DONE)
    c._emit_lifecycle_events(c.data)
    await hass.async_block_till_done()
    assert started == []
    assert done == []

    # Lid closes again: back to cooking, timer keeps running (not restarted).
    c.beacon_lid = False
    c.beacon_phase = "cooking"
    c._merge_beacon(c.data)
    assert c.data.grill.state == "cooking"
    assert c._local_cook_end == local_end_before


@pytest.mark.asyncio
async def test_stop_is_not_reverted_by_a_stale_beacon_still_saying_cooking(hass, entry):
    # Real-world: Stop shows the optimistic "idle" immediately, but the
    # grill's own beacon lags a few seconds behind the actual command and
    # keeps advertising "cooking" for a bit -> must not flip the card back.
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.beacon_phase = "cooking"; c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.state == "cooking"

    await c.async_stop_cook()
    assert c.data.grill.state == "idle"

    # The beacon hasn't caught up yet and still says "cooking".
    c.beacon_phase = "cooking"; c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.state == "idle"

    # Once the grace window has passed, a genuinely new cook (e.g. started at
    # the grill itself) must still be able to show up again.
    c._stop_issued_at -= 31
    c._merge_beacon(c.data)
    assert c.data.grill.state == "cooking"


@pytest.mark.asyncio
async def test_stop_grace_does_not_strand_a_fresh_poll_on_unknown(hass, entry):
    # Real-world regression: a GATT poll during the stop-grace window builds
    # a brand new CombinedState whose default grill.state is "unknown" (no
    # 0x0f frame). The grace window must still resolve that to "idle" -- not
    # leave "unknown" on screen until the 30s grace expires.
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
    from custom_components.ninja_woodfire._lib.models import CombinedState, GrillState

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.beacon_phase = "cooking"; c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    await c.async_stop_cook()
    assert c.data.grill.state == "idle"

    # A fresh poll (no 0x0f frame) builds a brand new CombinedState whose
    # default grill.state is "unknown" -- exactly what `_combined_state`
    # returns in that case. The beacon is idle (no phase).
    fresh = CombinedState(dsn=DSN, grill=GrillState())
    assert fresh.grill.state == "unknown"
    c.beacon_phase = None; c._beacon_at = _time.monotonic()
    c._merge_beacon(fresh)
    assert fresh.grill.state == "idle"


@pytest.mark.asyncio
async def test_unconfirmed_start_fires_an_event(hass, entry):
    # Real-world: a sleeping grill can accept the BLE write for start_cook
    # without acting on it. Nothing ever shows up on its beacon -> fire a
    # clear signal instead of silently flickering back to idle unexplained.
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire.const import EVENT_START_NOT_CONFIRMED
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c._burst_refresh = AsyncMock()
    events = async_capture_events(hass, EVENT_START_NOT_CONFIRMED)

    with patch("custom_components.ninja_woodfire.coordinator.asyncio.sleep", AsyncMock()):
        await c.async_start_cook()
        await hass.async_block_till_done()
    assert [e.data["dsn"] for e in events] == [DSN]


@pytest.mark.asyncio
async def test_a_confirmed_start_fires_no_event(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire.const import EVENT_START_NOT_CONFIRMED
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c._burst_refresh = AsyncMock()
    events = async_capture_events(hass, EVENT_START_NOT_CONFIRMED)

    # The beacon independently confirms preheat right as the grace window
    # would otherwise elapse.
    async def _confirm_during_the_wait(_delay):
        c.beacon_phase = "preheat"
        c._beacon_at = _time.monotonic()
        c._merge_beacon(c.data)

    with patch("custom_components.ninja_woodfire.coordinator.asyncio.sleep", _confirm_during_the_wait):
        await c.async_start_cook()
        await hass.async_block_till_done()
    assert events == []


@pytest.mark.asyncio
async def test_likely_asleep_tracks_unconfirmed_starts_and_clears_on_wake(hass, entry):
    # The card uses this to offer Wake instead of Start before the next try.
    import time as _time
    from unittest.mock import AsyncMock, patch
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c._burst_refresh = AsyncMock()
    assert c.likely_asleep is False

    with patch("custom_components.ninja_woodfire.coordinator.asyncio.sleep", AsyncMock()):
        await c.async_start_cook()
        await hass.async_block_till_done()
    assert c.likely_asleep is True

    # Pressing Wake is an optimistic "the user just acted on it" -- move past
    # the start's own pending-grace window and the still-"preheat" optimistic
    # state first, or Wake's guard (confirmed standby only, never overtake a
    # start plausibly in flight) would no-op here.
    c.data.grill.state = "idle"
    c._issued_at -= 31
    c.async_request_refresh = AsyncMock()
    await c.async_wake()
    assert c.likely_asleep is False

    # A genuinely confirmed start clears it too, independent of Wake.
    c._likely_asleep = True
    c.beacon_phase = "preheat"; c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.likely_asleep is False


@pytest.mark.asyncio
async def test_local_ble_keeps_last_then_reports_offline(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
    from custom_components.ninja_woodfire._lib.api.errors import TransportError

    ble = AsyncMock()
    good = mk_state(grill={"state": "idle"})
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    assert c.data.grill.state == "idle"

    # BLE can't connect now. Recent contact -> keep the last state (no flapping).
    ble.get_combined_state.side_effect = TransportError("no connect")
    c._last_contact_m = _time.monotonic()
    await c.async_refresh()
    assert c.data.online is True and c.data.grill.state == "idle"

    # No contact for a while -> report the grill cleanly offline (card -> Wake).
    c._last_contact_m = _time.monotonic() - 999
    await c.async_refresh()
    assert c.data.online is False
    assert c.data.grill.state == "powered_off"


@pytest.mark.asyncio
async def test_skip_preheat_works_over_ble_without_a_live_timer(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state(grill={"state": "preheat"})   # preheating, no GATT timer
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.cook_setting_mode = "grill"
    c.cook_setting_seconds = 600

    await c.async_skip_preheat()              # must not raise "timer has run out"
    assert ble.start_cook.call_args.kwargs["skip_preheat"] is True


@pytest.mark.asyncio
async def test_add_food_prompt_shows_insert_food_and_holds_the_timer(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.cook_setting_seconds = 1800

    # Skip-preheat done -> display "add food": prompt wins, timer must NOT start.
    c.beacon_phase = "cooking"
    c.beacon_prompt = "addfood"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.state == "insert_food"
    assert not c.data.grill.seconds_set

    # Food in -> prompt clears -> really cooking -> timer starts now.
    c.beacon_prompt = None
    c._merge_beacon(c.data)
    assert c.data.grill.state == "cooking"
    assert c.data.grill.seconds_set == 1800


@pytest.mark.asyncio
async def test_skip_preheat_toggle_auto_skips_once_preheating(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state(grill={"state": "preheat"})
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    # Toggle on at start -> pending; next poll sees preheat -> auto-fires skip.
    c.async_skip_preheat = AsyncMock()
    c._auto_skip_pending = True
    await c.async_refresh()
    await hass.async_block_till_done()
    c.async_skip_preheat.assert_called_once()


@pytest.mark.asyncio
async def test_local_cook_timer_is_derived_while_cooking(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()   # no GATT timer
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.cook_setting_seconds = 1800
    c.beacon_phase = "cooking"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.seconds_set == 1800
    assert 1790 <= c.data.grill.seconds_left <= 1800
    assert c.data.grill.end_time_utc is not None


@pytest.mark.asyncio
async def test_local_cook_timer_waits_for_lid_to_close(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    c.cook_setting_seconds = 1800
    c.beacon_phase = "cooking"
    c.beacon_lid = True            # lid still open (loading) -> timer must wait
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert not c.data.grill.seconds_set

    c.beacon_lid = False           # lid closed -> timer starts now
    c._merge_beacon(c.data)
    assert c.data.grill.seconds_set == 1800


@pytest.mark.asyncio
async def test_air_crisp_allows_smoke(hass, entry):
    # Issue #3: Air Crisp + Woodfire flavour is a supported combination per the
    # app and Ninja's EU docs; the capability gate must not strip smoke=True.
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state()
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    await c.async_modify_cook(mode="air crisp", smoke=True)
    await c.async_start_cook()
    assert ble.start_cook.call_args.kwargs["mode"] == "air crisp"
    assert ble.start_cook.call_args.kwargs["smoke"] is True


@pytest.mark.asyncio
async def test_smoker_mode_forces_smoke_on(hass, entry):
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state()
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    # Selecting smoker with smoke explicitly off must snap smoke back on.
    await c.async_modify_cook(mode="smoker", smoke=False)
    assert c.cook_setting_smoke is True

    # ...and Start sends the cook with smoke on.
    await c.async_start_cook()
    assert ble.start_cook.call_args.kwargs["mode"] == "smoker"
    assert ble.start_cook.call_args.kwargs["smoke"] is True


@pytest.mark.asyncio
async def test_fresh_idle_beacon_pins_state_to_idle(hass, entry):
    import time as _time
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    # No GATT state frame -> grill.state would be "unknown" without the beacon.
    ble.get_combined_state.return_value = mk_state(grill={"state": "unknown"})
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()

    # Fresh beacon, no active phase -> grill is on but idle -> stable "idle".
    c.beacon_phase = None
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.state == "idle"


@pytest.mark.asyncio
async def test_asleep_mid_cook_still_fires_cook_done(hass, entry):
    # Regression: _merge_beacon can report grill_state "powered_off" (BLE connect
    # succeeded but no beacon/0x0f, e.g. grill went to standby mid-cook) while
    # state.online stays True, so _emit_lifecycle_events runs. IDLE_STATES must
    # recognize "powered_off" (underscore, like the "idle"/"lid_open" dual
    # spellings elsewhere) or the cook_done event — and the Node-RED
    # notification depending on it — is permanently lost for that cook.
    from unittest.mock import AsyncMock
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire.const import EVENT_COOK_DONE
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = mk_state(cooking())
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    assert c.data.grill.state == "cooking"

    # Grill goes to standby mid-cook: BLE connect still succeeds (online stays
    # True) but there's no fresh beacon and no GATT state -> "powered_off".
    ble.get_combined_state.return_value = mk_state(grill={"state": "unknown"})
    events = async_capture_events(hass, EVENT_COOK_DONE)
    await c.async_refresh()
    assert c.data.grill.state == "powered_off"
    await hass.async_block_till_done()
    assert any(e.data.get("reason") == "stopped" for e in events)


@pytest.mark.asyncio
async def test_ble_reachable_tracks_the_radio_independent_of_active_backend(hass, entry):
    # Hybrid setup: cloud configured as fallback, BLE preferred. ble_reachable
    # must reflect whether BLE itself answered this poll, not which backend's
    # data ended up in `coordinator.data` (e.g. still True/False during a
    # cloud-fallback poll that nonetheless rechecked BLE).
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    cloud = _ayla()
    c = NinjaWoodfireCoordinator(hass, entry, cloud, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    assert c.ble_reachable is None          # never tried yet

    await c.async_refresh()
    assert c.backend == "ble"
    assert c.ble_reachable is True

    # BLE fails this poll -> falls back to cloud for data, but ble_reachable
    # must reflect the failed radio, not the cloud backend now in use.
    ble.get_combined_state.side_effect = TransportError("no connect")
    await c.async_refresh()
    assert c.backend == "ayla"
    assert c.ble_reachable is False


@pytest.mark.asyncio
async def test_ble_reachable_is_none_without_ble_configured(hass, entry):
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    c = NinjaWoodfireCoordinator(hass, entry, _ayla(), DSN, WOODFIRE_PRO_XL, 0)
    await c.async_refresh()
    assert c.ble is None
    assert c.ble_reachable is None


@pytest.mark.asyncio
async def test_ble_reachable_stays_false_across_repeated_failures_local_only(hass, entry):
    # The actual real-world trigger: a local-only setup (no cloud client at
    # all) where the grill's BLE sleep cycle makes every poll fail for a
    # while. ble_reachable must stay False the whole time, not flip back to
    # True/None with nothing to fall back to.
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble,
                                 beacon_slot=_BEACON_SLOT)
    await c.async_refresh()
    assert c.backend == "ble"
    assert c.ble_reachable is True

    ble.get_combined_state.side_effect = TransportError("not in BLE range / no proxy")
    for _ in range(5):
        await c.async_refresh()
        assert c.ble_reachable is False


def _local(hass, entry, state=None, slot=_BEACON_SLOT):
    """A local-only (BLE) coordinator with a mocked radio."""
    from unittest.mock import AsyncMock
    from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator
    from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL

    ble = AsyncMock()
    good = state or mk_state()
    good.online = True
    ble.get_combined_state.return_value = good
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0,
                                 ble=ble, beacon_slot=slot)
    c._burst_refresh = AsyncMock()
    return c, ble


@pytest.mark.asyncio
async def test_add_food_step_stays_an_active_cook_and_blocks_wake(hass, entry):
    # I-01: "add food" is a wait step *inside* the cook. It must not look idle
    # (settings stay live) and Wake must never send a Stop while it is showing.
    import time as _time
    from unittest.mock import AsyncMock

    c, ble = _local(hass, entry)
    await c.async_refresh()
    c.beacon_phase = "cooking"
    c.beacon_prompt = "addfood"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)

    assert c.data.grill.state == "insert_food"
    assert c.is_cook_active is True

    c.async_request_refresh = AsyncMock()
    await c.async_wake()
    ble.stop_cook.assert_not_called()


@pytest.mark.asyncio
async def test_preheat_to_cooking_via_add_food_fires_one_start(hass, entry):
    # I-01: preheat -> add food -> lid open -> cooking is ONE cook: exactly one
    # cook_started, one preheat_complete and no cook_done in between.
    import time as _time
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.const import (
        EVENT_COOK_DONE,
        EVENT_COOK_STARTED,
        EVENT_PREHEAT_COMPLETE,
    )

    c, _ble = _local(hass, entry)
    await c.async_refresh()
    started = async_capture_events(hass, EVENT_COOK_STARTED)
    preheat = async_capture_events(hass, EVENT_PREHEAT_COMPLETE)
    done = async_capture_events(hass, EVENT_COOK_DONE)

    def beacon(phase, prompt=None, lid=False):
        c.beacon_phase, c.beacon_prompt, c.beacon_lid = phase, prompt, lid
        c._beacon_at = _time.monotonic()
        c._merge_beacon(c.data)
        c._emit_lifecycle_events(c.data)

    beacon("preheat")
    assert c.data.grill.state == "preheat"
    beacon("cooking", prompt="addfood")
    assert c.data.grill.state == "insert_food"
    beacon(None, lid=True)
    assert c.data.grill.state == "lid_open"
    beacon("cooking")
    assert c.data.grill.state == "cooking"

    await hass.async_block_till_done()
    assert len(started) == 1
    assert len(preheat) == 1
    assert done == []


@pytest.mark.asyncio
async def test_wake_only_stops_a_positively_idle_grill(hass, entry):
    # I-01: Wake must key off a confirmed standby/idle state, not "not active".
    from unittest.mock import AsyncMock

    c, ble = _local(hass, entry, mk_state(grill={"state": "idle"}))
    await c.async_refresh()
    c.async_request_refresh = AsyncMock()
    await c.async_wake()
    ble.stop_cook.assert_awaited_once()

    ble.stop_cook.reset_mock()
    c.data.grill.state = "insert_food"
    await c.async_wake()
    ble.stop_cook.assert_not_called()


@pytest.mark.asyncio
async def test_a_vanished_probe_and_cleared_prompt_are_applied(hass, entry):
    # I-07: a fresh beacon is a full snapshot of its own fields. Unplugging the
    # probe / the prompt clearing must reach the entities, not be ignored
    # because the new beacon carries "nothing" for those fields.
    import time as _time

    c, _ble = _local(hass, entry)
    await c.async_refresh()
    c._cook_seen = True
    c.beacon_phase = "cooking"
    c.beacon_probes = [53.0, None]
    c.beacon_prompt = "flipfood"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.probes.probes[0].plugged_in is True
    assert c.data.grill.prompt == "flipfood"

    c.beacon_probes = [None, None]
    c.beacon_prompt = None
    c._merge_beacon(c.data)
    assert c.data.probes.probes[0].plugged_in is False
    assert c.data.grill.prompt is None


@pytest.mark.asyncio
async def test_a_repeated_prompt_fires_a_second_event(hass, entry):
    # I-07: flipfood -> cleared -> flipfood must notify twice.
    import time as _time
    from pytest_homeassistant_custom_component.common import async_capture_events
    from custom_components.ninja_woodfire.const import EVENT_PROMPT

    c, _ble = _local(hass, entry)
    await c.async_refresh()
    c._cook_seen = True
    c.beacon_phase = "cooking"
    events = async_capture_events(hass, EVENT_PROMPT)

    for prompt in ("flipfood", None, "flipfood"):
        c.beacon_prompt = prompt
        c._beacon_at = _time.monotonic()
        c._merge_beacon(c.data)
        c._emit_lifecycle_events(c.data)

    await hass.async_block_till_done()
    assert [e.data["prompt"] for e in events] == ["flipfood", "flipfood"]


@pytest.mark.asyncio
async def test_a_stale_beacon_stops_presenting_its_readings_as_current(hass, entry):
    import time as _time

    c, _ble = _local(hass, entry)
    await c.async_refresh()
    c._cook_seen = True
    c.beacon_phase = "cooking"
    c.beacon_probes = [53.0, None]
    c.beacon_prompt = "flipfood"
    c.beacon_lid = True
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.probes.probes[0].temp == 53.0

    c._beacon_at = _time.monotonic() - 999      # radio silence
    c._merge_beacon(c.data)
    assert c.data.probes.probes == []
    assert c.data.grill.prompt is None
    assert c.data.grill.lid_open is None


@pytest.mark.asyncio
async def test_the_estimated_timer_is_refreshed_on_every_merge(hass, entry, freezer):
    # I-08: the estimate is recomputed each merge; it must not freeze at the
    # value written into the state the first time round.
    import time as _time

    c, _ble = _local(hass, entry)
    await c.async_refresh()
    c.cook_setting_seconds = 600
    c.beacon_phase = "cooking"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.seconds_left == 600

    freezer.tick(60)
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.seconds_left == 540


@pytest.mark.asyncio
async def test_a_live_duration_change_moves_the_estimated_end(hass, entry, freezer):
    # I-08 acceptance: 600 s cook, after 120 s raised to 900 s total -> 780 s
    # left; the next change must carry those 780 s, not the old remainder.
    import time as _time

    c, ble = _local(hass, entry)
    await c.async_refresh()
    c.cook_setting_seconds = 600
    c.beacon_phase = "cooking"
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)

    freezer.tick(120)
    c._beacon_at = _time.monotonic()
    c._merge_beacon(c.data)
    assert c.data.grill.seconds_left == 480

    calls = []
    ble.start_cook.side_effect = lambda dsn, **kw: calls.append(kw)
    await c.async_modify_cook(seconds=900)
    sent = calls[-1]["seconds"]
    assert abs(sent - 780) <= 1              # whole-second truncation only

    c._merge_beacon(c.data)
    assert c.data.grill.seconds_set == 900
    assert c.data.grill.seconds_left == sent

    await c.async_modify_cook(temp=2)
    assert calls[-1]["seconds"] == sent      # the new remainder, not the old one
