"""Per-grill-model capabilities.

Selected at config-flow time from the device's `oem_model`. Unknown
models fall back to a generic profile so the integration still loads
with sensible defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModeCapability:
    """What temp range + smoke options a single mode supports.

    `temp` semantics depend on `temp_unit`:
      "level" — 1/2/3 = Lo/Med/Hi (only `grill` mode on Woodfire)
      "celsius" — direct °C value
    """

    name: str                       # wire-protocol mode name
    display_name: str
    temp_unit: str                  # "level" or "celsius"
    temp_min: int
    temp_max: int
    temp_step: int = 1
    duration_min_s: int = 60
    duration_max_s: int = 86400
    duration_default_s: int = 1800
    temp_default: int = 0
    supports_smoke: bool = False
    smoke_default: bool = False
    supports_probe: bool = True


@dataclass(frozen=True)
class GrillCapabilities:
    """All capabilities for a specific grill model."""

    model_id: str
    display_name: str
    modes: tuple[ModeCapability, ...]
    has_two_probes: bool = True
    supports_woodfire_aroma: bool = True

    @property
    def mode_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.modes)

    def get_mode(self, name: str) -> ModeCapability | None:
        for m in self.modes:
            if m.name == name:
                return m
        return None


# ----------------------------------------------------- Woodfire Pro XL OG900-EU

WOODFIRE_PRO_XL = GrillCapabilities(
    model_id="OG900-EU",
    display_name="Woodfire Connect Pro XL",
    modes=(
        ModeCapability(
            name="grill",
            display_name="Grill",
            temp_unit="level",
            temp_min=1, temp_max=3, temp_default=2,
            duration_default_s=600,         # 10 min
            duration_max_s=3600,            # 1 h — anyone grilling longer is on the wrong mode
            supports_smoke=True,
            supports_probe=True,
        ),
        ModeCapability(
            name="smoker",
            display_name="Smoker",
            temp_unit="celsius",
            temp_min=55, temp_max=210,
            temp_default=110, duration_default_s=14400,    # 4 h
            duration_max_s=43200,                          # 12 h
            supports_smoke=True, smoke_default=True,
            supports_probe=True,
        ),
        ModeCapability(
            name="bake",
            display_name="Bake",
            temp_unit="celsius",
            temp_min=120, temp_max=210,
            temp_default=180, duration_default_s=1800,     # 30 min
            duration_max_s=14400,                          # 4 h
            supports_smoke=True,
            supports_probe=True,
        ),
        ModeCapability(
            name="roast",
            display_name="Roast",
            temp_unit="celsius",
            temp_min=120, temp_max=230,
            temp_default=180, duration_default_s=1500,     # 25 min
            duration_max_s=14400,                          # 4 h
            supports_smoke=True,
            supports_probe=True,
        ),
        ModeCapability(
            name="dehydrate",
            display_name="Dehydrate",
            temp_unit="celsius",
            temp_min=40, temp_max=90,
            temp_default=60, duration_default_s=21600,     # 6 h
            duration_max_s=86400,                          # 24 h
            supports_smoke=False,
            supports_probe=False,
        ),
        ModeCapability(
            name="reheat",
            display_name="Reheat",
            temp_unit="celsius",
            temp_min=120, temp_max=210,
            temp_default=170, duration_default_s=900,      # 15 min
            duration_max_s=5400,                           # 90 min
            supports_smoke=False,
            supports_probe=True,
        ),
        ModeCapability(
            name="air crisp",
            display_name="Air Crisp",
            temp_unit="celsius",
            temp_min=120, temp_max=240,
            temp_default=200, duration_default_s=1200,     # 20 min
            duration_max_s=5400,                           # 90 min
            supports_smoke=False,
            supports_probe=True,
        ),
    ),
)


# ----------------------------------------------------- Generic / fallback

GENERIC = GrillCapabilities(
    model_id="GENERIC",
    display_name="Ninja Grill (generic)",
    modes=(
        ModeCapability(
            name="grill",
            display_name="Grill",
            temp_unit="level",
            temp_min=1, temp_max=3, temp_default=2,
            duration_default_s=600,
            supports_smoke=True,
            supports_probe=True,
        ),
    ),
    has_two_probes=False,
)


# ----------------------------------------------------- registry

_MODELS: dict[str, GrillCapabilities] = {
    "OG900-EU": WOODFIRE_PRO_XL,
    "OG901-EU": WOODFIRE_PRO_XL,
    "OG900": WOODFIRE_PRO_XL,
}


def for_oem_model(oem_model: str | None) -> GrillCapabilities:
    """Look up capabilities for a specific oem_model string."""
    if oem_model and oem_model in _MODELS:
        return _MODELS[oem_model]
    if oem_model and oem_model.startswith("OG9"):
        return WOODFIRE_PRO_XL
    return GENERIC
