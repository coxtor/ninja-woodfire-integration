"""Constants for the Ninja Woodfire HA integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "ninja_woodfire"

CONF_REGION = "region"
CONF_DSN = "dsn"
# Optional credential overrides — empty in the entry means the
# integration uses its bundled per-region defaults.
CONF_AUTH0_AUDIENCE = "auth0_audience"
CONF_AUTH0_CLIENT_ID = "auth0_client_id"
CONF_AYLA_APP_ID = "ayla_app_id"
CONF_AYLA_APP_SECRET = "ayla_app_secret"

# Adaptive polling cadence — fast while cooking, relaxed while idle.
SCAN_INTERVAL_ACTIVE = timedelta(seconds=1)        # while cooking / preheat / rest (matches app's ~750ms)
SCAN_INTERVAL_IDLE = timedelta(seconds=10)         # while idle
DEFAULT_SCAN_INTERVAL = SCAN_INTERVAL_IDLE         # initial; coordinator adapts
MIN_SCAN_INTERVAL = timedelta(seconds=1)
DEFAULT_REGION = "EU"

# Cook states that should trigger fast polling.
ACTIVE_STATES = frozenset(
    {"preheat", "cooking", "cook", "rest", "resting", "flip", "get food",
     "get_food", "lid open", "lid_open"}
)

# Cook-lifecycle events fired on the HA event bus. See coordinator
# `_emit_lifecycle_events`. Automations subscribe via `event_type`.
EVENT_COOK_STARTED = "ninja_woodfire_cook_started"
EVENT_PREHEAT_COMPLETE = "ninja_woodfire_preheat_complete"
EVENT_COOK_HALFTIME = "ninja_woodfire_cook_halftime"
EVENT_COOK_DONE = "ninja_woodfire_cook_done"
EVENT_PROBE_TARGET_REACHED = "ninja_woodfire_probe_target_reached"
