"""Meat-probe food presets: protein + doneness -> target temperature.

Extracted from the Ninja app's own bundled preset table (EU/international
region, `assets/json/probe_presets.json` in the APK), not guessed: the
default for Beef ("Rare 1", 42 °C) was confirmed live on the real grill
2026-11-06 - selecting it in the app sent exactly this value.

The grill's BLE protocol has no concept of a preset at all: `ble_proto.py`
only ever writes a single raw °C target (`@60 u16 probe0 target`). The
cloud command schema (reverse-engineered from the app's Kotlin serializers,
`CookProbe`/`Food`/`Doneness`) does carry a food/doneness pair, but using it
would mean an unverified write shape against a live account the project
can't safely test. So this table exists purely to resolve "Beef, medium"
to the same °C value the app would use, then hand it to the existing
manual-setpoint path (`async_set_probe`) - identical effect, zero protocol
or cloud-write risk, works the same on BLE and cloud.
"""
from __future__ import annotations

from dataclasses import dataclass

# The sentinel meaning "no preset, use the plain number entity".
PROBE_FOOD_MANUAL = "Manual"


@dataclass(frozen=True)
class DonenessLevel:
    label: str  # English label as the app shows it, e.g. "Rare 1"
    temp_c: int


@dataclass(frozen=True)
class FoodPreset:
    key: str  # matches the app's `Food` enum member name
    levels: tuple[DonenessLevel, ...]

    @property
    def default_temp_c(self) -> int:
        return self.levels[0].temp_c


FOOD_PRESETS: tuple[FoodPreset, ...] = (
    FoodPreset("Chicken", (DonenessLevel("Well", 74),)),
    FoodPreset("Beef", (
        DonenessLevel("Rare 1", 42),
        DonenessLevel("Rare 2", 44),
        DonenessLevel("M. Rare 3", 47),
        DonenessLevel("M. Rare 4", 50),
        DonenessLevel("Medium 5", 53),
        DonenessLevel("Medium 6", 56),
        DonenessLevel("M. Well 7", 59),
        DonenessLevel("M. Well 8", 62),
        DonenessLevel("Well 9", 65),
    )),
    FoodPreset("Pork", (
        DonenessLevel("Medium", 65),
        DonenessLevel("Med Well", 70),
        DonenessLevel("Well", 75),
    )),
    FoodPreset("Lamb", (
        DonenessLevel("Rare", 55),
        DonenessLevel("Med Rare", 60),
        DonenessLevel("Medium", 65),
        DonenessLevel("Med Well", 70),
        DonenessLevel("Well", 75),
    )),
    FoodPreset("Fish", (
        DonenessLevel("Med Rare", 45),
        DonenessLevel("Medium", 50),
        DonenessLevel("Med Well", 55),
        DonenessLevel("Well", 60),
    )),
)

PROBE_FOODS: tuple[str, ...] = (PROBE_FOOD_MANUAL,) + tuple(f.key for f in FOOD_PRESETS)


def get_food_preset(key: str) -> FoodPreset | None:
    """The preset for `key`, or None for "Manual" / an unknown key."""
    for f in FOOD_PRESETS:
        if f.key == key:
            return f
    return None
