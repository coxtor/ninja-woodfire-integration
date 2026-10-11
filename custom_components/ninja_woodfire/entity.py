"""Common base class for Ninja Woodfire entities."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator


class NinjaWoodfireEntity(CoordinatorEntity[NinjaWoodfireCoordinator]):
    _attr_has_entity_name = True
    _requires_live_state = True

    def __init__(self, coordinator: NinjaWoodfireCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.device_info_extra
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.dsn)},
            name=info.get("product_name") or coordinator.capabilities.display_name,
            manufacturer="SharkNinja",
            model=info.get("oem_model") or coordinator.capabilities.display_name,
            sw_version=info.get("sw_version") or None,
            serial_number=coordinator.dsn,
        )

    @property
    def available(self) -> bool:
        return super().available and (
            not self._requires_live_state or self.coordinator.state_online
        )
