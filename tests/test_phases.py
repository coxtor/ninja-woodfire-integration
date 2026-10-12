import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.ninja_woodfire.const import (
    EVENT_COOK_DONE,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROMPT,
)
from custom_components.ninja_woodfire.sensor import SENSORS, NinjaSensor, _cook_phase, _preheat_progress

from .conftest import cooking, mk_state

PREHEAT_DONE = {"state": {"state": "preheat", "progress": 100}}


def preheat(progress=60, **kw):
    return mk_state(cooking(state="cooking", left=1800, total=1800, **kw), {"state": {"state": "preheat", "progress": progress}})


def waiting_for_food(**kw):
    """Preheat finished; the firmware asks for food and keeps CookState at preheat/100."""
    return mk_state(cooking(state="cooking", message="1:addfood", eventmask="0x02", **kw), PREHEAT_DONE)


def test_prompt_is_parsed_from_message():
    assert waiting_for_food().grill.prompt == "addfood"
    assert mk_state(cooking(message="7:getfood")).grill.prompt == "getfood"
    assert mk_state(cooking(message="")).grill.prompt is None


def test_preheat_is_running_while_progress_below_100():
    assert preheat().preheating is True
    assert _cook_phase(preheat()) == "preheat"


@pytest.mark.parametrize(
    "state",
    [
        waiting_for_food(),
        mk_state(cooking(state="cooking", left=1800, total=1800), PREHEAT_DONE),
        mk_state(cooking(state="cooking", left=900, total=1800), {"state": {"state": "preheat", "progress": 80}}),
    ],
    ids=["addfood", "progress-100", "timer-running"],
)
def test_preheat_ends_although_cook_state_still_says_preheat(state):
    assert state.preheating is False
    assert _cook_phase(state) == "cooking"
    assert _preheat_progress(state) is None


async def test_cook_progress_counts_while_waiting_for_food(coordinator):
    coordinator.data = waiting_for_food(left=900, total=1800)
    sensor = NinjaSensor(coordinator, next(d for d in SENSORS if d.key == "cook_progress"))
    assert sensor.native_value == 50
    assert sensor.extra_state_attributes["phase"] == "cooking"


async def test_waiting_for_food_fires_preheat_complete_once(hass, coordinator, events):
    for s in (mk_state(), preheat(30), preheat(90), waiting_for_food(), waiting_for_food()):
        coordinator._emit_lifecycle_events(s)
    await hass.async_block_till_done()
    assert events.count(EVENT_PREHEAT_COMPLETE) == 1


async def test_change_while_waiting_for_food_does_not_restart_preheat(coordinator):
    coordinator.data = waiting_for_food(left=1700, total=1800)
    await coordinator.async_modify_cook(smoke=True)
    assert coordinator.client.start_cook.call_args.kwargs["skip_preheat"] is True


async def test_skip_preheat_refused_while_waiting_for_food(coordinator):
    coordinator.data = waiting_for_food()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_skip_preheat()


async def test_prompt_event_fires_once_per_prompt(hass, coordinator):
    seen = []
    hass.bus.async_listen(EVENT_PROMPT, lambda e: seen.append(e.data["prompt"]))
    states = [
        mk_state(),
        preheat(50),
        waiting_for_food(),
        waiting_for_food(),
        mk_state(cooking(message="")),
        mk_state(cooking(message="4:flipfood")),
        mk_state(cooking(message="4:flipfood")),
        mk_state(cooking(message="")),
        mk_state(cooking(message="4:flipfood")),
    ]
    for s in states:
        coordinator._emit_lifecycle_events(s)
    await hass.async_block_till_done()
    assert seen == ["addfood", "flipfood", "flipfood"]


async def test_prompt_present_at_startup_is_not_fired(hass, coordinator):
    seen = []
    hass.bus.async_listen(EVENT_PROMPT, lambda e: seen.append(e))
    coordinator._emit_lifecycle_events(waiting_for_food())
    coordinator._emit_lifecycle_events(waiting_for_food())
    await hass.async_block_till_done()
    assert seen == []


async def test_prompt_sensor(coordinator):
    sensor = NinjaSensor(coordinator, next(d for d in SENSORS if d.key == "prompt"))
    coordinator.data = waiting_for_food()
    assert sensor.native_value == "addfood"
    coordinator.data = mk_state(cooking(message=""))
    assert sensor.native_value is None


def test_waiting_for_lid_is_not_an_active_cook():
    from custom_components.ninja_woodfire.coordinator import is_active

    state = mk_state({"state": "wait lid closed before cook", "mode": "grill"})
    assert is_active(state) is False
    assert _cook_phase(state) == "none"


async def test_stop_on_device_with_open_lid_fires_cook_done(hass, coordinator, events):
    for s in (mk_state(), mk_state(cooking()), mk_state({"state": "wait lid closed before cook"})):
        coordinator._emit_lifecycle_events(s)
    await hass.async_block_till_done()
    assert events.count(EVENT_COOK_DONE) == 1


async def test_restart_from_home_assistant_is_a_new_cook(hass, coordinator, events):
    coordinator.data = mk_state(cooking())
    for s in (mk_state(), mk_state(cooking())):
        coordinator._emit_lifecycle_events(s)
    await coordinator.async_stop_cook()
    await coordinator.async_start_cook()
    coordinator._emit_lifecycle_events(mk_state(cooking(left=600, total=600)))
    await hass.async_block_till_done()
    assert events.count(EVENT_COOK_STARTED) == 2


async def test_reissue_is_not_a_new_cook(hass, coordinator, events):
    for s in (mk_state(), mk_state(cooking())):
        coordinator._emit_lifecycle_events(s)
    coordinator.data = mk_state(cooking())
    await coordinator.async_modify_cook(smoke=True)
    coordinator._emit_lifecycle_events(mk_state(cooking()))
    await hass.async_block_till_done()
    assert events.count(EVENT_COOK_STARTED) == 1
