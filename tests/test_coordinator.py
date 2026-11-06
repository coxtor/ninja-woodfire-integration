import time
from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.ninja_woodfire._lib.probe_presets import PROBE_FOOD_MANUAL
from custom_components.ninja_woodfire.const import (
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PELLET_REFILL_REMINDER,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROBE_ALARM,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_RESTING_STARTED,
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


# --------------------------------------------------------- probe food presets


async def test_selecting_a_food_preset_sends_its_default_temp(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Beef")
    assert coordinator.live_or_staged_probe_food(0) == "Beef"
    assert sent(coordinator)["probe_0"] == {"mode": "manual", "setpoint": 42}  # Rare 1


async def test_changing_doneness_resends_the_new_level_temp(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Beef")
    await coordinator.async_set_probe_doneness(0, 2)  # "M. Rare 3"
    assert coordinator.live_or_staged_probe_doneness_index(0) == 2
    assert sent(coordinator)["probe_0"] == {"mode": "manual", "setpoint": 47}


async def test_switching_food_resets_the_doneness_index_to_the_first_level(coordinator):
    # Bug reported live: picking a new food preset without also touching
    # doneness left the previous food's doneness index in place (e.g. index 2
    # carried over to a food where that means something else, or the
    # doneness select visually looked "stuck"). A new food should default to
    # its own first level, not whatever was last picked for a different one.
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Beef")
    await coordinator.async_set_probe_doneness(0, 2)  # "M. Rare 3"
    assert coordinator.live_or_staged_probe_doneness_index(0) == 2

    await coordinator.async_set_probe_food(0, "Lamb")
    assert coordinator.live_or_staged_probe_doneness_index(0) == 0


async def test_manual_food_does_not_touch_the_setpoint(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Manual")
    assert coordinator.live_or_staged_probe_food(0) == "Manual"
    assert coordinator.client.start_cook.await_count == 0


async def test_food_preset_while_idle_only_stages(coordinator):
    coordinator.data = mk_state()
    await coordinator.async_set_probe_food(1, "Lamb")
    assert coordinator.client.start_cook.await_count == 0
    assert coordinator.live_or_staged_probe_food(1) == "Lamb"
    assert coordinator.live_or_staged_probe_setpoint(1) == 55  # Lamb's "Rare" default


async def test_doneness_index_past_the_presets_range_clamps(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Chicken")  # only 1 level
    await coordinator.async_set_probe_doneness(0, 5)
    assert sent(coordinator)["probe_0"] == {"mode": "manual", "setpoint": 74}


async def test_selecting_a_food_preset_also_turns_the_probe_on(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(active=0, setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Beef")
    assert coordinator.live_or_staged_probe_enabled(0) is True
    assert sent(coordinator)["probe_0"]["setpoint"] == 42


async def test_choosing_a_doneness_also_turns_the_probe_on(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(active=0, setpoint=60)]})
    coordinator.cook_setting_probe_food[0] = "Beef"
    await coordinator.async_set_probe_doneness(0, 1)
    assert coordinator.live_or_staged_probe_enabled(0) is True


async def test_hand_typing_a_setpoint_resets_the_food_select_to_manual(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=60)]})
    await coordinator.async_set_probe_food(0, "Beef")
    await coordinator.async_set_probe(0, setpoint=70)  # as number.py calls it
    assert coordinator.live_or_staged_probe_food(0) == PROBE_FOOD_MANUAL
    assert coordinator.live_or_staged_probe_doneness_index(0) == 0


async def test_turning_the_probe_off_resets_the_food_select_too(coordinator):
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=42)]})
    await coordinator.async_set_probe_food(0, "Beef")
    await coordinator.async_set_probe(0, enabled=False)  # as the toggle calls it
    assert coordinator.live_or_staged_probe_food(0) == PROBE_FOOD_MANUAL


async def test_turning_the_probe_back_on_does_not_disturb_its_preset(coordinator):
    # The toggle re-enabling a probe backfills its live setpoint internally
    # (see async_set_probe); that must not look like a hand-typed change.
    coordinator.data = mk_state(cooking(), probes={"probes": [probe(setpoint=42)]})
    await coordinator.async_set_probe_food(0, "Beef")
    await coordinator.async_set_probe(0, enabled=True)  # as the toggle calls it
    assert coordinator.live_or_staged_probe_food(0) == "Beef"


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


async def _feed_with_payload(hass, coordinator, event_type, *states, tick_s=0, freezer=None):
    """Like _feed, but returns the last event's data dict instead of just counting."""
    payloads = []
    hass.bus.async_listen(event_type, lambda e: payloads.append(e.data))
    for i, s in enumerate(states):
        if freezer is not None and i > 0:
            freezer.tick(tick_s)
        coordinator._emit_lifecycle_events(s)
    await hass.async_block_till_done()
    return payloads


async def test_preheat_complete_flags_a_hardware_skip_as_skipped(hass, coordinator, freezer):
    # Live data 2026-11-06: a genuine preheat/ignition ran 262-590s; a skip via
    # the grill's own Start-button-held gesture ran 2-10s. is_prehaet_skipped
    # only reflects an app/HA-issued skip, not this hardware one - so the
    # coordinator times the phase itself instead of trusting that flag.
    base = dict(left=1800, total=1800)
    payloads = await _feed_with_payload(
        hass, coordinator, EVENT_PREHEAT_COMPLETE,
        mk_state(),
        mk_state(cooking(state="preheat", **base), {"state": {"state": "preheat"}}),
        mk_state(cooking(state="cooking", **base), {"state": {"state": "cooking"}}),
        tick_s=5,  # a 5 s preheat is only possible if it was skipped
        freezer=freezer,
    )
    assert payloads[-1]["skipped"] is True


async def test_preheat_complete_is_not_flagged_skipped_when_it_ran_its_course(
    hass, coordinator, freezer
):
    base = dict(left=1800, total=1800)
    payloads = await _feed_with_payload(
        hass, coordinator, EVENT_PREHEAT_COMPLETE,
        mk_state(),
        mk_state(cooking(state="preheat", **base), {"state": {"state": "preheat"}}),
        mk_state(cooking(state="cooking", **base), {"state": {"state": "cooking"}}),
        tick_s=300,  # a real preheat observed live: 262-590 s
        freezer=freezer,
    )
    assert payloads[-1]["skipped"] is False


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


async def test_probe_resting_started_fires_once_entering_the_rest_window(hass, coordinator, events):
    # The Rest window (see coordinator._REST_TOTAL_SECONDS) is the one
    # sub-state of a probe-driven cook with a real countdown - without a
    # dedicated event, those seconds_left/seconds_set never reach a
    # notification (by cook_done they're already cleared).
    def st(probe_state, left=140, total=300):
        return mk_state(
            cooking(left=left, total=total),
            probes={"probes": [probe(temp=75, setpoint=75, state=probe_state)]},
        )

    await _feed(hass, coordinator, mk_state(), st("cooking"))
    assert EVENT_PROBE_RESTING_STARTED not in events

    await _feed(hass, coordinator, st("resting", left=140))
    assert events.count(EVENT_PROBE_RESTING_STARTED) == 1

    # Still resting next poll - must not re-fire.
    await _feed(hass, coordinator, st("resting", left=100))
    assert events.count(EVENT_PROBE_RESTING_STARTED) == 1


async def test_probe_resting_started_carries_the_countdown(hass, coordinator):
    state = mk_state(
        cooking(left=140, total=300),
        probes={"probes": [probe(temp=75, setpoint=75, state="resting")]},
    )
    payloads = await _feed_with_payload(
        hass, coordinator, EVENT_PROBE_RESTING_STARTED, mk_state(), state,
    )
    data = payloads[-1]
    assert data["seconds_left"] == 140
    assert data["seconds_set"] == 300


async def test_probe_resting_started_fires_again_for_a_later_episode(hass, coordinator, events):
    def st(probe_state):
        return mk_state(
            cooking(left=140, total=300),
            probes={"probes": [probe(temp=75, setpoint=75, state=probe_state)]},
        )

    await _feed(hass, coordinator, mk_state(), st("resting"))
    assert events.count(EVENT_PROBE_RESTING_STARTED) == 1

    await _feed(hass, coordinator, st("done"), st("resting"))
    assert events.count(EVENT_PROBE_RESTING_STARTED) == 2


# ---------------------------------------------------------------- status snapshot
# A consistent status snapshot (grill temp/state/phase, cook timer, both
# probes) is attached to every lifecycle event, so a notification (e.g.
# Node-RED) can show the full picture without separately polling entities.


async def test_cook_started_carries_a_full_status_snapshot(hass, coordinator):
    # temps.grill arrives in °F on the wire (from_wire converts it) - 356°F == 180°C.
    state = mk_state(
        cooking(left=900, total=1800, inputs={"temps": {"grill": 356}}),
        {"state": {"state": "cooking"}},
        probes={"probes": [probe(temp=40, setpoint=74), probe(temp=22, setpoint=0, active=0, name="probe1")]},
    )
    payloads = await _feed_with_payload(hass, coordinator, EVENT_COOK_STARTED, mk_state(), state)
    data = payloads[-1]
    assert data["grill_state"] == "cooking"
    assert data["phase"] == "cooking"
    assert data["grill_temp"] == 180
    assert data["seconds_left"] == 900
    assert data["seconds_set"] == 1800
    assert data["probe0_temp"] == 40
    assert data["probe0_target"] == 74
    assert data["probe1_temp"] == 22
    assert data["probe1_target"] == 0  # inactive probe - fixture's own mode.setpoint


async def test_probe_target_reached_carries_the_status_snapshot_too(hass, coordinator):
    def st(temp):
        return mk_state(
            cooking(inputs={"temps": {"grill": 392}}),  # 392F == 200C
            probes={"probes": [probe(temp=temp, setpoint=74)]},
        )

    payloads = await _feed_with_payload(
        hass, coordinator, EVENT_PROBE_TARGET_REACHED, mk_state(), st(40), st(74),
    )
    data = payloads[-1]
    assert data["grill_temp"] == 200
    assert data["grill_state"] == "cooking"


async def test_probe_alarm_carries_the_status_snapshot_too(hass, coordinator, events):
    coordinator.probe_alarm_temp[0] = 50
    coordinator.probe_alarm_enabled[0] = True
    state = mk_state(
        cooking(inputs={"temps": {"grill": 410}}),  # 410F == 210C
        probes={"probes": [probe(temp=55, active=0)]},
    )
    payloads = await _feed_with_payload(hass, coordinator, EVENT_PROBE_ALARM, mk_state(), state)
    data = payloads[-1]
    assert data["grill_temp"] == 210
    assert data["probe0_temp"] == 55


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


# ---------------------------------------------------------- probe temperature alarm


async def test_probe_alarm_fires_independent_of_the_target_mechanism(hass, coordinator, events):
    # active=0 (not a target probe), but plugged in - the alarm must not
    # require probe.active, which only reflects the target/enabled
    # mechanism this is deliberately independent of.
    def st(temp):
        return mk_state(cooking(), probes={"probes": [probe(temp=temp, active=0)]})

    coordinator.probe_alarm_enabled[0] = True
    coordinator.probe_alarm_temp[0] = 60
    await _feed(hass, coordinator, mk_state(), st(40), st(55))
    assert EVENT_PROBE_ALARM not in events
    await _feed(hass, coordinator, st(61))
    assert events.count(EVENT_PROBE_ALARM) == 1


async def test_probe_alarm_does_not_fire_while_disabled(hass, coordinator, events):
    def st(temp):
        return mk_state(cooking(), probes={"probes": [probe(temp=temp, active=0)]})

    coordinator.probe_alarm_temp[0] = 60
    await _feed(hass, coordinator, mk_state(), st(40), st(70))
    assert EVENT_PROBE_ALARM not in events


async def test_probe_alarm_rearms_after_dropping_back_below(hass, coordinator, events):
    def st(temp):
        return mk_state(cooking(), probes={"probes": [probe(temp=temp, active=0)]})

    coordinator.probe_alarm_enabled[0] = True
    coordinator.probe_alarm_temp[0] = 60
    await _feed(hass, coordinator, mk_state(), st(40), st(65), st(50), st(65))
    assert events.count(EVENT_PROBE_ALARM) == 2


async def test_pellet_reminder_fires_after_the_configured_interval_while_smoking(
    hass, coordinator, events
):
    coordinator.cook_setting_smoke = True
    coordinator.pellet_reminder_interval_min = 10
    coordinator.pellet_reminder_count = 3
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))
    assert EVENT_PELLET_REFILL_REMINDER not in events

    coordinator._pellet_reminder_anchor_m -= 10 * 60
    await _feed(hass, coordinator, mk_state(cooking()))
    assert events.count(EVENT_PELLET_REFILL_REMINDER) == 1


async def test_pellet_reminder_does_not_fire_without_smoke(hass, coordinator, events):
    coordinator.cook_setting_smoke = False
    coordinator.pellet_reminder_interval_min = 10
    coordinator.pellet_reminder_count = 3
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))
    coordinator._pellet_reminder_anchor_m -= 30 * 60
    await _feed(hass, coordinator, mk_state(cooking()))
    assert EVENT_PELLET_REFILL_REMINDER not in events


async def test_pellet_reminder_disabled_by_default_zero_count(hass, coordinator, events):
    coordinator.cook_setting_smoke = True
    coordinator.pellet_reminder_interval_min = 10
    # pellet_reminder_count left at its default (0 = disabled).
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))
    coordinator._pellet_reminder_anchor_m -= 30 * 60
    await _feed(hass, coordinator, mk_state(cooking()))
    assert EVENT_PELLET_REFILL_REMINDER not in events


async def test_pellet_reminder_stops_after_the_configured_count(hass, coordinator, events):
    coordinator.cook_setting_smoke = True
    coordinator.pellet_reminder_interval_min = 10
    coordinator.pellet_reminder_count = 2
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))

    # Jump far enough that, without the cap, several intervals would have
    # elapsed - only 2 reminders may ever fire for this cook.
    coordinator._pellet_reminder_anchor_m -= 60 * 60
    await _feed(hass, coordinator, mk_state(cooking()))
    assert events.count(EVENT_PELLET_REFILL_REMINDER) == 2


async def test_pellet_reminder_resets_for_a_new_cook(hass, coordinator, events):
    coordinator.cook_setting_smoke = True
    coordinator.pellet_reminder_interval_min = 10
    coordinator.pellet_reminder_count = 1
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))
    coordinator._pellet_reminder_anchor_m -= 10 * 60
    await _feed(hass, coordinator, mk_state(cooking()))
    assert events.count(EVENT_PELLET_REFILL_REMINDER) == 1

    # Cook ends, a fresh one starts - the reminder budget must be fresh too.
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))
    coordinator._pellet_reminder_anchor_m -= 10 * 60
    await _feed(hass, coordinator, mk_state(cooking()))
    assert events.count(EVENT_PELLET_REFILL_REMINDER) == 2


async def test_pellet_reminder_carries_its_own_payload(hass, coordinator):
    coordinator.cook_setting_smoke = True
    coordinator.pellet_reminder_interval_min = 15
    coordinator.pellet_reminder_count = 5
    await _feed(hass, coordinator, mk_state(), mk_state(cooking()))
    coordinator._pellet_reminder_anchor_m -= 15 * 60
    payloads = await _feed_with_payload(
        hass, coordinator, EVENT_PELLET_REFILL_REMINDER, mk_state(cooking()),
    )
    data = payloads[-1]
    assert data["reminder_number"] == 1
    assert data["reminder_count"] == 5
    assert data["interval_min"] == 15


async def test_preheat_without_running_timer_uses_full_duration(coordinator):
    g = cooking(state="preheat", left=0, total=1800)
    del g["endtimeutc"]
    coordinator.data = mk_state(g)
    await coordinator.async_skip_preheat()
    assert sent(coordinator)["seconds"] == 1800
