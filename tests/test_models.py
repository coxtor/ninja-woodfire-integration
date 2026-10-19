import json

import pytest

from custom_components.ninja_woodfire._lib.api.aws import state_from_device
from custom_components.ninja_woodfire._lib.models import CombinedState, GrillState, ProbeState


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


# I-11: malformed nested structures must parse to defaults, never raise.


@pytest.mark.parametrize("raw", [
    {"inputs": None},
    {"inputs": "nope"},
    {"inputs": {"temps": None}},
    {"inputs": {"temps": [1, 2]}},
    {"inputs": {"io": None}},
    {"inputs": {"io": "lid open"}},
])
def test_malformed_grill_inputs_parse_to_defaults(raw):
    g = GrillState.from_property_value(json.dumps(raw))
    assert g.temps.grill is None
    assert g.lid_open is False


@pytest.mark.parametrize("raw", [
    {"probes": [None]},
    {"probes": ["probe0"]},
    {"probes": "none"},
    {"probes": {"probe0": {}}},
    {"probes": [{"name": "probe0", "mode": "manual", "state": None}]},
])
def test_malformed_probe_lists_parse_to_defaults(raw):
    assert isinstance(ProbeState.from_property_value(json.dumps(raw)).probes, list)


@pytest.mark.parametrize("device", [
    {"connectivityStatus": "connected"},
    {"connectivityStatus": []},
    {"telemetry": None},
    {"telemetry": "GrillState"},
    {"telemetry": {"GrillState": {"inputs": None}}},
])
def test_malformed_aws_device_records_parse(device):
    state = state_from_device("AC1", device)
    assert isinstance(state, CombinedState)


def test_a_naive_cloud_timestamp_is_treated_as_utc():
    from datetime import datetime, timezone

    from custom_components.ninja_woodfire._lib.api.ayla import AylaCloudClient

    state = CombinedState(dsn="AC1")
    state.last_updated_at = AylaCloudClient._parse_timestamp("2026-08-19T18:39:58.666")
    assert state.last_updated_at.tzinfo is not None
    assert state.last_updated_at == datetime(
        2026, 8, 19, 18, 39, 58, 666000, tzinfo=timezone.utc
    )
    assert state.state_age_seconds() is not None
