"""DataUpdateCoordinator for the Ninja Woodfire grill."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._lib.api import GrillTransport
from ._lib.api.aws import AwsCloudClient, is_connected, state_from_device
from ._lib.api.ayla import AylaCloudClient
from ._lib.api.ble_beacon import parse_beacon
from ._lib.api.errors import AuthError, NinjaCloudError, TransportError
from ._lib.capabilities import (
    PROBE_TARGET_MAX_C,
    PROBE_TARGET_MIN_C,
    GrillCapabilities,
    grill_level,
)
from ._lib.models import CombinedState, CookState, GrillState, ProbeInfo, ProbeMode, ProbeState

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
    EVENT_START_NOT_CONFIRMED,
    START_CONFIRM_GRACE,
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
# States that positively confirm standby. "unknown" / "" are the absence of a
# reading, not a confirmed idle grill, so they must never trigger a Wake stop.
_STANDBY_STATES = IDLE_STATES - {"unknown", ""}
# How often an offline grill is looked up on the other backend.
_AWS_RECHECK_S = 600
# How soon a failed first AWS lookup is retried.
_AWS_RETRY_S = 60
# How long to stay on cloud after a BLE failure before retrying the local link.
_BLE_RETRY_S = 60
# How long a passively-scanned advertisement beacon reading stays valid.
_BEACON_TTL_S = 60
# No beacon AND no successful connect for this long -> the grill is off / out of
# range. Shorter outages keep the last state (smooths transient radio glitches).
_OFFLINE_AFTER_S = 60
# Beacon display-prompt -> the grill's current cook step. Takes priority over the
# coarse phase so the card/state reflect what the grill display actually shows.
_PROMPT_STATE = {
    "addfood": "insert_food",   # preheat done, waiting for food (not yet cooking)
    "flipfood": "flip",
    "getfood": "get_food",
    "done": "done",
}

type NinjaWoodfireConfigEntry = ConfigEntry[NinjaWoodfireCoordinator]


def is_active(state: CombinedState) -> bool:
    return state.grill.state in ACTIVE_STATES


def is_preheating(state: CombinedState) -> bool:
    return state.preheating


def timer_running(state: CombinedState) -> bool:
    """Whether the grill's cook timer is counting down right now.

    Preheat and the wait steps inside a cook (add food, flip, lid open) are
    active, but no cook time passes in them.
    """
    if is_preheating(state):
        return False
    g, c = state.grill.state.casefold(), state.cook.state.casefold()
    if g in PAUSED_STATES or c in PAUSED_STATES or g in WAITING_STATES:
        return False
    return g in COOKING_STATES or c in COOKING_STATES


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
        # The cook we last sent. Every command re-sends all parameters, so a
        # follow-up change must merge against this rather than the live
        # snapshot, which lags a poll behind. None once the backend caught up.
        self._pending: dict[str, Any] | None = None
        # Set right when Stop is issued. The grill's passive beacon lags the
        # actual command by a few seconds, so a merge within this window must
        # not let a stale "still cooking" beacon revert the optimistic idle.
        self._stop_issued_at: float | None = None
        # One command at a time; a Stop pre-empts whatever is still in flight.
        self._cmd_lock = asyncio.Lock()
        self._cmd_task: asyncio.Task | None = None
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
        # Last time we heard anything from the grill (beacon or a successful
        # connect). Drives the "offline / powered off" indication over BLE.
        self._last_contact_m: float = 0.0
        # Last known BLE reachability, independent of which backend is
        # currently serving poll data (e.g. still updated during a cloud
        # fallback's periodic BLE recheck). None until BLE has been tried once;
        # stays at its last value on polls that don't touch BLE at all.
        self._ble_last_online: bool | None = None
        self._ble_last_attempt: datetime | None = None
        # Local cook-timer estimate for the BLE path (the GATT timer frame is
        # usually absent): when cooking started, counting down the configured
        # duration. None while not cooking.
        self._local_cook_end: datetime | None = None
        # Skip-preheat toggle: the firmware ignores skip=1 on the initial cook;
        # it only skips when re-sent during preheat (what the Skip button does).
        # So when the toggle is on we auto-fire that skip once preheat is seen.
        self._auto_skip_pending: bool = False
        # Have we actually seen a cook phase since the last idle/stop? Gates
        # trusting beacon display-prompts, so a stale idle prompt can't fabricate
        # a cook. Reset when the cook ends / the grill goes offline.
        self._cook_seen: bool = False
        # Watches a local BLE start_cook for independent confirmation (see
        # _verify_start_confirmed); cancelled by a later start/stop so a stale
        # check can't fire after the situation has already moved on.
        self._start_verify_task: asyncio.Task | None = None
        # Best-effort "the grill is probably asleep" signal: set when a start
        # goes unconfirmed (see _verify_start_confirmed), cleared once an
        # active phase is genuinely confirmed or the user presses Wake. The
        # card uses this to offer Wake instead of Start before the next try.
        self._likely_asleep: bool = False
        self.beacon_unsub = None

    # ------------------------------------------------------------------ polling

    async def _async_update_data(self) -> CombinedState:
        # Prefer local BLE when configured; drop to cloud on failure and retry
        # BLE periodically. The cloud ayla<->aws switch runs only on cloud polls.
        if self.ble is not None and self.transport is not self.ble \
                and time.monotonic() >= self._ble_recheck_at:
            self.transport = self.ble
        real = True
        try:
            try:
                state = await self.transport.get_combined_state(self.dsn)
                if self.transport is self.ble:
                    self._ble_last_online = True
                    self._ble_last_attempt = dt_util.utcnow()
            except TransportError:
                if self.transport is self.ble:
                    self._ble_last_attempt = dt_util.utcnow()
                    # BLE itself just failed this poll — true independent of
                    # whether we go on to fall back to cloud below, so the
                    # "BLE connection" diagnostic reflects the radio, not
                    # whichever backend ends up serving this poll's data.
                    self._ble_last_online = False
                # Fall back to the cloud only if we have a cloud client.
                if self.transport is self.ble and self.client is not None:
                    self._ble_recheck_at = time.monotonic() + _BLE_RETRY_S
                    _LOGGER.info("Grill %s BLE unavailable; using cloud", self.dsn)
                    self.transport = self.client
                    state = await self.transport.get_combined_state(self.dsn)
                elif self.transport is self.ble:
                    # Local-only: a couldn't-connect is normal when the grill is
                    # off or briefly out of range. Keep the last state for a short
                    # while (smooth glitches), then report it cleanly offline —
                    # don't flap entities to "unavailable".
                    state = self._ble_offline_or_last()
                    real = False
                else:
                    raise
            if self.transport is not self.ble and self._should_switch(state):
                switch = self._try_ayla if self.backend == "aws" else self._try_aws
                state = await switch(state)
        except AuthError as err:
            raise self._failed(ConfigEntryAuthFailed(str(err))) from err
        except TransportError as err:
            raise self._failed(UpdateFailed(f"transport: {err}")) from err
        if real:
            self.last_success_at = dt_util.utcnow()
            self.failed_polls = 0
            # Recovered: don't keep showing a resolved failure as the error.
            self.last_error = None
            self._last_contact_m = time.monotonic()
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
            self._cook_seen = False
        # Skip-preheat toggle: re-issue the skip (the mechanism that works) as
        # soon as the grill is actually preheating.
        if self._auto_skip_pending and state.online and is_preheating(state):
            self._auto_skip_pending = False
            self.hass.async_create_task(self.async_skip_preheat())
        return state

    def _failed(self, err: Exception) -> Exception:
        self.failed_polls += 1
        self.last_error = str(err)
        return err

    def _ble_offline_or_last(self) -> CombinedState:
        """State to use when a local BLE poll couldn't reach the grill.

        A fresh beacon counts as contact (a grill that still advertises but
        refuses a GATT connection is reachable, not off). While we've had contact
        recently, keep the last known state so a brief radio glitch doesn't flap
        the card. Once there's been no contact (beacon or connect) for a while,
        report the grill cleanly offline ("powered off") so the card shows Wake
        instead of every entity going "unavailable".
        """
        if self.data is not None and time.monotonic() - self._last_contact_m <= _OFFLINE_AFTER_S:
            return self.data
        return CombinedState(
            dsn=self.dsn,
            grill=GrillState(state="powered_off"),
            cook=CookState(),
            probes=ProbeState(),
            online=False,
            connection_status="Offline",
            last_updated_at=dt_util.utcnow(),
        )

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
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Beacon decoded: phase=%r lid=%r prompt=%r probes=%r",
                parsed["phase"], parsed["lid_open"], parsed["prompt"], self.beacon_probes,
            )
        self._beacon_at = time.monotonic()
        self._last_contact_m = self._beacon_at   # a beacon means the grill is reachable
        # Push the fresh reading to entities without waiting for the next poll.
        if self.data is not None and self.backend == "ble":
            self._merge_beacon(self.data)
            # Prompts (add food / flip / get food) and phase changes live in the
            # beacon and are often transient — the slow poll misses them. Fire
            # lifecycle events from the beacon push too so they reliably reach
            # automations / Node-RED.
            if self.data.online:
                self._emit_lifecycle_events(self.data)
            self.async_update_listeners()

    def _merge_beacon(self, state: CombinedState) -> None:
        """Overlay beacon probe temps onto a local (BLE) state.

        Only the local path needs this; the cloud backends report probes
        themselves. Skipped when the beacon reading is stale or empty.
        """
        if self.backend != "ble":
            return
        if time.monotonic() - self._beacon_at > _BEACON_TTL_S:
            # No live beacon: we can't tell cooking from idle from the
            # advertisement. If the GATT frame also gave nothing ("unknown" —
            # a BLE connect can succeed while the grill is asleep, it just
            # won't report state or advertise), the grill is most likely
            # asleep rather than idle. Show that as "powered off" so the card
            # offers Wake instead of a Start that the firmware will ignore.
            if state.grill.state == "unknown":
                state.grill.state = "powered_off"
            # Nothing received passively for a while: stop presenting the last
            # beacon's probe / lid / prompt readings as if they were current.
            state.grill.prompt = None
            state.grill.lid_open = None
            state.probes = ProbeState()
            return
        # The lid switch, cook phase and display prompt are in the beacon too;
        # surface them even with no probe. The phase gives the local path its
        # only preheat signal (the GATT frame can't tell preheat from cooking),
        # which is what enables Skip preheat and the preheat-complete event.
        if self.beacon_lid is not None:
            state.grill.lid_open = self.beacon_lid
        # The grill's passive beacon lags an issued Stop by a few seconds (it
        # keeps advertising the old phase for a bit). Ignore it for a grace
        # window so the optimistic idle Stop just set doesn't flicker back to
        # "cooking" before the beacon catches up.
        in_stop_grace = (
            self._stop_issued_at is not None
            and time.monotonic() - self._stop_issued_at <= _ISSUE_GRACE_S
        )
        # Only trust a display prompt once we've actually seen the grill in a cook
        # phase. An idle grill can broadcast a stale/left-over prompt (e.g. "get
        # food") in its beacon; honoring it would fabricate an active state and
        # fire spurious prompt / cook-done events with no cook running.
        if not in_stop_grace and self.beacon_phase in ("cooking", "preheat", "ignition"):
            self._cook_seen = True
            self._likely_asleep = False
        # A fresh beacon is a full snapshot of its own fields: a cleared prompt
        # has to clear the state too, or the display step would stick forever
        # (and the same prompt could never fire a second event).
        prompt = self.beacon_prompt if self._cook_seen and not in_stop_grace else None
        state.grill.prompt = prompt
        # Surface the cook phase the beacon reports. Over BLE the GATT state
        # frame is usually absent (idle grills don't stream it), so the beacon is
        # often the only live phase signal: "cooking" lets the card show
        # "Grilling" instead of Unknown; preheat/ignition are the only local
        # preheat signal (enables Skip preheat / the preheat-complete event).
        # A live prompt is the grill's current step and takes priority over the
        # coarse phase: "add food" (preheat done, waiting for food), flip, get
        # food, done. This also keeps "cooking" — and thus the cook timer — from
        # starting until the prompt clears (food actually went in).
        prompt_state = _PROMPT_STATE.get(prompt)
        if in_stop_grace:
            # We know the ground truth here: Stop was just issued. Pin idle
            # outright rather than merely skipping the promotion — a fresh
            # poll in this window builds a brand new CombinedState whose
            # default grill.state is "unknown" (no 0x0f frame), and leaving
            # that alone would strand the card on "unknown" for the whole
            # grace window instead of showing idle.
            state.grill.state = "idle"
        elif prompt_state is not None:
            state.grill.state = prompt_state
        elif self.beacon_phase == "cooking":
            state.grill.state = "cooking"
        elif self.beacon_phase in ("preheat", "ignition"):
            state.grill.state = self.beacon_phase
        elif self.beacon_lid and self._cook_seen:
            # Opening the lid mid-cook: the firmware's beacon phase bits read
            # blank/idle while paused, which would otherwise look like the
            # cook ending (and restarting when the lid closes again). Report
            # the same "lid_open" paused state the cloud path already uses
            # (in ACTIVE_STATES) so a lid flap during a cook can't be
            # mistaken for a stop/restart.
            state.grill.state = "lid_open"
        elif self.beacon_phase is None and not is_active(state):
            # A fresh beacon with no active phase means the grill is on but idle.
            # Pin it to "idle" so the card shows a stable "Ready" instead of
            # flapping to Unknown whenever the GATT state frame is absent.
            state.grill.state = "idle"
        # Without the GATT frame the mode is blank; show the configured cook so
        # the card reflects the running cook instead of an empty header.
        if state.grill.state in ("cooking", "preheat", "ignition") and not state.grill.mode:
            state.grill.mode = self.cook_setting_mode
        # Local cook-timer estimate. The GATT timer frame is usually absent over
        # BLE, so once the grill is cooking we count down the configured duration
        # (the firmware's cook timer also only starts after preheat). A real 0x0f
        # timer always wins; only our own estimate is recomputed — on every
        # merge, so it keeps counting down and follows a live duration change
        # instead of freezing at the value first written here.
        real_timer = bool(state.grill.seconds_set) and not state.grill.timer_estimated
        if state.grill.state == "cooking" and not real_timer:
            # Start the countdown only once the lid is closed after loading — the
            # firmware's cook timer starts then too, not the instant preheat ends
            # / skip is pressed. Once started, keep counting through lid flaps.
            if self._local_cook_end is None and not state.grill.lid_open:
                self._local_cook_end = dt_util.utcnow() + timedelta(seconds=self.cook_setting_seconds)
            if self._local_cook_end is not None:
                left = max(0, int((self._local_cook_end - dt_util.utcnow()).total_seconds()))
                state.grill.seconds_set = self.cook_setting_seconds
                state.grill.seconds_left = left
                state.grill.end_time_utc = int(self._local_cook_end.timestamp())
                state.grill.timer_estimated = True
        elif state.grill.state not in ACTIVE_STATES:
            # Only reset once the cook has actually ended (idle/done/off) —
            # NOT for a mid-cook pause (lid_open/flip/get_food), or a lid flap
            # or flip prompt would wipe the running timer and restart it from
            # the full duration once cooking resumes.
            self._local_cook_end = None
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
                "Grill %s is offline (%s reports %r); its state is not live",
                self.dsn,
                self.backend,
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
                self._pending = None
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
        self._reconcile_pending(state)

    def _reconcile_pending(self, state: CombinedState) -> None:
        """Take a fresh poll's cook timer into the pending command.

        The pending snapshot dates from when the command went out; the grill
        may have left preheat or paused since. Only the timer is adopted
        (normalised back to the issue time, which `_cook_base` ages from); the
        parameters stay as sent until the backend reflects them.
        """
        pending = self._pending_cook()
        if pending is None or state.grill.seconds_set != pending["total"]:
            return
        running = timer_running(state)
        since = int(time.monotonic() - self._issued_at) if running else 0
        pending["remaining"] = self._live_remaining(state) + since
        pending["timer_running"] = running

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
            self._cook_seen = False

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

    def _is_standby(self) -> bool:
        """Whether the grill positively reports standby/idle (never a guess)."""
        if self.data is None:
            return False
        return self.data.grill.state.casefold() in _STANDBY_STATES

    @property
    def ble_reachable(self) -> bool | None:
        """Outcome of the last GATT connection attempt.

        This is the *connection*, not passive reception: the grill can still be
        broadcasting decodable beacons (see `beacon_age_seconds`) while GATT
        refuses. None if BLE isn't configured or hasn't been tried yet.
        """
        return self._ble_last_online

    @property
    def ble_last_attempt(self) -> datetime | None:
        """When the GATT connection `ble_reachable` refers to was attempted."""
        return self._ble_last_attempt

    @property
    def beacon_age_seconds(self) -> float | None:
        """Seconds since the last decoded advertisement beacon, if any."""
        if not self._beacon_at:
            return None
        return round(time.monotonic() - self._beacon_at, 1)

    def _pending_cook(self) -> dict[str, Any] | None:
        """The cook we last sent, while the backend hasn't reflected it yet."""
        if self._pending is None or time.monotonic() - self._issued_at > _ISSUE_GRACE_S:
            return None
        return self._pending

    def _live_temp(self, state: CombinedState) -> int | None:
        if (pending := self._pending_cook()) is not None:
            return pending["temp"]
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

    def _live_remaining(self, live: CombinedState) -> int:
        """Cook time left according to the grill itself."""
        remaining = self._remaining_seconds(live)
        if remaining <= 0 and is_preheating(live):
            # Preheat hasn't started the cook timer yet; over BLE there's no live
            # timer at all. Use the configured full duration (the firmware's cook
            # timer also only starts once preheat ends).
            remaining = live.grill.seconds_set or self.cook_setting_seconds or 0
        return remaining

    def _live_probe(self, state: CombinedState, idx: int) -> dict[str, Any] | None:
        if (pending := self._pending_cook()) is not None:
            return pending["probes"][idx]
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
        if self.is_cook_active:
            if (pending := self._pending_cook()) is not None:
                return int(pending["total"])
            if self.data.grill.seconds_set:
                return int(self.data.grill.seconds_set)
        return self.cook_setting_seconds

    @property
    def live_or_staged_smoke(self) -> bool:
        if self.is_cook_active:
            if (pending := self._pending_cook()) is not None:
                return bool(pending["smoke"])
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

    def _remember_issue(
        self,
        mode: str,
        temp: int,
        seconds: int,
        total: int,
        *,
        smoke: bool,
        probes: list[dict[str, Any] | None],
        timer_running: bool,
    ) -> None:
        self._issued_at = time.monotonic()
        self._issued_level = (int(temp), None) if mode == "grill" else None
        self._seconds_override = (int(seconds), int(total)) if total != seconds else None
        self._pending = {
            "mode": mode,
            "temp": int(temp),
            "smoke": bool(smoke),
            "probes": list(probes),
            "remaining": int(seconds),
            "total": int(total),
            # Whether the cook timer was counting down when we sent this. Only
            # then does wall-clock time since spend any of the cook duration.
            "timer_running": timer_running,
        }

    def _cook_base(self, live: CombinedState) -> dict[str, Any]:
        """What a re-issue builds on.

        While a command is still pending that is the cook we just sent (the
        live snapshot is a poll behind and would revert it); otherwise the
        live state.
        """
        if (pending := self._pending_cook()) is not None:
            # Preheat, add-food and lid-open steps don't spend cook time: only
            # count the seconds since the command when the timer was running.
            since = int(time.monotonic() - self._issued_at) if pending["timer_running"] else 0
            remaining = max(0, pending["remaining"] - since)
            return {
                **pending,
                "remaining": remaining,
                "elapsed": max(0, pending["total"] - remaining),
            }
        remaining = self._live_remaining(live)
        total = max(live.grill.seconds_set or 0, remaining)
        temp = self._live_temp(live)
        return {
            "mode": live.grill.mode or self.cook_setting_mode,
            "temp": self.cook_setting_temp if temp is None else temp,
            "smoke": bool(live.grill.smoke),
            "probes": [self._live_probe(live, 0), self._live_probe(live, 1)],
            "remaining": remaining,
            "elapsed": max(0, total - remaining),
            "total": total,
        }

    @asynccontextmanager
    async def _exclusive(self, *, preempt: bool = False) -> AsyncIterator[None]:
        """Serialize command sends so they can't race on a stale snapshot.

        `preempt` (Stop) cancels whatever command is still in flight, so a
        long-running start can never land after the user stopped the cook.
        """
        if preempt and self._cmd_task is not None and not self._cmd_task.done():
            self._cmd_task.cancel()
        async with self._cmd_lock:
            self._cmd_task = asyncio.current_task()
            try:
                yield
            finally:
                self._cmd_task = None

    @staticmethod
    async def _command(coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except (NinjaCloudError, ValueError) as err:
            raise HomeAssistantError(f"Grill command failed: {err}") from err

    def _stage(
        self,
        *,
        mode: str | None = None,
        temp: int | None = None,
        seconds: int | None = None,
        smoke: bool | None = None,
        skip_preheat: bool | None = None,
        probes: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        """Record cook settings for the next Start (and as the fallback base)."""
        if mode is not None:
            self.cook_setting_mode = mode
        if temp is not None:
            self.cook_setting_temp = int(temp)
        if seconds is not None:
            self.cook_setting_seconds = int(seconds)
        if smoke is not None:
            self.cook_setting_smoke = smoke
        if skip_preheat is not None:
            self.cook_setting_skip_preheat = skip_preheat
        for idx, cfg in (probes or {}).items():
            if cfg.get("enabled") is not None:
                self.cook_setting_probe_enabled[idx] = bool(cfg["enabled"])
            if cfg.get("setpoint") is not None:
                self.cook_setting_probe_setpoint[idx] = int(cfg["setpoint"])
        # Smoking without smoke makes no sense: a mode that requires smoke keeps
        # it on, so the staged value (and the card's smoke switch) stay consistent.
        staged = self.capabilities.get_mode(self.cook_setting_mode)
        if staged is not None and staged.requires_smoke:
            self.cook_setting_smoke = True

    async def async_set_property(self, name: str, value) -> None:
        """Write a settable cloud property."""
        if self.client is None:
            raise HomeAssistantError("Not available in local-only (BLE) mode")
        await self._command(self.client.set_property_datapoint(self.dsn, name, value))
        await self._burst_refresh()

    async def async_start_cook(self) -> None:
        """Send the staged cook settings to the grill."""
        async with self._exclusive():
            await self._start_cook()
        await self._burst_refresh()

    async def async_configure_cook(
        self,
        *,
        mode: str | None = None,
        temp: int | None = None,
        seconds: int | None = None,
        smoke: bool | None = None,
        skip_preheat: bool | None = None,
        probes: dict[int, dict[str, Any]] | None = None,
        start: bool = False,
    ) -> None:
        """Apply several cook settings as a single command.

        `probes` maps the probe index to {"enabled": bool, "setpoint": int}.
        Idle (or `start`): stages everything and optionally starts the cook.
        Active: one re-issue carrying all changes — no racing per-field writes.
        """
        self._validate(mode=mode, temp=temp, seconds=seconds, probes=probes)
        async with self._exclusive():
            self._stage(
                mode=mode,
                temp=temp,
                seconds=seconds,
                smoke=smoke,
                skip_preheat=skip_preheat,
                probes=probes,
            )
            if start:
                await self._start_cook()
            elif self.is_cook_active:
                live = self.data
                await self._reissue(
                    live,
                    mode=mode,
                    temp=temp,
                    seconds=seconds,
                    smoke=smoke,
                    probes={
                        idx: self._merged_probe(live, idx, cfg)
                        for idx, cfg in (probes or {}).items()
                    } or None,
                    # An explicit skip_preheat is the user's decision; without
                    # the field, a re-issue must not skip a running preheat.
                    skip_preheat=(
                        skip_preheat if skip_preheat is not None else not is_preheating(live)
                    ),
                )
            else:
                self.async_update_listeners()
        await self._burst_refresh()

    def _validate(
        self,
        *,
        mode: str | None,
        temp: int | None,
        seconds: int | None,
        probes: dict[int, dict[str, Any]] | None,
    ) -> None:
        """Reject settings this grill can't cook — before anything is staged.

        The per-field entities clamp to their selector's range; a service call
        carries whatever the caller sent, so it is checked against the model's
        capabilities here rather than silently corrected further down.
        """
        name = mode if mode is not None else self.live_or_staged_mode
        caps = self.capabilities.get_mode(name)
        if caps is None:
            if mode is not None:
                raise ServiceValidationError(
                    f"{self.capabilities.display_name} has no cook mode {name!r}"
                )
            # A cook running in a mode we don't model: nothing to check against,
            # and refusing the change would leave the user with no way to adjust it.
            return
        if temp is not None and not caps.temp_min <= temp <= caps.temp_max:
            raise ServiceValidationError(
                f"temperature {temp} is outside {caps.temp_min}-{caps.temp_max} for {name}"
            )
        if seconds is not None and not caps.duration_min_s <= seconds <= caps.duration_max_s:
            raise ServiceValidationError(
                f"duration {seconds // 60} min is outside "
                f"{caps.duration_min_s // 60}-{caps.duration_max_s // 60} min for {name}"
            )
        for idx, cfg in (probes or {}).items():
            if idx not in (0, 1) or (idx == 1 and not self.capabilities.has_two_probes):
                raise ServiceValidationError(
                    f"{self.capabilities.display_name} has no probe {idx}"
                )
            if cfg.get("enabled") and not caps.supports_probe:
                raise ServiceValidationError(f"mode {name} cooks without a meat probe")
            setpoint = cfg.get("setpoint")
            if setpoint is not None and not PROBE_TARGET_MIN_C <= setpoint <= PROBE_TARGET_MAX_C:
                raise ServiceValidationError(
                    f"probe target {setpoint} °C is outside "
                    f"{PROBE_TARGET_MIN_C}-{PROBE_TARGET_MAX_C} °C"
                )

    def _staged_probe(self, idx: int) -> dict[str, Any] | None:
        if not self.cook_setting_probe_enabled[idx]:
            return None
        return self._build_probe_payload(self.cook_setting_probe_setpoint[idx])

    def _merged_probe(
        self, live: CombinedState, idx: int, cfg: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The probe of a running cook with only the named fields changed.

        The staged settings are the wrong base here: a cook started on the grill
        (or in the app) leaves them at their defaults, so a target-only change
        would switch the probe off and an enable-only change would reset its
        target. Build on what the cook is actually running instead (the pending
        command while one is in flight, otherwise the live state).
        """
        current = self._live_probe(live, idx)
        enabled = cfg.get("enabled")
        if enabled is None:
            enabled = current is not None
        if not enabled:
            return None
        setpoint = cfg.get("setpoint")
        if setpoint is None:
            setpoint = current["setpoint"] if current else self.cook_setting_probe_setpoint[idx]
        return self._build_probe_payload(setpoint)

    async def _start_cook(self) -> None:
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
        smoke = mode.requires_smoke or (self.cook_setting_smoke and mode.supports_smoke)
        await self._command(self.transport.start_cook(
            self.dsn,
            mode=mode.name,
            seconds=seconds,
            temp=temp,
            smoke=smoke,
            skip_preheat=self.cook_setting_skip_preheat,
            probe_0=probes[0],
            probe_1=probes[1],
            device_key=self._device_key,
        ))
        # A fresh cook always starts with preheat (or the lid-closed wait), so
        # its timer is not running yet.
        self._remember_issue(
            mode.name, temp, seconds, seconds, smoke=smoke, probes=probes, timer_running=False
        )
        self._new_cook = True
        self._stop_issued_at = None
        # The firmware ignores skip=1 at start; auto-skip once preheat is seen.
        self._auto_skip_pending = self.cook_setting_skip_preheat
        # Optimistic: reflect the start immediately so the card doesn't sit on the
        # old state for a whole BLE poll; the burst-refresh confirms it shortly.
        if self.data is not None:
            self.data.online = True
            self.data.grill.mode = mode.name
            self.data.grill.state = "cooking" if self.cook_setting_skip_preheat else "preheat"
            self.async_update_listeners()
        if self.backend == "ble":
            # A sleeping grill can accept the BLE connection and this write
            # without acting on it (only Wake's beep reliably reaches it).
            # There's no direct error for that, so watch for the one signal
            # we do have: the grill's own beacon independently showing an
            # active phase. _cook_seen resets here so the check below means
            # "confirmed since THIS start", not a leftover from a prior cook.
            self._cook_seen = False
            if self._start_verify_task is not None:
                self._start_verify_task.cancel()
            self._start_verify_task = self.hass.async_create_task(
                self._verify_start_confirmed()
            )

    async def _verify_start_confirmed(self) -> None:
        await asyncio.sleep(START_CONFIRM_GRACE.total_seconds())
        if self._cook_seen:
            return
        self._likely_asleep = True
        self.async_update_listeners()
        _LOGGER.warning(
            "Grill %s: start_cook was sent but no independent confirmation "
            "arrived within %s - it may still be asleep; try Wake",
            self.dsn, START_CONFIRM_GRACE,
        )
        self.hass.bus.async_fire(EVENT_START_NOT_CONFIRMED, {"dsn": self.dsn})

    @property
    def likely_asleep(self) -> bool:
        """Best-effort: a start went unconfirmed and Wake hasn't been tried since."""
        return self._likely_asleep

    async def async_stop_cook(self) -> None:
        # Stop pre-empts a command still in flight, so a slow start can never
        # take effect after the user stopped the cook.
        if self._start_verify_task is not None:
            self._start_verify_task.cancel()
            self._start_verify_task = None
        async with self._exclusive(preempt=True):
            await self._command(self.transport.stop_cook(self.dsn))
            self._issued_level = None
            self._seconds_override = None
            self._pending = None
            self._auto_skip_pending = False
            self._cook_seen = False
            self._stop_issued_at = time.monotonic()
            # Optimistic: show the cook ended right away (card updates instantly).
            if self.data is not None:
                self.data.grill.state = "idle"
                self.data.cook.state = "none"
                self.async_update_listeners()
        await self._burst_refresh()

    async def async_wake(self) -> None:
        """Wake a grill that has gone to standby.

        A bare BLE connect does not wake the grill; sending a Stop command does
        (it beeps and comes back online). Stop is only sent on a positively
        confirmed standby/idle state — never as the negation of "active", so an
        unmodelled cook step (waiting for food, flip, …) can't end the cook.

        Wake goes through the command lock and re-checks standby inside it: a
        Start still in flight would otherwise be overtaken by this Stop and the
        cook would come up on a grill the user just woke.

        Only the local link can wake a grill: on a cloud backend (including a
        hybrid entry currently fallen back to cloud) this just refreshes.
        """
        if self.backend == "ble":
            async with self._exclusive():
                await self._wake()
        await self.async_request_refresh()

    async def _wake(self) -> None:
        if self._pending_cook() is not None or not self._is_standby():
            return
        # Optimistic: the user just acted on it. If the grill is still
        # asleep, the next Start attempt will fail its own confirmation and
        # re-set this.
        self._likely_asleep = False
        try:
            await self.transport.stop_cook(self.dsn)
        except (NinjaCloudError, TransportError) as err:
            # Expected and common: a sleeping grill only opens a BLE connection
            # window every so often, so a single Wake press can land outside it.
            # Not an error worth surfacing to the user (they'll likely just press
            # Wake again), but silently losing ALL trace of it makes this
            # unreproducible from the logs — keep one line so a genuinely broken
            # link is diagnosable.
            _LOGGER.info("Grill %s: wake attempt did not reach it (%s)", self.dsn, err)

    async def async_skip_preheat(self) -> None:
        async with self._exclusive():
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
        async with self._exclusive():
            self._stage(mode=mode, temp=temp, seconds=seconds, smoke=smoke)
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
        async with self._exclusive():
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
            await self._reissue(
                live, probes={idx: payload}, skip_preheat=not is_preheating(live)
            )
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
        """Re-send the running cook, merged with the given overrides.

        The base is the cook we last sent while it is still pending, so two
        settings changed in quick succession can't revert each other.
        """
        if live.grill.state in REST_STATES or live.cook.state in REST_STATES:
            raise HomeAssistantError("The cook is finished and resting")
        base = self._cook_base(live)
        new_mode = mode or base["mode"]
        new_temp = temp if temp is not None else base["temp"]
        new_smoke = smoke if smoke is not None else base["smoke"]

        remaining, elapsed = base["remaining"], base["elapsed"]
        if remaining <= 0:
            raise HomeAssistantError("The cook timer has run out")
        if seconds is not None and seconds < elapsed + _MIN_REMAINING_S:
            raise HomeAssistantError(
                f"Duration must exceed the {elapsed // 60} min already cooked"
            )
        new_seconds = seconds - elapsed if seconds is not None else remaining

        payloads = list(base["probes"])
        for idx, payload in (probes or {}).items():
            payloads[idx] = payload

        caps = self.capabilities.get_mode(new_mode)
        if caps is not None:
            if not caps.temp_min <= new_temp <= caps.temp_max:
                new_temp = caps.temp_default
            new_smoke = caps.requires_smoke or (new_smoke and caps.supports_smoke)
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
        self._remember_issue(
            new_mode, new_temp, new_seconds, elapsed + new_seconds,
            smoke=new_smoke, probes=payloads, timer_running=timer_running(live),
        )
        self.cook_setting_mode = new_mode
        self.cook_setting_temp = int(new_temp)
        self.cook_setting_seconds = int(elapsed + new_seconds)
        self.cook_setting_smoke = new_smoke
        if self._local_cook_end is not None:
            # The estimated timer is relative to what we just sent.
            self._local_cook_end = dt_util.utcnow() + timedelta(seconds=int(new_seconds))
