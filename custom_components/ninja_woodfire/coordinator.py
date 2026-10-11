"""DataUpdateCoordinator for the Ninja Woodfire grill."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._lib.api.ayla import AuthError, AylaCloudClient, NinjaCloudError, TransportError
from ._lib.capabilities import GrillCapabilities, grill_level
from ._lib.models import CombinedState

from .const import (
    ACTIVE_STATES,
    COOKING_STATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_TARGET_REACHED,
    PAUSED_STATES,
    PREHEAT_STATES,
    REST_STATES,
    SCAN_INTERVAL_ACTIVE,
    SCAN_INTERVAL_IDLE,
)

_LOGGER = logging.getLogger(__name__)

# How long a just-sent command is trusted before the cloud reflects it.
_ISSUE_GRACE_S = 30
_LID_OPEN = frozenset({"lid open", "lid_open"})
_MIN_REMAINING_S = 60

type NinjaWoodfireConfigEntry = ConfigEntry[NinjaWoodfireCoordinator]


def is_active(state: CombinedState) -> bool:
    return state.grill.state in ACTIVE_STATES


def is_preheating(state: CombinedState) -> bool:
    return state.grill.state in PREHEAT_STATES or state.cook.state in PREHEAT_STATES


class NinjaWoodfireCoordinator(DataUpdateCoordinator[CombinedState]):
    """Polls the cloud for the full grill state and issues cook commands."""

    config_entry: NinjaWoodfireConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: AylaCloudClient,
        dsn: str,
        capabilities: GrillCapabilities,
        device_key: int,
        device_info_extra: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} {dsn}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.dsn = dsn
        self.capabilities = capabilities
        self._device_key = device_key
        self.device_info_extra = device_info_extra or {}
        # Staged cook settings, consumed by Start. While a cook is active the
        # live grill state takes precedence (see live_or_staged_*).
        default_mode = capabilities.modes[0]
        self.cook_setting_mode: str = default_mode.name
        self.cook_setting_temp: int = default_mode.temp_default
        self.cook_setting_seconds: int = default_mode.duration_default_s
        self.cook_setting_smoke: bool = default_mode.smoke_default
        self.cook_setting_skip_preheat: bool = False
        self.cook_setting_probe_enabled: list[bool] = [False, False]
        self.cook_setting_probe_setpoint: list[int] = [60, 60]
        # The firmware reports grill mode as °C and resets "seconds set" to
        # the remaining time on every re-issue. Remember what we sent so the
        # live state can be mapped back to what the user chose.
        self._issued_at: float = 0.0
        self._issued_level: tuple[int, int | None] | None = None
        self._seconds_override: tuple[int, int] | None = None
        # Lifecycle-event tracking.
        self._prev_grill_state: str | None = None
        self._reset_cook_flags()
        self._offline_logged = False

    # ------------------------------------------------------------------ polling

    async def _async_update_data(self) -> CombinedState:
        try:
            state = await self.client.get_combined_state(self.dsn)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TransportError as err:
            raise UpdateFailed(f"transport: {err}") from err
        self._normalize(state)
        active = (
            state.online
            and (is_active(state) or state.cook.state in ACTIVE_STATES)
            and not state.is_stale()
        )
        self.update_interval = SCAN_INTERVAL_ACTIVE if active else SCAN_INTERVAL_IDLE
        self._track_connection(state)
        if state.online:
            self._emit_lifecycle_events(state)
        else:
            self._prev_grill_state = None
        return state

    def _track_connection(self, state: CombinedState) -> None:
        if not state.online and not self._offline_logged:
            self._offline_logged = True
            _LOGGER.warning(
                "Grill %s is offline (cloud reports %r); its state is not live",
                self.dsn,
                state.connection_status,
            )
        elif state.online and self._offline_logged:
            self._offline_logged = False
            _LOGGER.info("Grill %s is back online", self.dsn)

    def _normalize(self, state: CombinedState) -> None:
        if not is_active(state):
            if time.monotonic() - self._issued_at > _ISSUE_GRACE_S:
                self._issued_level = None
                self._seconds_override = None
            return
        settled = time.monotonic() - self._issued_at > _ISSUE_GRACE_S
        if self._issued_level and state.grill.mode == "grill" and settled:
            level, seen = self._issued_level
            if seen is None:
                self._issued_level = (level, state.grill.setpoint)
            elif seen != state.grill.setpoint:
                self._issued_level = None
        if self._seconds_override and state.grill.seconds_set == self._seconds_override[0]:
            state.grill.seconds_set = self._seconds_override[1]

    async def _burst_refresh(self) -> None:
        """Poll a few times after a write so the UI catches up."""
        for delay in (0, 1.0, 1.5):
            await asyncio.sleep(delay)
            await self.async_refresh()

    # ------------------------------------------------------------------ events

    def _reset_cook_flags(self) -> None:
        self._seen_preheat = False
        self._preheat_fired = False
        self._halftime_fired = False
        self._probe_start_temp: list[float | None] = [None, None]
        self._probe_target: list[int | None] = [None, None]
        self._probe_halfway_fired = [False, False]
        self._probe_target_fired = [False, False]

    def _seed_cook_flags(self, state: CombinedState) -> None:
        """Mark milestones already passed so a restart doesn't re-fire them."""
        self._reset_cook_flags()
        if not is_active(state):
            return
        g = state.grill
        self._seen_preheat = is_preheating(state)
        self._preheat_fired = not self._seen_preheat
        self._halftime_fired = bool(
            g.seconds_set and g.seconds_left is not None and g.seconds_left <= g.seconds_set / 2
        )
        for idx, probe in enumerate(state.probes.probes[:2]):
            target = probe.target.setpoint
            if not probe.active or target is None:
                continue
            self._probe_target[idx] = target
            self._probe_start_temp[idx] = probe.temp
            reached = probe.temp >= target
            self._probe_halfway_fired[idx] = reached
            self._probe_target_fired[idx] = reached

    def _emit_lifecycle_events(self, state: CombinedState) -> None:
        """Fire HA events on cook-lifecycle transitions (one per poll)."""
        g, c = state.grill.state, state.cook.state
        if g == "unknown":
            return
        prev = self._prev_grill_state
        self._prev_grill_state = g
        if prev is None:
            self._seed_cook_flags(state)
            return

        fire = self.hass.bus.async_fire
        grill = state.grill
        common = {"dsn": self.dsn, "mode": grill.mode, "setpoint": grill.setpoint}
        active = g in ACTIVE_STATES

        if active and prev not in ACTIVE_STATES:
            self._reset_cook_flags()
            fire(EVENT_COOK_STARTED, {**common, "seconds_set": grill.seconds_set, "smoke": grill.smoke})

        if active and is_preheating(state):
            self._seen_preheat = True
        elif (
            active
            and self._seen_preheat
            and not self._preheat_fired
            and g not in _LID_OPEN
            and c not in _LID_OPEN
            and (g in COOKING_STATES | PAUSED_STATES or c in COOKING_STATES | PAUSED_STATES)
        ):
            self._preheat_fired = True
            fire(EVENT_PREHEAT_COMPLETE, common)

        if (
            active
            and not self._halftime_fired
            and grill.seconds_set
            and grill.seconds_left is not None
            and grill.seconds_left <= grill.seconds_set / 2
        ):
            self._halftime_fired = True
            fire(
                EVENT_COOK_HALFTIME,
                {**common, "seconds_left": grill.seconds_left, "seconds_set": grill.seconds_set},
            )

        if (g == "done" and prev != "done") or (prev in ACTIVE_STATES and g == "idle"):
            fire(EVENT_COOK_DONE, {**common, "reason": "done" if g == "done" else "stopped"})
            self._reset_cook_flags()

        if not active:
            return
        for idx, probe in enumerate(state.probes.probes[:2]):
            target = probe.target.setpoint
            if not probe.active or target is None:
                continue
            if self._probe_target[idx] != target:
                self._probe_target[idx] = target
                self._probe_start_temp[idx] = probe.temp
                self._probe_halfway_fired[idx] = False
                self._probe_target_fired[idx] = False
            start = self._probe_start_temp[idx]
            base = {"dsn": self.dsn, "probe_index": idx, "target": target, "current": probe.temp}
            halfway = start + (target - start) / 2
            if not self._probe_halfway_fired[idx] and start < target and probe.temp >= halfway:
                self._probe_halfway_fired[idx] = True
                fire(EVENT_PROBE_HALFWAY, {**base, "halfway": halfway})
            if not self._probe_target_fired[idx] and probe.temp >= target:
                self._probe_target_fired[idx] = True
                fire(EVENT_PROBE_TARGET_REACHED, base)

    # ------------------------------------------------------------------ live state

    @property
    def state_online(self) -> bool:
        return self.data is not None and self.data.online

    @property
    def is_cook_active(self) -> bool:
        return self.state_online and is_active(self.data)

    def _live_temp(self, state: CombinedState) -> int | None:
        if state.grill.mode == "grill":
            if self._issued_level:
                return self._issued_level[0]
            return grill_level(state.grill.setpoint)
        return state.grill.setpoint

    def _remaining_seconds(self, state: CombinedState) -> int:
        g = state.grill
        if g.end_time_utc:
            remaining = int(g.end_time_utc - time.time())
            if remaining > 0:
                return remaining
        if g.end_time_utc or g.seconds_left is not None:
            return g.seconds_left or 0
        return g.seconds_set or 0

    def _live_probe(self, state: CombinedState, idx: int) -> dict[str, Any] | None:
        probes = state.probes.probes
        if idx < len(probes) and probes[idx].active and probes[idx].target.setpoint:
            return self._build_probe_payload(probes[idx].target.setpoint)
        return None

    @property
    def live_or_staged_mode(self) -> str:
        if self.is_cook_active and self.data.grill.mode:
            return self.data.grill.mode
        return self.cook_setting_mode

    @property
    def live_or_staged_temp(self) -> int:
        if self.is_cook_active:
            live = self._live_temp(self.data)
            if live is not None:
                return int(live)
        return self.cook_setting_temp

    @property
    def live_or_staged_seconds(self) -> int:
        """Total cook duration."""
        if self.is_cook_active and self.data.grill.seconds_set:
            return int(self.data.grill.seconds_set)
        return self.cook_setting_seconds

    @property
    def live_or_staged_smoke(self) -> bool:
        if self.is_cook_active:
            return bool(self.data.grill.smoke)
        return self.cook_setting_smoke

    def live_or_staged_probe_enabled(self, idx: int) -> bool:
        if self.is_cook_active:
            return self._live_probe(self.data, idx) is not None
        return self.cook_setting_probe_enabled[idx]

    def live_or_staged_probe_setpoint(self, idx: int) -> int:
        if self.is_cook_active and (live := self._live_probe(self.data, idx)):
            return live["setpoint"]
        return self.cook_setting_probe_setpoint[idx]

    # ------------------------------------------------------------------ commands

    @staticmethod
    def _build_probe_payload(setpoint: int) -> dict[str, Any]:
        return {"mode": "manual", "setpoint": int(setpoint)}

    def _remember_issue(self, mode: str, temp: int, seconds: int, total: int) -> None:
        self._issued_at = time.monotonic()
        self._issued_level = (int(temp), None) if mode == "grill" else None
        self._seconds_override = (int(seconds), int(total)) if total != seconds else None

    @staticmethod
    async def _command(coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except (NinjaCloudError, ValueError) as err:
            raise HomeAssistantError(f"Grill command failed: {err}") from err

    async def async_set_property(self, name: str, value) -> None:
        """Write a settable cloud property."""
        await self._command(self.client.set_property_datapoint(self.dsn, name, value))
        await self._burst_refresh()

    async def async_start_cook(self) -> None:
        """Send the staged cook settings to the grill."""
        if self.data is not None and not self.data.online:
            raise HomeAssistantError("The grill is offline")
        mode = self.capabilities.get_mode(self.cook_setting_mode)
        if mode is None:
            raise HomeAssistantError(
                f"mode {self.cook_setting_mode!r} not supported by "
                f"{self.capabilities.display_name}"
            )
        probes = [
            self._build_probe_payload(self.cook_setting_probe_setpoint[idx])
            if self.cook_setting_probe_enabled[idx]
            and mode.supports_probe
            and (idx == 0 or self.capabilities.has_two_probes)
            else None
            for idx in (0, 1)
        ]
        temp = max(mode.temp_min, min(mode.temp_max, self.cook_setting_temp))
        seconds = max(mode.duration_min_s, min(mode.duration_max_s, self.cook_setting_seconds))
        await self._command(self.client.start_cook(
            self.dsn,
            mode=mode.name,
            seconds=seconds,
            temp=temp,
            smoke=self.cook_setting_smoke and mode.supports_smoke,
            skip_preheat=self.cook_setting_skip_preheat,
            probe_0=probes[0],
            probe_1=probes[1],
            device_key=self._device_key,
        ))
        self._remember_issue(mode.name, temp, seconds, seconds)
        await self._burst_refresh()

    async def async_stop_cook(self) -> None:
        await self._command(self.client.stop_cook(self.dsn))
        self._issued_level = None
        self._seconds_override = None
        await self._burst_refresh()

    async def async_skip_preheat(self) -> None:
        live = self.data
        if live is None or not live.online or not is_preheating(live):
            raise HomeAssistantError("The grill is not preheating")
        await self._reissue(live, skip_preheat=True)
        await self._burst_refresh()

    async def async_set_grill_name(self, new_name: str) -> None:
        await self._command(self.client.set_grill_name(self.dsn, new_name))
        await self._burst_refresh()

    async def async_modify_cook(
        self,
        *,
        mode: str | None = None,
        temp: int | None = None,
        seconds: int | None = None,
        smoke: bool | None = None,
    ) -> None:
        """Change one or more cook settings.

        Idle: stages the value for the next Start. Active: re-issues the
        running cook with the change applied, keeping heat level, probe
        targets and elapsed time.
        """
        if mode is not None:
            self.cook_setting_mode = mode
        if temp is not None:
            self.cook_setting_temp = temp
        if seconds is not None:
            self.cook_setting_seconds = seconds
        if smoke is not None:
            self.cook_setting_smoke = smoke

        live = self.data
        if not self.is_cook_active or (mode, temp, seconds, smoke) == (None,) * 4:
            self.async_update_listeners()
            return
        await self._reissue(
            live,
            mode=mode,
            temp=temp,
            seconds=seconds,
            smoke=smoke,
            skip_preheat=not is_preheating(live),
        )
        await self._burst_refresh()

    async def async_set_probe(
        self, idx: int, *, enabled: bool | None = None, setpoint: int | None = None
    ) -> None:
        """Change a probe target; applied live while a cook is running."""
        live = self.data
        active = self.is_cook_active
        if active:
            if enabled is None:
                enabled = self.live_or_staged_probe_enabled(idx)
            if setpoint is None:
                setpoint = self.live_or_staged_probe_setpoint(idx)
        if enabled is not None:
            self.cook_setting_probe_enabled[idx] = enabled
        if setpoint is not None:
            self.cook_setting_probe_setpoint[idx] = int(setpoint)
        if not active:
            self.async_update_listeners()
            return
        payload = self._build_probe_payload(setpoint) if enabled else None
        if payload == self._live_probe(live, idx):
            self.async_update_listeners()
            return
        await self._reissue(live, probes={idx: payload}, skip_preheat=not is_preheating(live))
        await self._burst_refresh()

    async def _reissue(
        self,
        live: CombinedState,
        *,
        mode: str | None = None,
        temp: int | None = None,
        seconds: int | None = None,
        smoke: bool | None = None,
        probes: dict[int, dict[str, Any] | None] | None = None,
        skip_preheat: bool,
    ) -> None:
        """Re-send the running cook from live state with the given overrides."""
        new_mode = mode or live.grill.mode or self.cook_setting_mode
        new_temp = temp if temp is not None else self._live_temp(live)
        if new_temp is None:
            new_temp = self.cook_setting_temp
        new_smoke = smoke if smoke is not None else bool(live.grill.smoke)

        if live.grill.state in REST_STATES or live.cook.state in REST_STATES:
            raise HomeAssistantError("The cook is finished and resting")
        remaining = self._remaining_seconds(live)
        if remaining <= 0 and is_preheating(live):
            remaining = live.grill.seconds_set or 0
        if remaining <= 0:
            raise HomeAssistantError("The cook timer has run out")
        elapsed = max(0, (live.grill.seconds_set or remaining) - remaining)
        if seconds is not None and seconds < elapsed + _MIN_REMAINING_S:
            raise HomeAssistantError(
                f"Duration must exceed the {elapsed // 60} min already cooked"
            )
        new_seconds = seconds - elapsed if seconds is not None else remaining

        payloads = [self._live_probe(live, 0), self._live_probe(live, 1)]
        for idx, payload in (probes or {}).items():
            payloads[idx] = payload

        caps = self.capabilities.get_mode(new_mode)
        if caps is not None:
            if not caps.temp_min <= new_temp <= caps.temp_max:
                new_temp = caps.temp_default
            new_smoke = new_smoke and caps.supports_smoke
            new_seconds = max(caps.duration_min_s, min(caps.duration_max_s, new_seconds))
            if not caps.supports_probe:
                payloads = [None, None]
        new_seconds = max(_MIN_REMAINING_S, new_seconds)

        await self._command(self.client.start_cook(
            self.dsn,
            mode=new_mode,
            seconds=int(new_seconds),
            temp=int(new_temp),
            smoke=new_smoke,
            skip_preheat=skip_preheat,
            probe_0=payloads[0],
            probe_1=payloads[1],
            device_key=self._device_key,
        ))
        self._remember_issue(new_mode, new_temp, new_seconds, elapsed + new_seconds)
        self.cook_setting_mode = new_mode
        self.cook_setting_temp = int(new_temp)
        self.cook_setting_seconds = int(elapsed + new_seconds)
        self.cook_setting_smoke = new_smoke
