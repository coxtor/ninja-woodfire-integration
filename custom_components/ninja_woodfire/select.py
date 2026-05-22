"""Cook-mode selector. Options come from the active capability model."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
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
    async_add_entities([CookModeSelect(coordinator)])


class CookModeSelect(NinjaWoodfireEntity, SelectEntity):
    _attr_translation_key = "cook_mode"
    _attr_icon = "mdi:grill"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NinjaWoodfireCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.dsn}_cook_mode"
        self._attr_options = [m.name for m in coordinator.capabilities.modes]

    @property
    def current_option(self) -> str | None:
        return self.coordinator.live_or_staged_mode

    async def async_select_option(self, option: str) -> None:
        # Snap temp/smoke to the new mode's compatibility before
        # we send anything: if temp is out of range or smoke isn't
        # supported, fix it locally so we don't issue an invalid combo.
        new_mode = self.coordinator.capabilities.get_mode(option)
        snap_temp: int | None = None
        snap_smoke: bool | None = None
        if new_mode:
            t = self.coordinator.live_or_staged_temp
            if t < new_mode.temp_min or t > new_mode.temp_max:
                snap_temp = new_mode.temp_default
                self.coordinator.cook_setting_temp = snap_temp
            cur_smoke = self.coordinator.live_or_staged_smoke
            if not new_mode.supports_smoke and cur_smoke:
                snap_smoke = False
                self.coordinator.cook_setting_smoke = False
            elif new_mode.smoke_default and not cur_smoke:
                snap_smoke = True
                self.coordinator.cook_setting_smoke = True
        # Active cook: live re-issue with all snapped values in one shot.
        # Idle: just stages for next start.
        await self.coordinator.async_modify_cook(
            mode=option, temp=snap_temp, smoke=snap_smoke,
        )
        self.async_write_ha_state()
