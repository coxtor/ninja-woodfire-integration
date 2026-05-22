"""Lid + probe-plugged-in binary sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._lib.models import CombinedState

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[CombinedState], bool | None]


BINARY_SENSORS: tuple[NinjaBinarySensorDescription, ...] = (
    NinjaBinarySensorDescription(
        key="lid_open",
        translation_key="lid_open",
        device_class=BinarySensorDeviceClass.OPENING,
        value_fn=lambda s: s.grill.lid_open,
    ),
    NinjaBinarySensorDescription(
        key="probe0_plugged_in",
        translation_key="probe0_plugged_in",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda s: s.probes.probes[0].plugged_in if len(s.probes.probes) > 0 else None,
    ),
    NinjaBinarySensorDescription(
        key="probe1_plugged_in",
        translation_key="probe1_plugged_in",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda s: s.probes.probes[1].plugged_in if len(s.probes.probes) > 1 else None,
    ),
    NinjaBinarySensorDescription(
        key="probe0_active",
        translation_key="probe0_active",
        value_fn=lambda s: s.probes.probes[0].active if len(s.probes.probes) > 0 else None,
    ),
    NinjaBinarySensorDescription(
        key="probe1_active",
        translation_key="probe1_active",
        value_fn=lambda s: s.probes.probes[1].active if len(s.probes.probes) > 1 else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NinjaWoodfireCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NinjaBinarySensor(coordinator, desc) for desc in BINARY_SENSORS)


class NinjaBinarySensor(NinjaWoodfireEntity, BinarySensorEntity):
    entity_description: NinjaBinarySensorDescription

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
