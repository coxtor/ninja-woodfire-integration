"""Ninja Woodfire HA integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._lib.api.auth0 import Auth0Session
from ._lib.api.aws import AwsCloudClient
from ._lib.api.ayla import AylaCloudClient
from ._lib.api.errors import AuthError, TransportError
from ._lib.capabilities import (
    PROBE_TARGET_MAX_C,
    PROBE_TARGET_MIN_C,
    WOODFIRE_PRO_XL,
    for_oem_model,
)
from ._lib.const import COOK_MODES, make_region

from .const import (
    DOMAIN,
    CONF_AWS_API_BASE,
    CONF_AWS_API_KEY,
    CONF_AUTH0_AUDIENCE,
    CONF_AUTH0_CLIENT_ID,
    CONF_AYLA_APP_ID,
    CONF_AYLA_APP_SECRET,
    CONF_BLE_ADDRESS,
    CONF_BLE_SLOTS,
    BLE_SERVICE_UUID,
    CONF_DSN,
    CONF_REGION,
    DEFAULT_REGION,
)
from .coordinator import NinjaWoodfireConfigEntry, NinjaWoodfireCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
]


SERVICE_CONFIGURE_COOK = "configure_cook"

# One atomic "apply these cook settings" call. Changing several settings through
# the per-field entities fires several commands, each re-sending the whole cook;
# this applies them together so they cannot overwrite each other.
# Ranges here are the firmware's outer bounds; what the grill actually supports
# per mode is checked against its capabilities in the coordinator.
_PROBE_TARGET = vol.All(
    vol.Coerce(int), vol.Range(min=PROBE_TARGET_MIN_C, max=PROBE_TARGET_MAX_C)
)
CONFIGURE_COOK_SCHEMA = vol.Schema({
    vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("mode"): vol.In(COOK_MODES),
    vol.Optional("temp"): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
    vol.Optional("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),  # minutes
    vol.Optional("smoke"): cv.boolean,
    vol.Optional("skip_preheat"): cv.boolean,
    vol.Optional("probe_0_enabled"): cv.boolean,
    vol.Optional("probe_0_target"): _PROBE_TARGET,
    vol.Optional("probe_1_enabled"): cv.boolean,
    vol.Optional("probe_1_target"): _PROBE_TARGET,
    vol.Optional("start", default=False): cv.boolean,
})


async def async_setup_entry(hass: HomeAssistant, entry: NinjaWoodfireConfigEntry) -> bool:
    _register_services(hass)
    # Local-only (BLE) entries carry no account; set up without any cloud.
    if CONF_EMAIL not in entry.data:
        return await _async_setup_local(hass, entry)

    # Each credential field is optional in the entry — when absent, the
    # bundled per-region default is used. This makes existing setups
    # automatically pick up new defaults if vendor identifiers rotate.
    region = make_region(
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        auth0_audience=entry.data.get(CONF_AUTH0_AUDIENCE),
        auth0_client_id=entry.data.get(CONF_AUTH0_CLIENT_ID),
        ayla_app_id=entry.data.get(CONF_AYLA_APP_ID),
        ayla_app_secret=entry.data.get(CONF_AYLA_APP_SECRET),
        aws_rest_base=entry.data.get(CONF_AWS_API_BASE),
        aws_api_key=entry.data.get(CONF_AWS_API_KEY),
    )
    http = async_get_clientsession(hass)
    auth = Auth0Session(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], region)

    client = AylaCloudClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        region=region,
        session=http,
        auth=auth,
    )

    try:
        await client.login()
        devices = await client.get_devices()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TransportError as err:
        raise ConfigEntryNotReady(str(err)) from err

    dsn = entry.data[CONF_DSN]
    device = next((d for d in devices if d.get("dsn") == dsn), None)
    if device is None:
        raise ConfigEntryNotReady(f"device {dsn} not found in account")
    capabilities = for_oem_model(device.get("oem_model"))
    device_key = int(device.get("key", 0))
    if not device_key:
        raise ConfigEntryNotReady(f"device {dsn} has no device key")

    ble, beacon_slot = _build_ble_transport(hass, entry)

    coordinator = NinjaWoodfireCoordinator(
        hass=hass,
        config_entry=entry,
        client=client,
        dsn=dsn,
        capabilities=capabilities,
        device_key=device_key,
        aws=AwsCloudClient(auth, region, http),
        ble=ble,
        beacon_slot=beacon_slot,
        device_info_extra={
            "oem_model": str(device.get("oem_model", "")),
            "model": str(device.get("model", "")),
            "sw_version": str(device.get("sw_version", "")),
            "product_name": str(device.get("product_name") or capabilities.display_name),
        },
    )
    await coordinator.async_config_entry_first_refresh()

    _register_beacon(hass, entry, coordinator)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_setup_local(hass: HomeAssistant, entry: NinjaWoodfireConfigEntry) -> bool:
    """Set up a cloud-free entry: BLE transport only, no Ayla/AWS."""
    ble, beacon_slot = _build_ble_transport(hass, entry)
    if ble is None:
        raise ConfigEntryNotReady(
            "grill not reachable over Bluetooth (adapter/proxy in range?)"
        )
    dsn = entry.data[CONF_DSN]
    coordinator = NinjaWoodfireCoordinator(
        hass=hass,
        config_entry=entry,
        client=None,
        dsn=dsn,
        # Local setup targets the Woodfire Pro XL; without a cloud account we
        # can't query the oem_model, so default to its full mode set.
        capabilities=WOODFIRE_PRO_XL,
        device_key=0,
        aws=None,
        ble=ble,
        beacon_slot=beacon_slot,
        device_info_extra={
            "product_name": entry.title or WOODFIRE_PRO_XL.display_name,
            "model": WOODFIRE_PRO_XL.model_id,
        },
    )
    await coordinator.async_config_entry_first_refresh()
    # The grill's real serial/model arrive in the BLE grill-info burst.
    info = getattr(ble, "info", {}) or {}
    coordinator.grill_serial = info.get("serial")
    if info.get("model"):
        coordinator.device_info_extra["oem_model"] = info["model"]
    _register_beacon(hass, entry, coordinator)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _coordinators(hass: HomeAssistant, device_ids: list[str]) -> list:
    registry = dr.async_get(hass)
    found = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        entries = device.config_entries if device else set()
        for entry in (hass.config_entries.async_get_entry(e) for e in entries):
            if entry is None or entry.domain != DOMAIN:
                continue
            if (coordinator := getattr(entry, "runtime_data", None)) is not None:
                found.append(coordinator)
    if not found:
        raise ServiceValidationError(f"no Ninja Woodfire grill for {device_ids}")
    return found


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_CONFIGURE_COOK):
        return

    async def configure_cook(call: ServiceCall) -> None:
        data = call.data
        probes = {}
        for idx in (0, 1):
            cfg = {
                key: data[f"probe_{idx}_{field}"]
                for key, field in (("enabled", "enabled"), ("setpoint", "target"))
                if f"probe_{idx}_{field}" in data
            }
            if cfg:
                probes[idx] = cfg
        duration = data.get("duration")
        for coordinator in _coordinators(hass, data["device_id"]):
            await coordinator.async_configure_cook(
                mode=data.get("mode"),
                temp=data.get("temp"),
                seconds=None if duration is None else int(duration) * 60,
                smoke=data.get("smoke"),
                skip_preheat=data.get("skip_preheat"),
                probes=probes or None,
                start=data["start"],
            )

    hass.services.async_register(
        DOMAIN, SERVICE_CONFIGURE_COOK, configure_cook, schema=CONFIGURE_COOK_SCHEMA
    )


def _register_beacon(hass: HomeAssistant, entry: NinjaWoodfireConfigEntry, coordinator) -> None:
    """Passively read the grill's advertisement beacon for the probe temps + lid.

    The meat-probe temperature and lid state are not in the GATT state frame; the
    grill broadcasts them in its BLE advertisement (encrypted, but with no
    per-device secret — see ble_beacon). We subscribe to those adverts over HA's
    Bluetooth stack (proxy-capable), decrypt them and feed the coordinator.
    """
    # Only BLE-addressed (local) entries have a beacon to read; cloud-only
    # entries have no Bluetooth address, so there is nothing to subscribe to.
    address = entry.data.get(CONF_BLE_ADDRESS)
    if not address:
        return
    # The beacon is a best-effort bonus: a failure here must never stop the
    # integration from loading.
    try:
        from homeassistant.components import bluetooth
        from homeassistant.components.bluetooth import (
            BluetoothCallbackMatcher,
            BluetoothScanningMode,
        )

        from ._lib.api.ble_beacon import mfg1_from_advertisement

        @callback
        def _on_adv(service_info, change) -> None:
            mfg1 = mfg1_from_advertisement(
                service_info.manufacturer_data, getattr(service_info, "raw", None)
            )
            coordinator.update_beacon(mfg1)

        coordinator.beacon_unsub = bluetooth.async_register_callback(
            hass,
            _on_adv,
            BluetoothCallbackMatcher(address=address, connectable=False),
            BluetoothScanningMode.PASSIVE,
        )
    except Exception:  # noqa: BLE001 - beacon is optional; never block setup
        _LOGGER.debug("beacon listener not registered", exc_info=True)


def _build_ble_transport(hass: HomeAssistant, entry: NinjaWoodfireConfigEntry):
    """Create the local BLE transport and beacon slot from the pasted key bundle.

    Returns (transport, beacon_slot), or (None, None) when local BLE is not set
    up. This package ships no key material: the keys come from the bundle the
    user pastes, extracted from their own copy of the vendor app (see README).
    Nothing is derived from the address.
    """
    slots_blob = entry.data.get(CONF_BLE_SLOTS)
    if not slots_blob:
        return None, None

    from ._lib.api import ble_proto

    try:
        tx_slot, rx_slot, beacon_slot = ble_proto.parse_slots_blob(slots_blob)
    except ValueError:
        _LOGGER.warning("invalid BLE key bundle in config entry; local disabled")
        return None, None

    address = entry.data.get(CONF_BLE_ADDRESS)
    if not address:
        try:
            from homeassistant.components import bluetooth

            for info in bluetooth.async_discovered_service_info(hass, connectable=True):
                if BLE_SERVICE_UUID in (info.service_uuids or []):
                    address = info.address
                    break
        except Exception:  # noqa: BLE001 - no BT stack; fall through to cloud
            address = None
    if not address:
        return None, None

    from ._lib.api.ble import BleTransport
    from ._lib.api.ble_link import HaBleLink

    _LOGGER.info("Local BLE transport enabled (%s)", address)
    return BleTransport(tx_slot, rx_slot, HaBleLink(hass, address)), beacon_slot


async def async_unload_entry(hass: HomeAssistant, entry: NinjaWoodfireConfigEntry) -> bool:
    # Release the transport only once the platforms are actually gone: on a
    # failed unload the entities stay loaded and still need their data source.
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator = entry.runtime_data
    if coordinator is not None:
        if getattr(coordinator, "beacon_unsub", None) is not None:
            coordinator.beacon_unsub()
            coordinator.beacon_unsub = None
        if getattr(coordinator, "ble", None) is not None:
            await coordinator.ble.close()
    return True
