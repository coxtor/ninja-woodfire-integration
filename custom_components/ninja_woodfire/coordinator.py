"""DataUpdateCoordinator for the Ninja Woodfire grill."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._lib.api import GrillTransport
from ._lib.api.aws import AwsCloudClient, is_connected, state_from_device
from ._lib.api.ayla import AylaCloudClient
from ._lib.api.ble_beacon import parse_beacon
from ._lib.api.errors import AuthError, NinjaCloudError, TransportError
from ._lib.capabilities import GrillCapabilities, grill_level
from ._lib.models import CombinedState, ProbeInfo, ProbeMode, ProbeState

from .const import (
    ACTIVE_STATES,
    COOKING_STATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROMPT,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_TARGET_REACHED,
    IDLE_STATES,
    PAUSED_STATES,
    REST_STATES,
    WAITING_STATES,
    SCAN_INTERVAL_ACTIVE,
    SCAN_INTERVAL_IDLE,
)

_LOGGER = logging.getLogger(__name__)

# How long a just-sent command is trusted before the cloud reflects it.
_ISSUE_GRACE_S = 30
_LID_OPEN = frozenset({"lid open", "lid_open"})
_MIN_REMAINING_S = 60
# How often an offline grill is looked up on the other backend.
_AWS_RECHECK_S = 600
# How soon a failed first AWS lookup is retried.
_AWS_RETRY_S = 60
# How long to stay on cloud after a BLE failure before retrying the local link.
_BLE_RETRY_S = 60
# How long a passively-scanned advertisement beacon reading stays valid.
_BEACON_TTL_S = 60

type NinjaWoodfireConfigEntry = ConfigEntry[NinjaWoodfireCoordinator]


def is_active(state: CombinedState) -> bool:
    return state.grill.state in ACTIVE_STATES


def is_preheating(state: CombinedState) -> bool:
    return state.preheating


class NinjaWoodfireCoordinator(DataUpdateCoordinator[CombinedState]):
    """Polls the cloud for the full grill state and issues cook commands."""

    config_entry: NinjaWoodfireConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: AylaCloudClient | None,
        dsn: str,
        capabilities: GrillCapabilities,
        device_key: int,
        device_info_extra: dict[str, str] | None = None,
        aws: AwsCloudClient | None = None,
        ble: GrillTransport | None = None,
        beacon_slot: tuple[bytes, bytes] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} {dsn}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        # Ayla stays in use for setup and rename; state and cooks go through
        # `transport`, which follows the backend the grill is connected to.
        self.client = client
        self.aws = aws
        # Local BLE is preferred when configured; cloud (Ayla/AWS) is the fallback.
        self.ble = ble
        self.transport: GrillTransport = ble or client
        # Real grill serial (DSN) learned from BLE grill-info; falls back to dsn.
        self.grill_serial: str | None = None
        self._ble_recheck_at = 0.0
        self._aws_reached = False
        self._aws_failed = False
        self._backend_check_at = 0.0
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
        self._prev_prompt: str | None = None
        self._new_cook = False
        self._reset_cook_flags()
        self._offline_logged = False
        # Poll health, for the cloud diagnostic entities.
        self.last_success_at: datetime | None = None
        self.failed_polls = 0
        self.last_error: str | None = None
        # Meat-probe temps and lid state read passively from the BLE
        # advertisement beacon (the GATT 0x0f frame carries neither). Merged
        # into the local state.
        self.beacon_probes: list[float | None] = [None, None]
        self.beacon_lid: bool | None = None
        self.beacon_phase: str | None = None
        self.beacon_prompt: str | None = None
        # The beacon slot (key, nonce) that decrypts the advertisement; from the
        # pasted key bundle. None -> no beacon decode (probe/lid/phase stay unknown).
        self.beacon_slot = beacon_slot
        self._beacon_at: float = 0.0
        self.beacon_unsub = None

    # ------------------------------------------------------------------ polling

    async def _async_update_data(self) -> CombinedState:
        # Prefer local BLE when configured; drop to cloud on failure and retry
        # BLE periodically. The cloud ayla<->aws switch runs only on cloud polls.
        if self.ble is not None and self.transport is not self.ble \
                and time.monotonic() >= self._ble_recheck_at:
            self.transport = self.ble
        try:
            try:
                state = await self.transport.get_combined_state(self.dsn)
            except TransportError:
                # Fall back to the cloud only if we have a cloud client.
                if self.transport is self.ble and self.client is not None:
                    self._ble_recheck_at = time.monotonic() + _BLE_RETRY_S
                    _LOGGER.info("Grill %s BLE unavailable; using cloud", self.dsn)
                    self.transport = self.client
                    state = await self.transport.get_combined_state(self.dsn)
                else:
                    raise
            if self.transport is not self.ble and self._should_switch(state):
                switch = self._try_ayla if self.backend == "aws" else self._try_aws
                state = await switch(state)
        except AuthError as err:
            raise self._failed(ConfigEntryAuthFailed(str(err))) from err
        except TransportError as err:
            raise self._failed(UpdateFailed(f"transport: {err}")) from err
        self.last_success_at = dt_util.utcnow()
        self.failed_polls = 0
        self._normalize(state)
        self._merge_beacon(state)
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

    def _failed(self, err: Exception) -> Exception:
        self.failed_polls += 1
        self.last_error = str(err)
        return err

    # ------------------------------------------------------------- beacon (passive)

    @callback
    def update_beacon(self, mfg: bytes | None) -> None:
        """Store probe temps from a passively-scanned advertisement beacon.

        The meat-probe temperature is not in the GATT state frame; the grill
        broadcasts it in its advertisement. Called from a Bluetooth callback.
        """
        if self.beacon_slot is None:
            return
        parsed = parse_beacon(mfg, self.beacon_slot[0], self.beacon_slot[1])
        if parsed is None:
            return
        self.beacon_probes = [parsed["probe0_temp"], parsed["probe1_temp"]]
        self.beacon_lid = parsed["lid_open"]
        self.beacon_phase = parsed["phase"]
        self.beacon_prompt = parsed["prompt"]
        self._beacon_at = time.monotonic()
        # Push the fresh reading to entities without waiting for the next poll.
        if self.data is not None and self.backend == "ble":
            self._merge_beacon(self.data)
            self.async_update_listeners()

    def _merge_beacon(self, state: CombinedState) -> None:
        """Overlay beacon probe temps onto a local (BLE) state.

        Only the local path needs this; the cloud backends report probes
        themselves. Skipped when the beacon reading is stale or empty.
        """
        if self.backend != "ble":
            return
        if time.monotonic() - self._beacon_at > _BEACON_TTL_S:
            return
        # The lid switch, cook phase and display prompt are in the beacon too;
        # surface them even with no probe. The phase gives the local path its
        # only preheat signal (the GATT frame can't tell preheat from cooking),
        # which is what enables Skip preheat and the preheat-complete event.
        if self.beacon_lid is not None:
            state.grill.lid_open = self.beacon_lid
        if self.beacon_prompt is not None:
            state.grill.prompt = self.beacon_prompt
        # preheat (grill/air) and ignition (smoke) are both "still heating up",
        # the only local preheat signal; surface them unless add-food cleared it.
        if self.beacon_phase in ("preheat", "ignition") and self.beacon_prompt != "addfood":
            state.grill.state = self.beacon_phase
        if not any(t is not None for t in self.beacon_probes):
            return
        probes = []
        for idx, temp in enumerate(self.beacon_probes):
            plugged = temp is not None
            enabled = self.cook_setting_probe_enabled[idx]
            target = self.cook_setting_probe_setpoint[idx] if enabled else None
            probes.append(ProbeInfo(
                name=f"probe{idx}",
                plugged_in=plugged,
                active=plugged and enabled,
                temp=temp or 0.0,
                target=ProbeMode(mode="manual", setpoint=target) if target else ProbeMode(),
            ))
        state.probes = ProbeState(probes=probes)

    @callback
    def _async_refresh_finished(self) -> None:
        # HA skips listeners on repeated failures; the failure counter still moved.
        if not self.last_update_success and self.failed_polls > 1:
            self.async_update_listeners()

    @property
    def backend(self) -> str:
        if self.ble is not None and self.transport is self.ble:
            return "ble"
        return "ayla" if self.transport is self.client else "aws"

    def _should_switch(self, state: CombinedState) -> bool:
        """Until AWS answers once, then every few minutes while the grill is offline."""
        if self.aws is None or time.monotonic() < self._backend_check_at:
            return False
        return not state.online or (self.backend == "ayla" and not self._aws_reached)

    async def _try_aws(self, ayla: CombinedState) -> CombinedState:
        """Switch to AWS if the grill is connected there."""
        try:
            device = await self.aws.find_device(self.dsn)
        except TransportError as err:
            retry = _AWS_RECHECK_S if self._aws_failed else _AWS_RETRY_S
            self._backend_check_at = time.monotonic() + retry
            if not self._aws_failed:
                self._aws_failed = True
                _LOGGER.info("SharkNinja AWS backend unavailable (%s); using Ayla", err)
            return ayla
        self._aws_reached = True
        self._backend_check_at = time.monotonic() + _AWS_RECHECK_S
        if device is None or not is_connected(device):
            return ayla
        _LOGGER.info("Grill %s is on SharkNinja's AWS backend; using it", self.dsn)
        self.transport = self.aws
        return state_from_device(self.dsn, device)

    async def _try_ayla(self, aws: CombinedState) -> CombinedState:
        """Switch back to Ayla if the grill is offline on AWS but online there."""
        self._backend_check_at = time.monotonic() + _AWS_RECHECK_S
        try:
            ayla = await self.client.get_combined_state(self.dsn)
        except TransportError:
            return aws
        if str(ayla.connection_status).casefold() != "online":
            return aws
        _LOGGER.info("Grill %s is offline on AWS but online on Ayla; using Ayla", self.dsn)
        self.transport = self.client
        return ayla

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
            self._prev_prompt = state.grill.prompt
            self._seed_cook_flags(state)
            return

        fire = self.hass.bus.async_fire
        grill = state.grill
        common = {"dsn": self.dsn, "mode": grill.mode, "setpoint": grill.setpoint}
        active = g in ACTIVE_STATES

        if active and (prev not in ACTIVE_STATES or self._new_cook):
            self._new_cook = False
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

        if (g == "done" and prev != "done") or (prev in ACTIVE_STATES and g.casefold() in IDLE_STATES | WAITING_STATES):
            fire(EVENT_COOK_DONE, {**common, "reason": "done" if g == "done" else "stopped"})
            self._reset_cook_flags()

        prompt = grill.prompt
        if prompt and prompt != self._prev_prompt:
            fire(EVENT_PROMPT, {**common, "prompt": prompt})
        self._prev_prompt = prompt

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
        if self.client is None:
            raise HomeAssistantError("Not available in local-only (BLE) mode")
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
        await self._command(self.transport.start_cook(
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
        self._new_cook = True
        await self._burst_refresh()

    async def async_stop_cook(self) -> None:
        await self._command(self.transport.stop_cook(self.dsn))
        self._issued_level = None
        self._seconds_override = None
        await self._burst_refresh()

    async def async_wake(self) -> None:
        """Wake a grill that has gone to standby.

        A bare BLE connect does not wake the grill; sending a Stop command does
        (it beeps and comes back online). We only send Stop when no cook is
        running, so an active cook is never interrupted; then refresh.
        """
        if self.backend == "ble" and not self.is_cook_active:
            try:
                await self.transport.stop_cook(self.dsn)
            except (NinjaCloudError, TransportError):
                pass
        await self.async_request_refresh()

    async def async_skip_preheat(self) -> None:
        live = self.data
        if live is None or not live.online or not is_preheating(live):
            raise HomeAssistantError("The grill is not preheating")
        await self._reissue(live, skip_preheat=True)
        await self._burst_refresh()

    async def async_set_grill_name(self, new_name: str) -> None:
        if self.client is None:
            raise HomeAssistantError("Renaming needs a cloud account (local-only mode)")
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

        await self._command(self.transport.start_cook(
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
