"""Cook-mode and probe-food-preset selectors."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._lib.probe_presets import PROBE_FOODS, get_food_preset
from .coordinator import NinjaWoodfireConfigEntry, NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity

# A probe with no preset selected has nothing to show in the doneness
# select; HA selects can't be empty, so this placeholder fills the slot.
_NO_PRESET_OPTION = "—"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NinjaWoodfireConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [CookModeSelect(coordinator)]
    for idx in range(2 if coordinator.capabilities.has_two_probes else 1):
        entities.append(ProbeFoodSelect(coordinator, idx))
        entities.append(ProbeDonenessSelect(coordinator, idx))
    async_add_entities(entities)


class CookModeSelect(NinjaWoodfireEntity, SelectEntity):
    _requires_live_state = False
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


class ProbeFoodSelect(NinjaWoodfireEntity, SelectEntity):
    """Protein preset for a probe target, or "Manual" for a free-typed °C.

    Picking a preset resolves its current doneness level to a plain °C value
    and sends it through the same path the manual number entity uses - the
    grill's protocol has no concept of a preset (see `_lib/probe_presets.py`).
    """

    _requires_live_state = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:food-steak"
    _attr_options = list(PROBE_FOODS)

    def __init__(self, coordinator: NinjaWoodfireCoordinator, probe_index: int) -> None:
        super().__init__(coordinator)
        self._probe_index = probe_index
        self._attr_translation_key = f"probe{probe_index}_food"
        self._attr_unique_id = f"{coordinator.dsn}_probe{probe_index}_food"

    @property
    def current_option(self) -> str:
        return self.coordinator.live_or_staged_probe_food(self._probe_index)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_probe_food(self._probe_index, option)
        self.async_write_ha_state()


class ProbeDonenessSelect(NinjaWoodfireEntity, SelectEntity):
    """Doneness level within the probe's currently selected food preset."""

    _requires_live_state = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-chevron-up"

    def __init__(self, coordinator: NinjaWoodfireCoordinator, probe_index: int) -> None:
        super().__init__(coordinator)
        self._probe_index = probe_index
        self._attr_translation_key = f"probe{probe_index}_doneness"
        self._attr_unique_id = f"{coordinator.dsn}_probe{probe_index}_doneness"

    @property
    def _preset(self):
        food = self.coordinator.live_or_staged_probe_food(self._probe_index)
        return get_food_preset(food)

    @property
    def options(self) -> list[str]:
        preset = self._preset
        return [lvl.label for lvl in preset.levels] if preset else [_NO_PRESET_OPTION]

    @property
    def current_option(self) -> str:
        preset = self._preset
        if preset is None:
            return _NO_PRESET_OPTION
        idx = self.coordinator.live_or_staged_probe_doneness_index(self._probe_index)
        idx = max(0, min(idx, len(preset.levels) - 1))
        return preset.levels[idx].label

    @property
    def available(self) -> bool:
        return super().available and self._preset is not None

    async def async_select_option(self, option: str) -> None:
        preset = self._preset
        if preset is None:
            return
        idx = next(
            (i for i, lvl in enumerate(preset.levels) if lvl.label == option), 0
        )
        await self.coordinator.async_set_probe_doneness(self._probe_index, idx)
        self.async_write_ha_state()
