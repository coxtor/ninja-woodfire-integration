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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._lib.models import CombinedState

from .coordinator import NinjaWoodfireConfigEntry, NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[CombinedState], bool | None]
    attrs_fn: Callable[[CombinedState], dict[str, Any]] | None = None
    requires_live_state: bool = True
    requires_successful_poll: bool = True


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
    NinjaBinarySensorDescription(
        key="cloud_connected",
        translation_key="cloud_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.online,
        attrs_fn=lambda s: {"connection_status": s.connection_status},
        requires_live_state=False,
        requires_successful_poll=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NinjaWoodfireConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
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
        self._requires_live_state = description.requires_live_state
        self._requires_successful_poll = description.requires_successful_poll
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        if fn is None or self.coordinator.data is None:
            return None
        return fn(self.coordinator.data)
