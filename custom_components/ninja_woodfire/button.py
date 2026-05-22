"""Start / stop cook buttons."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[NinjaWoodfireCoordinator], Awaitable[None]]


BUTTONS: tuple[NinjaButtonDescription, ...] = (
    NinjaButtonDescription(
        key="start_cook",
        translation_key="start_cook",
        icon="mdi:play",
        press_fn=lambda c: c.async_start_cook(),
    ),
    NinjaButtonDescription(
        key="stop_cook",
        translation_key="stop_cook",
        icon="mdi:stop",
        press_fn=lambda c: c.async_stop_cook(),
    ),
    NinjaButtonDescription(
        key="skip_preheat",
        translation_key="skip_preheat",
        icon="mdi:fast-forward",
        press_fn=lambda c: c.async_skip_preheat(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NinjaWoodfireCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NinjaButton(coordinator, desc) for desc in BUTTONS)


class NinjaButton(NinjaWoodfireEntity, ButtonEntity):
    entity_description: NinjaButtonDescription

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaButtonDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
