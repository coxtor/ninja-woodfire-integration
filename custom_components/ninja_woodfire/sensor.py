"""Temperature + state sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._lib.capabilities import grill_level
from ._lib.const import GRILL_HEAT_LEVELS, IDLE_STATES
from ._lib.models import CombinedState

from .const import COOKING_STATES, PAUSED_STATES, PREHEAT_STATES, REST_STATES

PROMPTS = ("addfood", "flipfood", "done", "getfood")

from .coordinator import NinjaWoodfireConfigEntry, NinjaWoodfireCoordinator
from .entity import NinjaWoodfireEntity


@dataclass(frozen=True, kw_only=True)
class NinjaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[CombinedState], Any]
    # Optional — if set, return value is exposed as `extra_state_attributes`
    # so templates/automations can branch without consulting other entities.
    attrs_fn: Callable[[CombinedState], dict[str, Any]] | None = None


def _setpoint_display(state: CombinedState, level: int | None = None) -> str | int | None:
    """User-facing setpoint: °C, or Lo/Med/Hi in grill mode."""
    sp = state.grill.setpoint
    if sp is None:
        return None
    if state.grill.mode != "grill":
        return sp
    return GRILL_HEAT_LEVELS.get(level if level in GRILL_HEAT_LEVELS else grill_level(sp))


def _cook_phase(state: CombinedState) -> str:
    """Sub-phase of the cook: preheat | cooking | rest | none."""
    g = state.grill.state
    c = state.cook.state
    if state.preheating:
        return "preheat"
    if g in PREHEAT_STATES or c in PREHEAT_STATES:
        return "cooking"
    if g in REST_STATES or c in REST_STATES:
        return "rest"
    if g in COOKING_STATES | PAUSED_STATES or c in COOKING_STATES | PAUSED_STATES:
        return "cooking"
    return "none"


def _cook_progress(state: CombinedState) -> int | None:
    """Cook-phase progress in percent (skips preheat).

    During preheat the firmware-reported value is preheat-progress, NOT
    cook progress — exposing it here would make the sensor jump 0→100→0
    when the phase flips. So preheat is excluded; use the dedicated
    preheat_progress sensor for that. Returns None when no cook is
    active so HA shows "unavailable" instead of "Unknown" / "0%".
    """
    phase = _cook_phase(state)
    if phase == "none":
        return None
    if phase == "preheat":
        # Cook hasn't started yet — keep this sensor at 0%.
        return 0
    # Cooking or rest: derive from elapsed/total cook time. Firmware's
    # cook.progress during rest is not a duration indicator, so we
    # always derive here.
    total = state.grill.seconds_set
    if total and total > 0:
        if state.grill.end_time_utc:
            left = max(0, state.grill.end_time_utc - datetime.now(tz=timezone.utc).timestamp())
        elif state.grill.seconds_left is not None:
            left = state.grill.seconds_left
        else:
            return None
        elapsed = max(0, total - left)
        return min(100, int(elapsed * 100 / total))
    return None


def _preheat_progress(state: CombinedState) -> int | None:
    """Preheat progress in percent. Only meaningful during preheat phase.

    Trusts the firmware value (cook.progress during state=preheat) over
    any derived calculation — preheat duration depends on starting temp
    and target temp, which the integration doesn't know.
    """
    if _cook_phase(state) != "preheat":
        return None
    return state.cook.progress


def _plausible_temp(state: CombinedState, raw: float | None, name: str = "grill") -> float | None:
    """Hide stale-cache, idle-cached, or mode-irrelevant readings.

    `name` is "grill", "air", or "smoke" — drives which physical
    chamber's gating rules apply (e.g. smoke chamber is only meaningful
    when smoke=on; air chamber only in air-crisp / bake / dehydrate).
    """
    return raw if state.temp_is_plausible(raw, name=name) else None


def _plausible_probe_temp(state: CombinedState, idx: int) -> float | None:
    """Hide stale probe readings.

    Probes have their own cache problem: the cloud reports the last-seen
    value forever after unplug. Mute when the probe is unplugged, when
    the grill isn't actively cooking, or when the value is implausibly
    hot for an idle probe.
    """
    if idx >= len(state.probes.probes):
        return None
    probe = state.probes.probes[idx]
    if not probe.plugged_in:
        return None
    raw = probe.temp
    if raw is None:
        return None
    if state.is_stale():
        return None
    # Idle grill + probe reading hotter than ambient = stale cache.
    # Probes report ambient ~20-30°C when plugged into a cold grill;
    # anything above 50 °C while idle is leftover from a prior cook.
    if state.grill.state.casefold() in IDLE_STATES and raw > 50:
        return None
    return raw


SENSORS: tuple[NinjaSensorDescription, ...] = (
    NinjaSensorDescription(
        key="grill_temp",
        translation_key="grill_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _plausible_temp(s, s.grill.temps.grill, "grill"),
    ),
    NinjaSensorDescription(
        key="air_temp",
        translation_key="air_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _plausible_temp(s, s.grill.temps.air, "air"),
    ),
    NinjaSensorDescription(
        key="smoke_temp",
        translation_key="smoke_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _plausible_temp(s, s.grill.temps.smoke, "smoke"),
    ),
    NinjaSensorDescription(
        key="grill_state",
        translation_key="grill_state",
        value_fn=lambda s: s.grill.state,
    ),
    NinjaSensorDescription(
        key="cook_state",
        translation_key="cook_state",
        value_fn=lambda s: s.cook.state,
    ),
    NinjaSensorDescription(
        key="probe0_temp",
        translation_key="probe0_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _plausible_probe_temp(s, 0),
    ),
    NinjaSensorDescription(
        key="probe1_temp",
        translation_key="probe1_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _plausible_probe_temp(s, 1),
    ),
    # Probe progress / setpoint are only meaningful while a probe-targeted
    # cook is running. When the probe is inactive, the firmware leaves
    # progress at 100% and target.setpoint as the last-set value, which
    # confuses the dashboard ("100%" + "Off" simultaneously). Return None
    # in that case so HA shows "unavailable".
    NinjaSensorDescription(
        key="probe0_progress",
        translation_key="probe0_progress",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            s.probes.probes[0].progress
            if len(s.probes.probes) > 0 and s.probes.probes[0].active
            else None
        ),
    ),
    NinjaSensorDescription(
        key="probe1_progress",
        translation_key="probe1_progress",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            s.probes.probes[1].progress
            if len(s.probes.probes) > 1 and s.probes.probes[1].active
            else None
        ),
    ),
    NinjaSensorDescription(
        key="probe0_setpoint",
        translation_key="probe0_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            s.probes.probes[0].target.setpoint
            if len(s.probes.probes) > 0 and s.probes.probes[0].active
            else None
        ),
    ),
    NinjaSensorDescription(
        key="probe1_setpoint",
        translation_key="probe1_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            s.probes.probes[1].target.setpoint
            if len(s.probes.probes) > 1 and s.probes.probes[1].active
            else None
        ),
    ),
    NinjaSensorDescription(
        key="probe0_state",
        translation_key="probe0_state",
        value_fn=lambda s: s.probes.probes[0].state if len(s.probes.probes) > 0 else None,
    ),
    NinjaSensorDescription(
        key="probe1_state",
        translation_key="probe1_state",
        value_fn=lambda s: s.probes.probes[1].state if len(s.probes.probes) > 1 else None,
    ),
    NinjaSensorDescription(
        key="event_mask",
        translation_key="event_mask",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.grill.event_mask,
    ),
    NinjaSensorDescription(
        key="prompt",
        translation_key="prompt",
        device_class=SensorDeviceClass.ENUM,
        options=list(PROMPTS),
        icon="mdi:message-alert",
        value_fn=lambda s: s.grill.prompt if s.grill.prompt in PROMPTS else None,
    ),
    NinjaSensorDescription(
        key="message",
        translation_key="message",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.grill.message or None,
    ),
    # ---- Display-mirror sensors (only meaningful while cooking) ----
    NinjaSensorDescription(
        key="active_mode",
        translation_key="active_mode",
        value_fn=lambda s: s.grill.mode,
    ),
    NinjaSensorDescription(
        key="setpoint",
        translation_key="setpoint",
        # Setpoint semantics depend on mode: in grill mode the firmware
        # reports an internal °C value (e.g. 205) that the user never sees
        # — the UI shows "Lo/Med/Hi". Translate it back so the dashboard
        # matches the device. For all other modes it's a real °C value.
        # Don't set device_class=temperature so HA doesn't render the
        # heat-level string as a temperature.
        value_fn=lambda s: _setpoint_display(s),
    ),
    NinjaSensorDescription(
        key="seconds_left",
        translation_key="seconds_left",
        # Diagnostic — `end_time` (timestamp) is the user-facing countdown;
        # this raw seconds value is for templates/automations only.
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="s",
        # Derived from endtimeutc rather than the snapshot `seconds left` field
        # so the value stays accurate between polls — same trick the app uses.
        # Falls back to the snapshot if endtimeutc isn't present.
        value_fn=lambda s: (
            max(0, int(s.grill.end_time_utc - datetime.now(tz=timezone.utc).timestamp()))
            if s.grill.end_time_utc
            else s.grill.seconds_left
        ),
    ),
    NinjaSensorDescription(
        key="end_time",
        translation_key="end_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: (
            datetime.fromtimestamp(s.grill.end_time_utc, tz=timezone.utc)
            if s.grill.end_time_utc
            else None
        ),
    ),
    NinjaSensorDescription(
        key="cook_progress",
        translation_key="cook_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
        value_fn=lambda s: _cook_progress(s),
        # Expose phase so a single template can render "Vorheizen 60%" vs
        # "Kochen 60%" without poking another sensor.
        attrs_fn=lambda s: {"phase": _cook_phase(s)},
    ),
    NinjaSensorDescription(
        key="preheat_progress",
        translation_key="preheat_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire-alert",
        value_fn=lambda s: _preheat_progress(s),
    ),
    NinjaSensorDescription(
        key="error_code",
        translation_key="error_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.grill.error if s.grill.error else None,
    ),
)


# Diagnostic sensors built from device metadata cached at setup time.
@dataclass(frozen=True, kw_only=True)
class NinjaDiagnosticDescription:
    key: str
    translation_key: str
    info_key: str   # which key in coordinator.device_info_extra to read
    icon: str | None = None


DIAGNOSTICS: tuple[NinjaDiagnosticDescription, ...] = (
    NinjaDiagnosticDescription(
        key="firmware_version",
        translation_key="firmware_version",
        info_key="sw_version",
        icon="mdi:chip",
    ),
    NinjaDiagnosticDescription(
        key="oem_model",
        translation_key="oem_model",
        info_key="oem_model",
        icon="mdi:tag",
    ),
    NinjaDiagnosticDescription(
        key="dsn",
        translation_key="dsn",
        info_key="dsn",
        icon="mdi:identifier",
    ),
    NinjaDiagnosticDescription(
        key="cloud_backend",
        translation_key="cloud_backend",
        info_key="backend",
        icon="mdi:cloud",
    ),
)


# Cloud and poll health, read from the coordinator rather than the grill state.
@dataclass(frozen=True, kw_only=True)
class NinjaCloudSensorDescription(SensorEntityDescription):
    value_fn: Callable[[NinjaWoodfireCoordinator], Any]
    attrs_fn: Callable[[NinjaWoodfireCoordinator], dict[str, Any]] | None = None
    requires_successful_poll: bool = False


CLOUD_SENSORS: tuple[NinjaCloudSensorDescription, ...] = (
    NinjaCloudSensorDescription(
        key="last_report",
        translation_key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.data.last_updated_at,
    ),
    NinjaCloudSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.rssi,
        requires_successful_poll=True,
    ),
    NinjaCloudSensorDescription(
        key="last_successful_poll",
        translation_key="last_successful_poll",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.last_success_at,
    ),
    NinjaCloudSensorDescription(
        key="failed_polls",
        translation_key="failed_polls",
        icon="mdi:cloud-alert",
        value_fn=lambda c: c.failed_polls,
        attrs_fn=lambda c: {"last_error": c.last_error},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NinjaWoodfireConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [NinjaSensor(coordinator, desc) for desc in SENSORS]
    entities.extend(NinjaDiagnosticSensor(coordinator, desc) for desc in DIAGNOSTICS)
    entities.extend(NinjaCloudSensor(coordinator, desc) for desc in CLOUD_SENSORS)
    async_add_entities(entities)


class NinjaSensor(NinjaWoodfireEntity, SensorEntity):
    entity_description: NinjaSensorDescription

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        if self.entity_description.key == "setpoint":
            c = self.coordinator
            return _setpoint_display(c.data, c.live_or_staged_temp if c.is_cook_active else None)
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        fn = self.entity_description.attrs_fn
        if fn is None:
            return None
        return fn(self.coordinator.data)


class NinjaDiagnosticSensor(NinjaWoodfireEntity, SensorEntity):
    """Static-info sensor (firmware version, model, etc.) — populated once at setup."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _requires_live_state = False
    _requires_successful_poll = False

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaDiagnosticDescription,
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"
        self._attr_icon = description.icon
        self._info_key = description.info_key

    @property
    def native_value(self) -> Any:
        if self._info_key in ("dsn", "backend"):
            return getattr(self.coordinator, self._info_key)
        return self.coordinator.device_info_extra.get(self._info_key) or None


class NinjaCloudSensor(NinjaWoodfireEntity, SensorEntity):
    entity_description: NinjaCloudSensorDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _requires_live_state = False

    def __init__(
        self,
        coordinator: NinjaWoodfireCoordinator,
        description: NinjaCloudSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._requires_successful_poll = description.requires_successful_poll
        self._attr_unique_id = f"{coordinator.dsn}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return None if fn is None else fn(self.coordinator)
