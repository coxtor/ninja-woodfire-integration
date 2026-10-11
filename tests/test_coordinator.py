import time
from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.ninja_woodfire.const import (
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_TARGET_REACHED,
    SCAN_INTERVAL_ACTIVE,
    SCAN_INTERVAL_IDLE,
)

from .conftest import cooking, mk_state, probe


def sent(coordinator):
    return coordinator.client.start_cook.call_args.kwargs


# ---------------------------------------------------------------- re-issue


async def test_modify_in_grill_mode_keeps_heat_level_of_foreign_cook(coordinator):
    coordinator.data = mk_state(cooking(mode="grill", setpoint=260))
    await coordinator.async_modify_cook(smoke=True)
    assert sent(coordinator)["temp"] == 3
    assert sent(coordinator)["smoke"] is True


async def test_modify_in_grill_mode_prefers_issued_level(coordinator):
    coordinator.cook_setting_mode = "grill"
    coordinator.cook_setting_temp = 1
    await coordinator.async_start_cook()
    coordinator.data = mk_state(cooking(mode="grill", setpoint=230))
    await coordinator.async_modify_cook(smoke=True)
    assert sent(coordinator)["temp"] == 1


async def test_grill_mode_live_temp_is_level(coordinator):
    coordinator.data = mk_state(cooking(mode="grill", setpoint=190))
    assert coordinator.live_or_staged_temp == 2


async def test_modify_keeps_probe_targets(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=74), probe(active=0, name="probe1")]})
    await coordinator.async_modify_cook(temp=210)
    assert sent(coordinator)["probe_0"] == {"mode": "manual", "setpoint": 74}
    assert sent(coordinator)["probe_1"] is None


async def test_duration_change_sets_new_total(coordinator):
    coordinator.data = mk_state(cooking(left=600, total=1800))
    await coordinator.async_modify_cook(seconds=35 * 60)
    assert sent(coordinator)["seconds"] in range(895, 905)


async def test_reissued_cook_reports_original_total(coordinator):
    coordinator.data = mk_state(cooking(left=600, total=1800))
    await coordinator.async_modify_cook(smoke=True)
    s = sent(coordinator)["seconds"]
    coordinator.client.get_combined_state.return_value = mk_state(cooking(left=s, total=s))
    data = await coordinator._async_update_data()
    assert data.grill.seconds_set in range(1795, 1805)


async def test_past_endtime_falls_back_to_seconds_left(coordinator):
    coordinator.data = mk_state(cooking(left=600, endtimeutc=int(time.time()) - 30))
    await coordinator.async_modify_cook(smoke=True)
    assert sent(coordinator)["seconds"] == 600


async def test_modify_during_preheat_is_sent_without_skipping(coordinator):
    coordinator.data = mk_state(cooking(state="preheat", left=1800), {"state": {"state": "preheat"}})
    await coordinator.async_modify_cook(temp=220)
    assert sent(coordinator)["temp"] == 220
    assert sent(coordinator)["skip_preheat"] is False
    assert sent(coordinator)["seconds"] in range(1795, 1805)


async def test_modify_while_idle_only_stages_and_notifies(coordinator):
    calls = []
    coordinator.async_add_listener(lambda: calls.append(1))
    coordinator.data = mk_state()
    await coordinator.async_modify_cook(mode="smoker")
    assert coordinator.client.start_cook.await_count == 0
    assert coordinator.cook_setting_mode == "smoker"
    assert calls


async def test_skip_preheat_when_not_preheating_raises(coordinator):
    coordinator.data = mk_state()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_skip_preheat()
    assert coordinator.client.start_cook.await_count == 0


async def test_skip_preheat_in_grill_mode_sends_level_and_probes(coordinator):
    coordinator.data = mk_state(
        cooking(mode="grill", setpoint=230, state="preheat"),
        probes={"probes": [probe(setpoint=60)]},
    )
    await coordinator.async_skip_preheat()
    assert sent(coordinator)["temp"] == 3
    assert sent(coordinator)["skip_preheat"] is True
    assert sent(coordinator)["probe_0"] == {"mode": "manual", "setpoint": 60}


async def test_probe_setpoint_change_during_cook_is_applied(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=74)]})
    assert coordinator.live_or_staged_probe_setpoint(0) == 74
    assert coordinator.live_or_staged_probe_enabled(0) is True
    await coordinator.async_set_probe(0, setpoint=80)
    assert sent(coordinator)["probe_0"] == {"mode": "manual", "setpoint": 80}


async def test_probe_change_while_idle_only_stages(coordinator):
    coordinator.data = mk_state()
    await coordinator.async_set_probe(1, enabled=True, setpoint=65)
    assert coordinator.client.start_cook.await_count == 0
    assert coordinator.live_or_staged_probe_enabled(1) is True
    assert coordinator.live_or_staged_probe_setpoint(1) == 65


# ---------------------------------------------------------------- polling


async def test_active_polling_interval_is_three_seconds(coordinator):
    assert SCAN_INTERVAL_ACTIVE == timedelta(seconds=3)
    coordinator.client.get_combined_state.return_value = mk_state(cooking())
    await coordinator._async_update_data()
    assert coordinator.update_interval == SCAN_INTERVAL_ACTIVE


async def test_stale_active_state_polls_slowly(coordinator):
    state = mk_state(cooking())
    state.last_updated_at = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    coordinator.client.get_combined_state.return_value = state
    await coordinator._async_update_data()
    assert coordinator.update_interval == SCAN_INTERVAL_IDLE


# ---------------------------------------------------------------- events


async def _feed(hass, coordinator, *states):
    for s in states:
        coordinator._emit_lifecycle_events(s)
    await hass.async_block_till_done()


async def test_preheat_complete_fires_once(hass, coordinator, events):
    base = dict(left=1800, total=1800)
    await _feed(
        hass, coordinator,
        mk_state(),
        mk_state(cooking(state="preheat", **base), {"state": {"state": "preheat"}}),
        mk_state(cooking(state="lid open", **base), {"state": {"state": "preheat"}}),
        mk_state(cooking(state="preheat", **base), {"state": {"state": "preheat"}}),
        mk_state(cooking(state="cooking", **base), {"state": {"state": "preheat"}}),
        mk_state(cooking(state="cooking", **base), {"state": {"state": "cooking"}}),
    )
    assert events.count(EVENT_PREHEAT_COMPLETE) == 1
    assert events.count(EVENT_COOK_STARTED) == 1


async def test_unknown_state_does_not_refire(hass, coordinator, events):
    from custom_components.ninja_woodfire._lib.models import CombinedState

    g = cooking(left=600, total=1800)
    await _feed(hass, coordinator, mk_state(), mk_state(g), CombinedState(dsn="X"), mk_state(g))
    assert events.count(EVENT_COOK_STARTED) == 1
    assert events.count(EVENT_COOK_HALFTIME) == 1


async def test_first_poll_after_restart_fires_nothing(hass, coordinator, events):
    await _feed(hass, coordinator, mk_state({"state": "done"}))
    coordinator._prev_grill_state = None
    mid_cook = mk_state(cooking(left=100, total=1800), probes={"probes": [probe(temp=80, setpoint=74)]})
    await _feed(hass, coordinator, mid_cook, mid_cook)
    assert events == []


async def test_power_off_after_cook_fires_done_once(hass, coordinator, events):
    off = mk_state({"state": "powered OFF"})
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()), off, off)
    assert events.count(EVENT_COOK_DONE) == 1


async def test_probe_halfway_measured_from_start_temp(hass, coordinator, events):
    def st(temp):
        return mk_state(cooking(), probes={"probes": [probe(temp=temp, setpoint=74)]})

    await _feed(hass, coordinator, mk_state(), st(10), st(38))
    assert EVENT_PROBE_HALFWAY not in events
    await _feed(hass, coordinator, st(42))
    assert events.count(EVENT_PROBE_HALFWAY) == 1
    await _feed(hass, coordinator, st(74))
    assert events.count(EVENT_PROBE_TARGET_REACHED) == 1


# ---------------------------------------------------------------- review follow-ups


async def test_modify_during_rest_is_rejected(coordinator):
    coordinator.data = mk_state(cooking(state="rest", left=0, endtimeutc=int(time.time()) - 60))
    with pytest.raises(HomeAssistantError):
        await coordinator.async_modify_cook(smoke=True)
    assert coordinator.client.start_cook.await_count == 0


async def test_duration_below_elapsed_is_rejected(coordinator):
    coordinator.data = mk_state(cooking(left=600, total=1800))
    with pytest.raises(HomeAssistantError):
        await coordinator.async_modify_cook(seconds=10 * 60)
    assert coordinator.client.start_cook.await_count == 0


async def test_unlisted_mode_never_sends_negative_seconds(coordinator):
    coordinator.data = mk_state(cooking(mode="broil", left=30, total=1500))
    await coordinator.async_modify_cook(smoke=True)
    assert sent(coordinator)["seconds"] >= 60


async def test_editing_inactive_probe_target_does_not_reissue(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(active=0)]})
    await coordinator.async_set_probe(0, setpoint=80)
    assert coordinator.client.start_cook.await_count == 0
    assert coordinator.cook_setting_probe_setpoint[0] == 80


async def test_stale_poll_after_reissue_keeps_issued_level(coordinator):
    coordinator.data = mk_state(cooking(mode="grill", setpoint=260))
    await coordinator.async_modify_cook(temp=1)
    for sp in (260, 190):
        coordinator.client.get_combined_state.return_value = mk_state(cooking(mode="grill", setpoint=sp))
        coordinator.data = await coordinator._async_update_data()
    assert coordinator.live_or_staged_temp == 1


async def test_cloud_error_on_command_is_home_assistant_error(coordinator):
    from custom_components.ninja_woodfire._lib.api.ayla import TransportError

    coordinator.client.stop_cook.side_effect = TransportError("down")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_stop_cook()


async def test_lingering_cook_state_does_not_count_as_active(coordinator):
    coordinator.data = mk_state({"state": "idle"}, {"state": {"state": "rest"}})
    assert coordinator.is_cook_active is False


async def test_probe_events_rearm_when_target_changes(hass, coordinator, events):
    def st(temp, target):
        return mk_state(cooking(), probes={"probes": [probe(temp=temp, setpoint=target)]})

    await _feed(hass, coordinator, mk_state(), st(20, 60), st(60, 60), st(60, 80), st(80, 80))
    assert events.count(EVENT_PROBE_TARGET_REACHED) == 2


async def test_preheat_without_running_timer_uses_full_duration(coordinator):
    g = cooking(state="preheat", left=0, total=1800)
    del g["endtimeutc"]
    coordinator.data = mk_state(g)
    await coordinator.async_skip_preheat()
    assert sent(coordinator)["seconds"] == 1800
