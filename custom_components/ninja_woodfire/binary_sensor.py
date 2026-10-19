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
    value_fn: Callable[[CombinedState], bool | None] | None = None
    # For values that aren't part of the backend-agnostic CombinedState (e.g.
    # BLE reachability, tracked independently of which backend is active).
    coordinator_value_fn: Callable[[NinjaWoodfireCoordinator], bool | None] | None = None
    attrs_fn: Callable[[CombinedState], dict[str, Any]] | None = None
    coordinator_attrs_fn: Callable[[NinjaWoodfireCoordinator], dict[str, Any]] | None = None
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
        # In a hybrid setup this is whichever backend is serving right now, so
        # name it: it can be the local link rather than the cloud.
        coordinator_attrs_fn=lambda c: {
            "connection_status": c.data.connection_status if c.data else None,
            "backend": c.backend,
        },
        requires_live_state=False,
        requires_successful_poll=False,
    ),
    NinjaBinarySensorDescription(
        # The last GATT *connection*, not passive beacon reception: the grill
        # can keep advertising while refusing a connection, so both values are
        # exposed side by side.
        key="ble_connected",
        translation_key="ble_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        coordinator_value_fn=lambda c: c.ble_reachable,
        coordinator_attrs_fn=lambda c: {
            "last_gatt_attempt": c.ble_last_attempt,
            "beacon_age_seconds": c.beacon_age_seconds,
            "likely_asleep": c.likely_asleep,
        },
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
    skip = set()
    # "cloud_connected" is cloud-specific and misleading in local-only mode.
    if coordinator.client is None:
        skip.add("cloud_connected")
    # "ble_connected" only makes sense when BLE is actually configured.
    if coordinator.ble is None:
        skip.add("ble_connected")
    async_add_entities(
        NinjaBinarySensor(coordinator, desc)
        for desc in BINARY_SENSORS if desc.key not in skip
    )


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
        if self.entity_description.coordinator_value_fn is not None:
            return self.entity_description.coordinator_value_fn(self.coordinator)
        if self.coordinator.data is None or self.entity_description.value_fn is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (fn := self.entity_description.coordinator_attrs_fn) is not None:
            return fn(self.coordinator)
        fn = self.entity_description.attrs_fn
        if fn is None or self.coordinator.data is None:
            return None
        return fn(self.coordinator.data)
