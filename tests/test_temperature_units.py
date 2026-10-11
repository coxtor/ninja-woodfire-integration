"""Unit handling of GET_GrillState.inputs.temps.

The firmware reports the raw sensor block (grill/air/smoke/probeN_a/b) in
°F, while `setpoint` and `GET_ProbeState.probes[].temp` are already °C.
Captured evidence: probe1_a 79/107/114/122 alongside ProbeState temp
26.1/41.6/45.5/50.0 — the same measurement in both units.
"""
import json

import pytest

from custom_components.ninja_woodfire._lib.models import (
    FAHRENHEIT_TEMP_FIELDS,
    GrillState,
    GrillTemps,
)
from custom_components.ninja_woodfire.sensor import _plausible_temp

from .conftest import cooking, mk_state, probe


def _temps(**temps) -> dict:
    return {"inputs": {"temps": temps, "io": {"lid open": 0}}}


@pytest.mark.parametrize(
    "field", ["grill", "air", "smoke", "probe0_a", "probe0_b", "probe1_a", "probe1_b"]
)
def test_fahrenheit_fields_are_converted(field: str) -> None:
    temps = GrillState.from_property_value(json.dumps(_temps(**{field: 212}))).temps
    assert getattr(temps, field) == 100.0


def test_conversion_rounds_to_one_decimal() -> None:
    assert GrillTemps.from_wire({"air": 72}).air == 22.2
    assert GrillTemps.from_wire({"grill": 243.3}).grill == 117.4


def test_pcb_channels_are_untouched() -> None:
    temps = GrillTemps.from_wire({"main": 6542.4, "ui": 6513.6})
    assert temps.main == 6542.4
    assert temps.ui == 6513.6
    assert FAHRENHEIT_TEMP_FIELDS == set(vars(GrillTemps())) - {"main", "ui"}


def test_invalid_values_fall_back_to_zero() -> None:
    temps = GrillTemps.from_wire({"grill": "nan", "air": None, "main": "x"})
    assert temps.grill == 0.0
    assert temps.air == 0.0
    assert temps.main == 0.0


def test_captured_probe_pair_agrees_and_probe_state_is_not_converted() -> None:
    state = mk_state(
        grill={"state": "idle", **_temps(probe1_a=79, probe1_b=79)},
        probes={"probes": [probe(temp=26.1, active=0)]},
    )
    assert state.grill.temps.probe1_a == 26.1
    assert state.probes.probes[0].temp == 26.1


def test_setpoint_is_not_converted() -> None:
    state = mk_state(grill=cooking(mode="bake", setpoint=160, **_temps(air=320)))
    assert state.grill.setpoint == 160
    assert state.grill.temps.air == 160.0


def test_idle_ambient_reading_is_shown() -> None:
    state = mk_state(grill={"state": "idle", **_temps(grill=72, air=72.5)})
    assert _plausible_temp(state, state.grill.temps.grill, "grill") == 22.2
    assert _plausible_temp(state, state.grill.temps.air, "air") == 22.5


def test_hot_idle_stale_cache_is_still_hidden() -> None:
    state = mk_state(grill={"state": "idle", **_temps(grill=243.3, air=210.7)})
    assert _plausible_temp(state, state.grill.temps.grill, "grill") is None
    assert _plausible_temp(state, state.grill.temps.air, "air") is None


def test_cook_reading_is_converted() -> None:
    state = mk_state(
        grill=cooking(mode="bake", setpoint=200, **_temps(grill=243.3, air=384.2, smoke=273.9))
    )
    assert _plausible_temp(state, state.grill.temps.grill, "grill") == 117.4
    assert _plausible_temp(state, state.grill.temps.air, "air") == 195.7


def test_missing_temps_stay_zero_not_minus_17_8() -> None:
    assert GrillTemps.from_wire({}) == GrillTemps()
