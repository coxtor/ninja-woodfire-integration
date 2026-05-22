"""Cook-config switches: smoke add-on, skip preheat, probe enable.

Most of these hold session-only state in the coordinator — they don't write
to the cloud directly. The "Start cook" button reads them when sending the
SET_Cook_Command. This matches how the official app works.

Exception: the **skip-preheat switch** is context-aware. While the grill is
in `preheat`, flipping it to ON immediately re-issues the cook command with
`skip preheat: 1` (the firmware accepts that and jumps to the cook phase).
While idle, it just stages the flag for the next Start press.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NinjaSwitchDescription(SwitchEntityDescription):
    get_fn: Callable[[NinjaWoodfireCoordinator], bool]
    set_fn: Callable[[NinjaWoodfireCoordinator, bool], None]
    available_fn: Callable[[NinjaWoodfireCoordinator], bool] | None = None
    # Optional async post-set hook. Lets a switch trigger a real cloud action
    # (e.g. skip-preheat re-issues the cook command if currently preheating).
    post_set_fn: Callable[[NinjaWoodfireCoordinator, bool], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Post-set hooks (must be defined before SWITCHES references them)
# ---------------------------------------------------------------------------

_PREHEAT_STATES = frozenset({"preheat", "preheating", "start", "ignition"})


async def _maybe_skip_preheat_now(
    coordinator: NinjaWoodfireCoordinator, new_value: bool
) -> None:
    """If the grill is currently preheating, immediately fire skip-preheat.

    The firmware can't reverse a phase transition, so flipping the switch
    OFF while already past preheat does nothing. Flipping ON during preheat
    re-issues the cook command with `skip preheat: 1`, which the firmware
    accepts and jumps straight into cooking.

    When idle, the switch just stages the flag for the next Start press.
    """
    if not new_value:
        return
    state = coordinator.data
    if state is None:
        return
    if (
        state.grill.state in _PREHEAT_STATES
        or state.cook.state in _PREHEAT_STATES
    ):
        _LOGGER.info("skip-preheat switch flipped during preheat — re-issuing cook")
        try:
            await coordinator.async_skip_preheat()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("skip-preheat trigger failed: %s", err)


# ---------------------------------------------------------------------------
# Switch table
# ---------------------------------------------------------------------------

async def _modify_smoke(
    coordinator: NinjaWoodfireCoordinator, new_value: bool
) -> None:
    """Apply smoke change live during a cook, otherwise stage it."""
    await coordinator.async_modify_cook(smoke=new_value)


SWITCHES: tuple[NinjaSwitchDescription, ...] = (
    NinjaSwitchDescription(
        key="cook_smoke",
        translation_key="cook_smoke",
        icon="mdi:smoke",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda c: c.live_or_staged_smoke,
        set_fn=lambda c, v: setattr(c, "cook_setting_smoke", v),
        available_fn=lambda c: (
            (m := c.capabilities.get_mode(c.live_or_staged_mode)) is not None
            and m.supports_smoke
        ),
        post_set_fn=_modify_smoke,
    ),
    NinjaSwitchDescription(
        key="cook_skip_preheat",
        translation_key="cook_skip_preheat",
        icon="mdi:fast-forward",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda c: c.cook_setting_skip_preheat,
        set_fn=lambda c, v: setattr(c, "cook_setting_skip_preheat", v),
        # When flipped ON during preheat, immediately apply by re-issuing
        # the cook command. While idle it just stages the flag for next start.
        post_set_fn=_maybe_skip_preheat_now,
    ),
    NinjaSwitchDescription(
        key="probe0_enabled",
        translation_key="probe0_enabled",
        icon="mdi:thermometer-probe",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda c: c.cook_setting_probe0_enabled,
        set_fn=lambda c, v: setattr(c, "cook_setting_probe0_enabled", v),
        available_fn=lambda c: (
            (m := c.capabilities.get_mode(c.live_or_staged_mode)) is not None
            and m.supports_probe
        ),
    ),
    NinjaSwitchDescription(
        key="probe1_enabled",
        translation_key="probe1_enabled",
        icon="mdi:thermometer-probe",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda c: c.cook_setting_probe1_enabled,
        set_fn=lambda c, v: setattr(c, "cook_setting_probe1_enabled", v),
        available_fn=lambda c: (
            c.capabilities.has_two_probes
            and (m := c.capabilities.get_mode(c.live_or_staged_mode)) is not None
            and m.supports_probe
        ),
    ),
)


# ---------------------------------------------------------------------------
# Entity glue
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NinjaWoodfireCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NinjaSwitch(coordinator, desc) for desc in SWITCHES)


class NinjaSwitch(NinjaWoodfireEntity, SwitchEntity):
    entity_description: NinjaSwitchDescription

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def is_on(self) -> bool:
        return self.entity_description.get_fn(self.coordinator)

    @property
    def available(self) -> bool:
        if self.entity_description.available_fn is None:
            return super().available
        return super().available and self.entity_description.available_fn(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.entity_description.set_fn(self.coordinator, True)
        self.async_write_ha_state()
        if self.entity_description.post_set_fn:
            await self.entity_description.post_set_fn(self.coordinator, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.entity_description.set_fn(self.coordinator, False)
        self.async_write_ha_state()
        if self.entity_description.post_set_fn:
            await self.entity_description.post_set_fn(self.coordinator, False)
