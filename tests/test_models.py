import json

from custom_components.ninja_woodfire._lib.models import GrillState, ProbeState


def test_null_and_float_strings_parse():
    g = GrillState.from_property_value(json.dumps(
        {"state": "cooking", "setpoint": None, "seconds set": "1800.0", "seconds left": 12.7, "endtimeutc": "x"}
    ))
    assert g.setpoint is None
    assert g.seconds_set == 1800
    assert g.seconds_left == 12
    assert g.end_time_utc is None


def test_probe_with_null_values_parses():
    p = ProbeState.from_property_value(json.dumps(
        {"probes": [{"name": "probe0", "temp": None, "progress": None, "mode": {"mode": "manual", "setpoint": None}}]}
    ))
    assert p.probes[0].temp == 0.0
    assert p.probes[0].progress == 0
    assert p.probes[0].target.setpoint is None


def test_missing_or_null_property_values_parse_to_defaults():
    from custom_components.ninja_woodfire._lib.models import CookState

    for raw in (None, "null", "[]", 5):
        assert GrillState.from_property_value(raw).state == "unknown"
        assert CookState.from_property_value(raw).state == "none"
        assert ProbeState.from_property_value(raw).probes == []


def test_non_finite_numbers_are_ignored():
    g = GrillState.from_property_value(json.dumps({"state": "cooking", "setpoint": "inf", "seconds left": "nan"}))
    assert g.setpoint is None
    assert g.seconds_left is None
