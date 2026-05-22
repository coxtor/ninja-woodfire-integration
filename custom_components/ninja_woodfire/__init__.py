"""Ninja Woodfire HA integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._lib.api.ayla import AuthError, AylaCloudClient, TransportError
from ._lib.const import make_region

from .const import (
    CONF_AUTH0_AUDIENCE,
    CONF_AUTH0_CLIENT_ID,
    CONF_AYLA_APP_ID,
    CONF_AYLA_APP_SECRET,
    CONF_DSN,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
)
from .coordinator import NinjaWoodfireCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Each credential field is optional in the entry — when absent, the
    # bundled per-region default is used. This makes existing setups
    # automatically pick up new defaults if vendor identifiers rotate.
    region = make_region(
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        auth0_audience=entry.data.get(CONF_AUTH0_AUDIENCE),
        auth0_client_id=entry.data.get(CONF_AUTH0_CLIENT_ID),
        ayla_app_id=entry.data.get(CONF_AYLA_APP_ID),
        ayla_app_secret=entry.data.get(CONF_AYLA_APP_SECRET),
    )

    client = AylaCloudClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        region=region,
        session=async_get_clientsession(hass),
    )

    from ._lib.capabilities import for_oem_model

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

    coordinator = NinjaWoodfireCoordinator(
        hass=hass,
        client=client,
        dsn=dsn,
        capabilities=capabilities,
        device_key=device_key,
        device_info_extra={
            "oem_model": str(device.get("oem_model", "")),
            "model": str(device.get("model", "")),
            "sw_version": str(device.get("sw_version", "")),
            "product_name": str(device.get("product_name") or capabilities.display_name),
        },
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
