"""Cook-temp / duration / probe-target setters."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NinjaWoodfireCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        CookDurationNumber(coordinator),
        CookTempNumber(coordinator),
        ProbeSetpointNumber(coordinator, probe_index=0),
    ]
    if coordinator.capabilities.has_two_probes:
        entities.append(ProbeSetpointNumber(coordinator, probe_index=1))
    async_add_entities(entities)


class CookDurationNumber(NinjaWoodfireEntity, NumberEntity):
    _attr_translation_key = "cook_duration"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 1
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer"

    def __init__(self, coordinator: NinjaWoodfireCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.dsn}_cook_duration"

    @property
    def _active_mode(self):
        return self.coordinator.capabilities.get_mode(
            self.coordinator.live_or_staged_mode
        )

    @property
    def native_max_value(self) -> float:
        m = self._active_mode
        # Per-mode cap so the slider doesn't go to 12 h when the user
        # is in Grill mode where any cook over an hour is a mistake.
        if m and m.duration_max_s:
            return m.duration_max_s / 60.0
        return 720.0

    @property
    def native_value(self) -> float:
        return self.coordinator.live_or_staged_seconds / 60.0

    async def async_set_native_value(self, value: float) -> None:
        # Active cook: re-issues with new total duration (preserves
        # the cook in progress). Idle: stages for next start.
        await self.coordinator.async_modify_cook(seconds=int(value * 60))
        self.coordinator.cook_setting_seconds = int(value * 60)
        self.async_write_ha_state()


class CookTempNumber(NinjaWoodfireEntity, NumberEntity):
    """Mode-aware temp/heat-level number.

    Range + unit follow the currently selected cook_mode:
      grill mode (heat-level)  → 1..3, no unit
      celsius modes            → mode-specific min/max, °C
    """

    _attr_translation_key = "cook_temp"
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NinjaWoodfireCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.dsn}_cook_temp"

    @property
    def _active_mode(self):
        return self.coordinator.capabilities.get_mode(self.coordinator.live_or_staged_mode)

    @property
    def icon(self) -> str:
        m = self._active_mode
        if m and m.temp_unit == "level":
            return "mdi:fire"
        return "mdi:thermometer"

    @property
    def native_unit_of_measurement(self) -> str | None:
        m = self._active_mode
        if m and m.temp_unit == "celsius":
            return UnitOfTemperature.CELSIUS
        return None

    @property
    def native_min_value(self) -> float:
        m = self._active_mode
        return m.temp_min if m else 0

    @property
    def native_max_value(self) -> float:
        m = self._active_mode
        return m.temp_max if m else 300

    @property
    def native_step(self) -> float:
        m = self._active_mode
        return m.temp_step if m else 1

    @property
    def native_value(self) -> float:
        # During an active cook the firmware reports its internal °C
        # target even for grill mode (which the user picked as 1/2/3).
        # Showing the live value mid-cook is more honest than echoing
        # the staged level.
        return float(self.coordinator.live_or_staged_temp)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_modify_cook(temp=int(value))
        self.coordinator.cook_setting_temp = int(value)
        self.async_write_ha_state()


class ProbeSetpointNumber(NinjaWoodfireEntity, NumberEntity):
    """Per-probe target setpoint in °C (manual mode).

    Companion switch ('Probe N target enabled') controls whether the cook
    is sent with this probe targeting at all.
    """

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 30
    _attr_native_max_value = 110
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-probe"

    def __init__(
        self, coordinator: NinjaWoodfireCoordinator, probe_index: int
    ) -> None:
        super().__init__(coordinator)
        self._probe_index = probe_index
        self._attr_translation_key = f"probe{probe_index}_target_temp"
        self._attr_unique_id = f"{coordinator.dsn}_probe{probe_index}_target_temp"

    @property
    def native_value(self) -> float:
        if self._probe_index == 0:
            return float(self.coordinator.cook_setting_probe0_setpoint)
        return float(self.coordinator.cook_setting_probe1_setpoint)

    async def async_set_native_value(self, value: float) -> None:
        v = int(value)
        if self._probe_index == 0:
            self.coordinator.cook_setting_probe0_setpoint = v
        else:
            self.coordinator.cook_setting_probe1_setpoint = v
        self.async_write_ha_state()
