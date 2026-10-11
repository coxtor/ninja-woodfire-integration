import base64
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.ninja_woodfire  # noqa: F401
from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
from custom_components.ninja_woodfire._lib.models import (
    CombinedState,
    CookState,
    GrillState,
    ProbeState,
)
from custom_components.ninja_woodfire.const import (
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_TARGET_REACHED,
)
from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator

pytest_plugins = ["pytest_homeassistant_custom_component"]

FIXTURES = Path(__file__).parent / "fixtures"

DOMAIN = "ninja_woodfire"
DSN = "AC000W1"
ENTRY_DATA = {"email": "a@b.c", "password": "pw", "region": "EU", "dsn": DSN}
DEVICE = {
    "dsn": DSN,
    "oem_model": "OG900-EU",
    "key": 42,
    "product_name": "Grill",
    "sw_version": "1.0",
    "model": "m",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def mk_state(grill=None, cook=None, probes=None) -> CombinedState:
    return CombinedState(
        dsn=DSN,
        grill=GrillState.from_property_value(json.dumps(grill or {"state": "idle"})),
        cook=CookState.from_property_value(json.dumps(cook or {"state": "none"})),
        probes=ProbeState.from_property_value(json.dumps(probes or {"probes": []})),
    )


def cooking(mode="roast", setpoint=200, left=900, total=1800, state="cooking", **extra):
    return {
        "state": state,
        "mode": mode,
        "setpoint": setpoint,
        "smoke": 0,
        "seconds set": total,
        "seconds left": left,
        "endtimeutc": int(time.time()) + left,
        **extra,
    }


def probe(temp=40, setpoint=74, active=1, name="probe0"):
    return {
        "name": name,
        "plugged in": 1,
        "active": active,
        "temp": temp,
        "mode": {"mode": "manual", "setpoint": setpoint},
    }


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(domain=DOMAIN, unique_id=DSN, data=dict(ENTRY_DATA), title="Grill")
    e.add_to_hass(hass)
    return e


@pytest.fixture
def coordinator(hass, entry):
    client = AsyncMock()
    c = NinjaWoodfireCoordinator(hass, entry, client, DSN, WOODFIRE_PRO_XL, 42)
    c._burst_refresh = AsyncMock()
    return c


@pytest.fixture
def events(hass):
    seen = []
    for ev in (EVENT_COOK_DONE, EVENT_COOK_HALFTIME, EVENT_COOK_STARTED, EVENT_PREHEAT_COMPLETE,
               EVENT_PROBE_HALFWAY, EVENT_PROBE_TARGET_REACHED):
        hass.bus.async_listen(ev, lambda e: seen.append(e.event_type))
    return seen


def jwt(sub="auth0|user1", exp=None) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"sub": sub, "exp": int(time.time()) + 3600 if exp is None else exp}
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def aws_device(name="aws_cooking", connected=None, updated_at=None):
    """An AWS device record for the configured grill, built from a capture."""
    fx = load_fixture(name)
    return {
        "deviceId": "SND1",
        "registry": {"serialNumber": "SND1", "WiFiModuleSerialNumber": f"{DSN}-SND1"},
        "telemetry": fx["telemetry"],
        "connectivityStatus": {
            "connected": fx["device"]["connected"] if connected is None else connected
        },
        "updatedAt": updated_at or fx["device"]["updatedAt"],
    }


def mock_aws(device=None):
    aws = AsyncMock()
    aws.find_device = AsyncMock(return_value=device)
    return aws


async def setup_integration(hass, entry, client, aws=None):
    with patch("custom_components.ninja_woodfire.AylaCloudClient", return_value=client), \
         patch("custom_components.ninja_woodfire.AwsCloudClient", return_value=aws or mock_aws()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
