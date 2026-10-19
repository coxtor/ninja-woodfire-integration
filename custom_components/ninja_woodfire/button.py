"""Start / stop cook buttons."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NinjaWoodfireConfigEntry, NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[NinjaWoodfireCoordinator], Awaitable[None]]
    requires_live_state: bool = True
    always_available: bool = False


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
        always_available=True,
        press_fn=lambda c: c.async_stop_cook(),
    ),
    NinjaButtonDescription(
        key="skip_preheat",
        translation_key="skip_preheat",
        icon="mdi:fast-forward",
        always_available=True,
        press_fn=lambda c: c.async_skip_preheat(),
    ),
    # Wake is a local-BLE feature: only the radio can rouse a sleeping grill.
    # On the cloud backends (including a hybrid entry that has fallen back to
    # cloud) the press is a no-op plus a refresh.
    NinjaButtonDescription(
        key="wake",
        translation_key="wake",
        icon="mdi:bell-ring",
        always_available=True,
        press_fn=lambda c: c.async_wake(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NinjaWoodfireConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
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
        self._requires_live_state = description.requires_live_state
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def available(self) -> bool:
        return self.entity_description.always_available or super().available

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
