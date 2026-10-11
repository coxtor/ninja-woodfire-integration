"""Cook-config switches: smoke add-on, skip preheat, probe enable.

While idle these stage settings for the next Start press. During a cook,
smoke and probe changes are applied live; flipping skip-preheat ON while
preheating skips the preheat phase immediately.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NinjaWoodfireConfigEntry, NinjaWoodfireCoordinator, is_preheating
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaSwitchDescription(SwitchEntityDescription):
    get_fn: Callable[[NinjaWoodfireCoordinator], bool]
    set_fn: Callable[[NinjaWoodfireCoordinator, bool], None] | None = None
    available_fn: Callable[[NinjaWoodfireCoordinator], bool] | None = None
    # Optional async post-set hook. Lets a switch trigger a real cloud action
    # (e.g. skip-preheat re-issues the cook command if currently preheating).
    post_set_fn: Callable[[NinjaWoodfireCoordinator, bool], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Post-set hooks (must be defined before SWITCHES references them)
# ---------------------------------------------------------------------------

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
    if coordinator.data is not None and is_preheating(coordinator.data):
        await coordinator.async_skip_preheat()


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
        get_fn=lambda c: c.live_or_staged_probe_enabled(0),
        post_set_fn=lambda c, v: c.async_set_probe(0, enabled=v),
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
        get_fn=lambda c: c.live_or_staged_probe_enabled(1),
        post_set_fn=lambda c, v: c.async_set_probe(1, enabled=v),
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
    entry: NinjaWoodfireConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
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
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        desc = self.entity_description
        if desc.set_fn:
            desc.set_fn(self.coordinator, value)
            self.async_write_ha_state()
        if desc.post_set_fn:
            await desc.post_set_fn(self.coordinator, value)
