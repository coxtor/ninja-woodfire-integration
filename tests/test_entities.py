import json
from pathlib import Path

from custom_components.ninja_woodfire.binary_sensor import BINARY_SENSORS
from custom_components.ninja_woodfire.button import BUTTONS
from custom_components.ninja_woodfire.number import CookTempNumber
from custom_components.ninja_woodfire.sensor import (
    CLOUD_SENSORS,
    DIAGNOSTICS,
    SENSORS,
    NinjaSensor,
    _cook_phase,
)
from custom_components.ninja_woodfire.switch import SWITCHES

from .conftest import cooking, mk_state

COMPONENT = Path(__file__).parent.parent / "custom_components" / "ninja_woodfire"


def _setpoint_sensor(coordinator):
    return NinjaSensor(coordinator, next(d for d in SENSORS if d.key == "setpoint"))


async def test_setpoint_sensor_for_foreign_grill_cook(coordinator):
    coordinator.data = mk_state(cooking(mode="grill", setpoint=260))
    assert _setpoint_sensor(coordinator).native_value == "Hi"


async def test_cook_temp_number_in_range_during_grill_cook(coordinator):
    coordinator.data = mk_state(cooking(mode="grill", setpoint=230))
    n = CookTempNumber(coordinator)
    assert n.native_min_value <= n.native_value <= n.native_max_value


def test_paused_states_keep_cooking_phase():
    for s in ("lid open", "flip", "get food"):
        assert _cook_phase(mk_state(cooking(state=s))) == "cooking"


def test_translations_cover_all_entities():
    keys = {
        "sensor": {d.translation_key for d in (*SENSORS, *DIAGNOSTICS, *CLOUD_SENSORS)},
        "binary_sensor": {d.translation_key for d in BINARY_SENSORS},
        "switch": {d.translation_key for d in SWITCHES},
        "button": {d.translation_key for d in BUTTONS},
        "number": {"cook_duration", "cook_temp", "probe0_target_temp", "probe1_target_temp"},
        "select": {"cook_mode"},
    }
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        data = json.loads((COMPONENT / name).read_text())["entity"]
        for platform, wanted in keys.items():
            missing = wanted - set(data.get(platform, {}))
            assert not missing, f"{name} {platform}: {missing}"
