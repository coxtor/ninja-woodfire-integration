"""Constants for the Ninja Woodfire HA integration."""
from __future__ import annotations

from datetime import timedelta

from ._lib.const import (  # noqa: F401
    ACTIVE_STATES,
    COOKING_STATES,
    IDLE_STATES,
    PAUSED_STATES,
    PREHEAT_STATES,
    REST_STATES,
    WAITING_STATES,
)

DOMAIN = "ninja_woodfire"

CONF_REGION = "region"
CONF_DSN = "dsn"
# Optional credential overrides — empty in the entry means the
# integration uses its bundled per-region defaults.
CONF_AUTH0_AUDIENCE = "auth0_audience"
CONF_AUTH0_CLIENT_ID = "auth0_client_id"
CONF_AYLA_APP_ID = "ayla_app_id"
CONF_AYLA_APP_SECRET = "ayla_app_secret"
CONF_AWS_API_BASE = "aws_api_base"
CONF_AWS_API_KEY = "aws_api_key"
CONF_BLE_SLOTS = "ble_slots"        # paste-block: tx/rx key+nonce
CONF_BLE_ADDRESS = "ble_address"    # grill BLE MAC/UUID (optional)

# Woodfire BLE GATT service (advertised); used to discover the grill.
BLE_SERVICE_UUID = "0000fcbb-0000-1000-8000-00805f9b34fb"

# Adaptive polling cadence — fast while cooking, relaxed while idle.
SCAN_INTERVAL_ACTIVE = timedelta(seconds=3)
SCAN_INTERVAL_IDLE = timedelta(seconds=10)
DEFAULT_SCAN_INTERVAL = SCAN_INTERVAL_IDLE
DEFAULT_REGION = "EU"

# Cook-lifecycle events fired on the HA event bus. See coordinator
# `_emit_lifecycle_events`. Automations subscribe via `event_type`.
EVENT_COOK_STARTED = "ninja_woodfire_cook_started"
EVENT_PREHEAT_COMPLETE = "ninja_woodfire_preheat_complete"
EVENT_COOK_HALFTIME = "ninja_woodfire_cook_halftime"
EVENT_COOK_DONE = "ninja_woodfire_cook_done"
EVENT_PROBE_TARGET_REACHED = "ninja_woodfire_probe_target_reached"
EVENT_PROBE_HALFWAY = "ninja_woodfire_probe_halfway"
# Purely informational: a plugged-in probe crossing a user-set alarm
# threshold. Independent of the probe's own target/enabled mechanism (which
# drives temperature-controlled cooking) - a probe reports its temperature
# regardless of mode, confirmed live 2026-11-06, so this works alongside a
# time-controlled cook without touching the exclusive cook-style the
# firmware itself enforces.
EVENT_PROBE_ALARM = "ninja_woodfire_probe_alarm"
# Fires once a probe enters the firmware's fixed 300 s post-target Rest
# window (see coordinator._REST_TOTAL_SECONDS) - the one sub-state of a
# probe-driven cook with a real countdown, shown on the grill's own physical
# display but never surfaced by the app's own UI. Without a dedicated event,
# the status snapshot's seconds_left/seconds_set never actually reaches a
# notification: by the time cook_done fires, the window (and the ground-truth
# Uim-idle override that releases it) has already cleared those fields.
EVENT_PROBE_RESTING_STARTED = "ninja_woodfire_probe_resting_started"
EVENT_PROMPT = "ninja_woodfire_prompt"
# Fired when a local BLE start_cook produced no independent confirmation
# (beacon never showed an active phase) within the grace window below — the
# grill is most likely still asleep and ignored it. See
# coordinator._verify_start_confirmed.
EVENT_START_NOT_CONFIRMED = "ninja_woodfire_start_not_confirmed"
# Fired instead of EVENT_START_NOT_CONFIRMED when a previous probe's post-
# target Rest window (see coordinator._REST_TOTAL_SECONDS) is still blocking
# the beacon's phase signal. Live-confirmed 2026-11-06: a remote Stop cannot
# cancel an already-resting probe at all - only the grill's own physical
# button can - so a new cook genuinely cannot start yet, but the grill is
# awake and busy, not asleep. Suggesting Wake here would be actively
# misleading (there is nothing to wake).
EVENT_START_BLOCKED_BY_RESTING_PROBE = "ninja_woodfire_start_blocked_by_resting_probe"
START_CONFIRM_GRACE = timedelta(seconds=25)
