"""I-02: the atomic `configure_cook` service."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .conftest import DEVICE, DOMAIN, DSN, cooking, mk_state, setup_integration


def _client(state=None):
    client = AsyncMock()
    client.get_devices = AsyncMock(return_value=[DEVICE])
    client.get_combined_state = AsyncMock(
        return_value=state or mk_state(cooking(setpoint=180, left=1800, total=1800))
    )
    return client


def _device_id(hass, entry):
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return next(d.id for d in devices if (DOMAIN, DSN) in d.identifiers)


async def test_configure_cook_service_sends_one_merged_command(hass, entry):
    client = _client()
    await setup_integration(hass, entry, client)
    entry.runtime_data._burst_refresh = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        "configure_cook",
        {
            "device_id": _device_id(hass, entry),
            "temp": 200,
            "duration": 40,
            "probe_0_enabled": True,
            "probe_0_target": 70,
        },
        blocking=True,
    )

    assert client.start_cook.await_count == 1
    kwargs = client.start_cook.call_args.kwargs
    assert kwargs["temp"] == 200
    assert kwargs["seconds"] >= 2390
    assert kwargs["probe_0"] == {"mode": "manual", "setpoint": 70}


async def test_configure_cook_service_can_start(hass, entry):
    client = _client(mk_state())
    await setup_integration(hass, entry, client)
    entry.runtime_data._burst_refresh = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        "configure_cook",
        {"device_id": _device_id(hass, entry), "mode": "bake", "temp": 180,
         "duration": 20, "start": True},
        blocking=True,
    )

    kwargs = client.start_cook.call_args.kwargs
    assert (kwargs["mode"], kwargs["temp"], kwargs["seconds"]) == ("bake", 180, 1200)


async def test_configure_cook_service_rejects_an_unknown_device(hass, entry):
    await setup_integration(hass, entry, _client())
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "configure_cook", {"device_id": "nope", "temp": 180}, blocking=True
        )


# ------------------------------------------------- service validation


async def _call(hass, entry, data):
    await hass.services.async_call(
        DOMAIN, "configure_cook", {"device_id": _device_id(hass, entry), **data}, blocking=True
    )


# Out-of-range values are rejected by the service schema (vol.Invalid), values
# this grill can't cook by the coordinator (ServiceValidationError) — either way
# nothing reaches the grill and nothing is staged.
REJECTED = (ServiceValidationError, vol.Invalid)


@pytest.mark.parametrize("data", [
    {"mode": "teleport"},                  # no such mode
    {"mode": "sousvide"},                  # not on this grill
    {"duration": 0},
    {"duration": -10},
    {"temp": 400},                         # beyond the mode's range
    {"probe_0_target": 5},
    {"probe_0_target": 300},
    {"mode": "dehydrate", "probe_0_enabled": True},   # mode has no probe
])
async def test_configure_cook_service_rejects_impossible_values(hass, entry, data):
    client = _client(mk_state())
    await setup_integration(hass, entry, client)
    entry.runtime_data._burst_refresh = AsyncMock()

    with pytest.raises(REJECTED):
        await _call(hass, entry, data)

    client.start_cook.assert_not_awaited()
    assert entry.runtime_data.cook_setting_mode == "grill"     # nothing staged


async def test_configure_cook_service_accepts_the_documented_ranges(hass, entry):
    client = _client(mk_state())
    await setup_integration(hass, entry, client)
    entry.runtime_data._burst_refresh = AsyncMock()

    await _call(hass, entry, {"mode": "roast", "temp": 230, "duration": 240,
                              "probe_0_enabled": True, "probe_0_target": 110, "start": True})

    kwargs = client.start_cook.call_args.kwargs
    assert (kwargs["mode"], kwargs["temp"], kwargs["seconds"]) == ("roast", 230, 14400)
    assert kwargs["probe_0"] == {"mode": "manual", "setpoint": 110}


async def test_a_cook_in_an_unmodelled_mode_can_still_be_adjusted(hass, entry):
    client = _client(mk_state(cooking(mode="broil", setpoint=200, left=900, total=900)))
    await setup_integration(hass, entry, client)
    entry.runtime_data._burst_refresh = AsyncMock()

    await _call(hass, entry, {"temp": 220})

    assert client.start_cook.call_args.kwargs["temp"] == 220
