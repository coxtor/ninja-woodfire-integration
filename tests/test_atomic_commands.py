"""I-02: concurrent / rapid setting changes must not overwrite each other.

Every command re-sends the *whole* cook, so a change applied on top of a stale
snapshot silently reverts whatever another call just set. These tests drive two
commands into the coordinator at once (deterministic asyncio barrier) and check
the last payload carries both changes.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator

from .conftest import DSN, cooking, mk_state, probe


def _cooking_coordinator(hass, entry, grill=None, probes=None):
    from unittest.mock import AsyncMock

    ble = AsyncMock()
    state = mk_state(grill or cooking(setpoint=180, left=1800, total=1800), probes=probes)
    state.online = True
    ble.get_combined_state.return_value = state
    c = NinjaWoodfireCoordinator(hass, entry, None, DSN, WOODFIRE_PRO_XL, 0, ble=ble)
    c._burst_refresh = AsyncMock()
    return c, ble


def _gated(ble):
    """Hold the first start_cook until released; record every payload."""
    calls: list[dict] = []
    gate = asyncio.Event()

    async def start_cook(dsn, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await gate.wait()

    ble.start_cook.side_effect = start_cook
    return calls, gate


async def _race(gate, first, second):
    t1 = asyncio.create_task(first)
    for _ in range(5):
        await asyncio.sleep(0)
    t2 = asyncio.create_task(second)
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_parallel_temp_and_duration_changes_keep_both(hass, entry):
    c, ble = _cooking_coordinator(hass, entry)
    await c.async_refresh()
    calls, gate = _gated(ble)

    await _race(gate, c.async_modify_cook(temp=200), c.async_modify_cook(seconds=2400))

    assert len(calls) == 2
    assert calls[-1]["temp"] == 200
    assert calls[-1]["seconds"] >= 2390       # the new total, not the old 1800


@pytest.mark.asyncio
async def test_parallel_probe_enable_and_target_keep_both(hass, entry):
    c, ble = _cooking_coordinator(hass, entry)
    await c.async_refresh()
    calls, gate = _gated(ble)

    await _race(
        gate,
        c.async_set_probe(0, enabled=True),
        c.async_set_probe(0, setpoint=70),
    )

    assert calls[-1]["probe_0"] == {"mode": "manual", "setpoint": 70}


@pytest.mark.asyncio
async def test_parallel_changes_to_both_probes_keep_both(hass, entry):
    c, ble = _cooking_coordinator(hass, entry)
    await c.async_refresh()
    c.cook_setting_probe_setpoint = [65, 75]
    calls, gate = _gated(ble)

    await _race(
        gate,
        c.async_set_probe(0, enabled=True),
        c.async_set_probe(1, enabled=True),
    )

    assert calls[-1]["probe_0"] == {"mode": "manual", "setpoint": 65}
    assert calls[-1]["probe_1"] == {"mode": "manual", "setpoint": 75}


@pytest.mark.asyncio
async def test_rapid_sequential_changes_before_the_next_poll_keep_both(hass, entry):
    # No concurrency at all: two awaited calls back to back, with no poll in
    # between to refresh the snapshot. The second must build on the first.
    c, ble = _cooking_coordinator(hass, entry)
    await c.async_refresh()
    calls: list[dict] = []
    ble.start_cook.side_effect = lambda dsn, **kw: calls.append(kw)

    await c.async_modify_cook(temp=200)
    await c.async_modify_cook(seconds=2400)

    assert calls[-1]["temp"] == 200
    assert calls[-1]["seconds"] >= 2390


@pytest.mark.asyncio
async def test_stop_overrides_a_start_still_in_flight(hass, entry):
    c, ble = _cooking_coordinator(hass, entry, grill={"state": "idle"})
    await c.async_refresh()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_start(dsn, **kwargs):
        started.set()
        await release.wait()

    ble.start_cook.side_effect = slow_start
    start = asyncio.create_task(c.async_start_cook())
    await started.wait()

    await c.async_stop_cook()
    ble.stop_cook.assert_awaited_once()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    # ...and no further start was issued after the stop.
    assert ble.start_cook.await_count == 1


@pytest.mark.asyncio
async def test_configure_cook_applies_everything_in_one_command(hass, entry):
    c, ble = _cooking_coordinator(
        hass, entry, probes={"probes": [probe(active=0), probe(name="probe1", active=0)]}
    )
    await c.async_refresh()
    calls: list[dict] = []
    ble.start_cook.side_effect = lambda dsn, **kw: calls.append(kw)

    await c.async_configure_cook(
        temp=200,
        seconds=2400,
        probes={0: {"enabled": True, "setpoint": 70}},
    )

    assert len(calls) == 1                    # one command, not three
    assert calls[0]["temp"] == 200
    assert calls[0]["seconds"] >= 2390
    assert calls[0]["probe_0"] == {"mode": "manual", "setpoint": 70}


@pytest.mark.asyncio
async def test_configure_cook_can_stage_and_start_in_one_go(hass, entry):
    c, ble = _cooking_coordinator(hass, entry, grill={"state": "idle"})
    await c.async_refresh()

    await c.async_configure_cook(mode="bake", temp=180, seconds=1200, start=True)

    kwargs = ble.start_cook.call_args.kwargs
    assert (kwargs["mode"], kwargs["temp"], kwargs["seconds"]) == ("bake", 180, 1200)


# ------------------------------------------------- V-02: Wake uses the command lock


@pytest.mark.asyncio
async def test_wake_never_overtakes_a_start_in_flight(hass, entry):
    from unittest.mock import AsyncMock

    c, ble = _cooking_coordinator(hass, entry, grill={"state": "idle"})
    await c.async_refresh()
    c.async_request_refresh = AsyncMock()
    order: list[str] = []
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_start(dsn, **kwargs):
        order.append("start_begin")
        started.set()
        await release.wait()
        order.append("start_finish")

    ble.start_cook.side_effect = slow_start
    ble.stop_cook.side_effect = lambda dsn: order.append("stop")

    start = asyncio.create_task(c.async_start_cook())
    await started.wait()
    wake = asyncio.create_task(c.async_wake())
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(start, wake)

    assert order == ["start_begin", "start_finish"]
    ble.stop_cook.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", ""])
async def test_wake_does_not_treat_an_unknown_state_as_standby(hass, entry, state):
    from unittest.mock import AsyncMock

    c, ble = _cooking_coordinator(hass, entry)
    await c.async_refresh()
    # No reading at all (e.g. a beacon phase we don't model) — not a confirmed
    # standby, so Wake must not send a Stop into it.
    c.data.grill.state = state
    c.async_request_refresh = AsyncMock()

    await c.async_wake()

    ble.stop_cook.assert_not_called()


@pytest.mark.asyncio
async def test_wake_without_any_state_sends_nothing(hass, entry):
    from unittest.mock import AsyncMock

    c, ble = _cooking_coordinator(hass, entry)
    c.async_request_refresh = AsyncMock()

    await c.async_wake()                      # never polled: data is None

    ble.stop_cook.assert_not_called()


@pytest.mark.asyncio
async def test_a_powered_off_grill_is_still_woken(hass, entry):
    from unittest.mock import AsyncMock

    c, ble = _cooking_coordinator(hass, entry, grill={"state": "powered_off"})
    await c.async_refresh()
    c.async_request_refresh = AsyncMock()

    await c.async_wake()

    ble.stop_cook.assert_awaited_once()


# ------------------------------------------------- V-03/V-04/V-05: the atomic service

PREHEAT = {"state": "preheat", "mode": "roast", "setpoint": 180,
           "seconds set": 600, "seconds left": 600}
COOKING = {"state": "cooking", "mode": "roast", "setpoint": 180,
           "seconds set": 1800, "seconds left": 1800}


def _cloud_coordinator(hass, entry, grill=None, probes=None):
    """A cook running on the cloud path, where the grill reports its probes."""
    from unittest.mock import AsyncMock

    client = AsyncMock()
    state = mk_state(grill or cooking(setpoint=180, left=1800, total=1800), probes=probes)
    state.online = True
    client.get_combined_state.return_value = state
    c = NinjaWoodfireCoordinator(hass, entry, client, DSN, WOODFIRE_PRO_XL, 0)
    c._burst_refresh = AsyncMock()
    return c, client


def _polled(grill):
    """A fresh online poll result."""
    state = mk_state(grill)
    state.online = True
    return state


def _recorded(ble):
    calls: list[dict] = []
    ble.start_cook.side_effect = lambda dsn, **kw: calls.append(kw)
    return calls


@pytest.mark.asyncio
async def test_a_probe_target_change_keeps_the_running_probe_enabled(hass, entry):
    # Cook started on the grill itself: staging still says "probe off, 60°C".
    c, ble = _cloud_coordinator(
        hass, entry, probes={"probes": [probe(setpoint=70), probe(name="probe1", setpoint=80)]}
    )
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(probes={0: {"setpoint": 75}})

    assert calls[-1]["probe_0"] == {"mode": "manual", "setpoint": 75}
    assert calls[-1]["probe_1"] == {"mode": "manual", "setpoint": 80}


@pytest.mark.asyncio
async def test_enabling_a_probe_keeps_its_running_target(hass, entry):
    c, ble = _cloud_coordinator(
        hass, entry, probes={"probes": [probe(setpoint=70), probe(name="probe1", active=0)]}
    )
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(probes={0: {"enabled": True}})

    assert calls[-1]["probe_0"] == {"mode": "manual", "setpoint": 70}


@pytest.mark.asyncio
async def test_a_partial_probe_change_merges_with_a_pending_command(hass, entry):
    c, ble = _cloud_coordinator(
        hass, entry, probes={"probes": [probe(active=0), probe(name="probe1", active=0)]}
    )
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(probes={0: {"enabled": True, "setpoint": 68}})
    await c.async_configure_cook(probes={0: {"setpoint": 72}})

    assert calls[-1]["probe_0"] == {"mode": "manual", "setpoint": 72}


@pytest.mark.asyncio
async def test_a_probe_can_still_be_switched_off(hass, entry):
    c, ble = _cloud_coordinator(hass, entry, probes={"probes": [probe(setpoint=70)]})
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(probes={0: {"enabled": False}})

    assert calls[-1]["probe_0"] is None


@pytest.mark.asyncio
async def test_an_explicit_skip_preheat_is_honoured_during_preheat(hass, entry):
    c, ble = _cooking_coordinator(hass, entry, grill=PREHEAT)
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(temp=200, skip_preheat=True)

    assert len(calls) == 1
    assert calls[0]["skip_preheat"] is True
    assert calls[0]["temp"] == 200


@pytest.mark.asyncio
async def test_without_the_field_preheat_is_not_skipped(hass, entry):
    c, ble = _cooking_coordinator(hass, entry, grill=PREHEAT)
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(temp=200)

    assert calls[0]["skip_preheat"] is False


@pytest.mark.asyncio
async def test_pending_time_does_not_run_down_during_preheat(hass, entry):
    c, ble = _cooking_coordinator(hass, entry, grill=PREHEAT)
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(temp=200)
    assert calls[-1]["seconds"] == 600
    c._issued_at -= 20                        # 20 s later, still preheating
    await c.async_configure_cook(temp=210)

    assert calls[-1]["seconds"] == 600


@pytest.mark.asyncio
async def test_a_polled_phase_change_updates_the_pending_timer(hass, entry):
    # R-02: preheat ended while the command was still pending; the cook time
    # the grill now reports is what the next change has to build on.
    c, client = _cloud_coordinator(hass, entry, grill=PREHEAT)
    await c.async_refresh()
    calls = _recorded(client)

    await c.async_configure_cook(temp=200)
    assert calls[-1]["seconds"] == 600
    c._issued_at -= 20
    client.get_combined_state.return_value = _polled(
        {"state": "cooking", "mode": "roast", "setpoint": 200,
         "seconds set": 600, "seconds left": 590}
    )
    await c.async_refresh()

    await c.async_configure_cook(temp=210)

    assert calls[-1]["seconds"] == 590


@pytest.mark.asyncio
async def test_a_polled_pause_stops_the_pending_timer(hass, entry):
    # The cook paused shortly after the command: only the seconds that really
    # ran may be spent.
    c, client = _cloud_coordinator(hass, entry, grill=COOKING)
    await c.async_refresh()
    calls = _recorded(client)

    await c.async_configure_cook(temp=200)
    assert calls[-1]["seconds"] == 1800
    c._issued_at -= 20
    client.get_combined_state.return_value = _polled(
        {"state": "lid open", "mode": "roast", "setpoint": 200,
         "seconds set": 1800, "seconds left": 1795}
    )
    await c.async_refresh()

    await c.async_configure_cook(temp=210)

    assert calls[-1]["seconds"] == 1795


@pytest.mark.asyncio
async def test_pending_time_runs_down_while_the_cook_timer_runs(hass, entry):
    c, ble = _cooking_coordinator(hass, entry, grill=COOKING)
    await c.async_refresh()
    calls = _recorded(ble)

    await c.async_configure_cook(temp=200)
    assert calls[-1]["seconds"] == 1800
    c._issued_at -= 20
    await c.async_configure_cook(temp=210)

    assert calls[-1]["seconds"] == 1780
