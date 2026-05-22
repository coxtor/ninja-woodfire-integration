"""Constants for the Ninja Woodfire cloud auth flow."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CloudRegion:
    """Per-region authentication and API endpoints."""

    name: str
    auth0_base: str
    auth0_audience: str
    auth0_client_id: str
    ayla_user_base: str
    ayla_device_base: str
    ayla_app_id: str
    ayla_app_secret: str


# Per-region defaults. Credentials below are the same identifiers the
# official Ninja Kitchen Android app sends to the cloud; they're public
# in every install of that app. Bundling them here means the integration
# works out of the box.
#
# If they ever stop working (vendor rotation, regional split, etc.),
# users can extract fresh values from their own phone with
# scripts/extract_credentials.py and override via the config flow.
REGION_EU_DEFAULTS = dict(
    name="EU",
    auth0_base="https://logineu.sharkninja.com",
    auth0_audience="https://sharkninja-eu-prod.eu.auth0.com/api/v2/",
    auth0_client_id="WjsyFHxF1B1OT7EEh0LWc3NZJktQ2an2",
    ayla_user_base="https://user-field-eu.aylanetworks.com",
    ayla_device_base="https://ads-eu.aylanetworks.com",
    ayla_app_id="android_ninjakitchen_prod-PQ-id",
    ayla_app_secret="android_ninjakitchen_prod-k8MHvn6qaNqafn4UNKu9OjR_Epc",
)

REGION_NA_DEFAULTS = dict(
    name="NA",
    auth0_base="https://login.sharkninja.com",
    auth0_audience="https://sharkninja-prod.us.auth0.com/api/v2/",
    auth0_client_id="Jz8oxd4LMwOD7wFbGoR0ji43o292SB0s",
    ayla_user_base="https://user-field-39a9391a.aylanetworks.com",
    ayla_device_base="https://ads-field-39a9391a.aylanetworks.com",
    ayla_app_id="android_ninjakitchen_prod-gg-id",
    ayla_app_secret="android_ninjakitchen_prod-b85m9QC9-Pp-pTAWhoaJSzt-EhI",
)

REGION_DEFAULTS: dict[str, dict[str, str]] = {
    "EU": REGION_EU_DEFAULTS,
    "NA": REGION_NA_DEFAULTS,
}


def make_region(
    name: str,
    *,
    auth0_audience: str | None = None,
    auth0_client_id: str | None = None,
    ayla_app_id: str | None = None,
    ayla_app_secret: str | None = None,
) -> CloudRegion:
    """Build a CloudRegion. Each credential falls back to the bundled
    default when not provided — user-supplied values take precedence."""
    base = REGION_DEFAULTS.get(name, REGION_EU_DEFAULTS)
    return CloudRegion(
        name=base["name"],
        auth0_base=base["auth0_base"],
        auth0_audience=auth0_audience or base["auth0_audience"],
        auth0_client_id=auth0_client_id or base["auth0_client_id"],
        ayla_user_base=base["ayla_user_base"],
        ayla_device_base=base["ayla_device_base"],
        ayla_app_id=ayla_app_id or base["ayla_app_id"],
        ayla_app_secret=ayla_app_secret or base["ayla_app_secret"],
    )

# Property names used by the cloud API.
PROP_GRILL_STATE = "GET_GrillState"
PROP_COOK_STATE = "GET_CookState"
PROP_PROBE_STATE = "GET_ProbeState"
PROP_DEVICE_MODEL = "GET_Device_Model_Number"
PROP_DEVICE_SERIAL = "GET_Device_Serial_Num"
PROP_FW_VERSION = "GET_HostSWVer"

PROP_SET_COOK = "SET_Cook_Command"
PROP_SET_POWER = "SET_GrillPower"
PROP_RESET_FACTORY = "SET_Reset_Factory"
PROP_RESET_WIFI = "SET_Reset_WiFi"

ALL_READ_PROPERTIES = [PROP_GRILL_STATE, PROP_COOK_STATE, PROP_PROBE_STATE]

# Cook modes accepted by the cook command (strings exactly as the
# firmware expects).
COOK_MODES = (
    "grill",
    "smoker",
    "bake",
    "roast",
    "broil",
    "dehydrate",
    "griddle",
    "reheat",
    "air crisp",
)

# Heat levels for grill mode (other modes use °C).
GRILL_HEAT_LEVELS = {1: "Lo", 2: "Med", 3: "Hi"}

PROBE_MODE_MANUAL = "manual"
PROBE_MODE_PRESET = "preset"
PROBE_MODES = (PROBE_MODE_MANUAL, PROBE_MODE_PRESET)
