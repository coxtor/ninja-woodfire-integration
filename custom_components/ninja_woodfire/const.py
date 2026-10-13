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
EVENT_PROMPT = "ninja_woodfire_prompt"
