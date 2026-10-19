"""Config flow: collect Ninja-account credentials, list grills, pick one."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import SectionConfig, section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ._lib.api.ayla import AuthError, AylaCloudClient, TransportError
from ._lib.const import REGION_DEFAULTS, make_region

from .const import (
    CONF_AWS_API_BASE,
    CONF_AWS_API_KEY,
    CONF_AUTH0_AUDIENCE,
    CONF_AUTH0_CLIENT_ID,
    CONF_AYLA_APP_ID,
    CONF_AYLA_APP_SECRET,
    CONF_BLE_ADDRESS,
    CONF_BLE_SLOTS,
    CONF_DSN,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Friendly label for local (Bluetooth) grills. The grill advertises its MAC as
# its BLE name, which is ugly in discovery and as the device name, so we fall
# back to this unless the adapter reports a genuine, non-MAC name.
_LOCAL_NAME = "Ninja Woodfire"


def _nice_name(name: str | None, address: str | None) -> str:
    """A human name for a local grill; ignore a MAC-shaped BLE name."""
    cleaned = (name or "").strip()
    mac_shaped = len(cleaned) == 17 and all(c in "0123456789ABCDEFabcdef:" for c in cleaned)
    if not cleaned or mac_shaped or cleaned.casefold() == (address or "").casefold():
        return _LOCAL_NAME
    return cleaned

# Standard form shows just the account info. The six vendor-credential
# overrides are expert escape hatches (for when the bundled defaults rotate);
# they live in a collapsed section so they don't confuse everyone else.
_ADVANCED_FIELDS = (
    CONF_AUTH0_AUDIENCE,
    CONF_AUTH0_CLIENT_ID,
    CONF_AYLA_APP_ID,
    CONF_AYLA_APP_SECRET,
    CONF_AWS_API_BASE,
    CONF_AWS_API_KEY,
)
_ADVANCED_SECTION = "advanced_options"


def _user_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): vol.In(
            list(REGION_DEFAULTS.keys())
        ),
        vol.Optional(_ADVANCED_SECTION): section(
            vol.Schema({vol.Optional(key, default=""): str for key in _ADVANCED_FIELDS}),
            SectionConfig(collapsed=True),
        ),
    })


def _opt(value: str | None) -> str | None:
    """Treat empty strings as 'not provided' so make_region() falls back."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class NinjaWoodfireConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] | None = None
        self._devices: list[dict[str, Any]] = []
        self._ble_address: str | None = None
        self._ble_name: str | None = None

    async def async_step_bluetooth(self, discovery_info: Any) -> ConfigFlowResult:
        """A Woodfire grill was auto-discovered over Bluetooth -> local setup."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._ble_address = discovery_info.address
        self._ble_name = _nice_name(discovery_info.name, discovery_info.address)
        self.context["title_placeholders"] = {"name": self._ble_name}
        return await self.async_step_local()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between a cloud account or local-only (BLE) setup."""
        return self.async_show_menu(step_id="user", menu_options=["cloud", "local"])

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Flatten the collapsed section so the rest of the flow (and
            # async_step_pick_device, which persists these) can keep treating
            # the overrides as plain top-level keys.
            advanced = user_input.pop(_ADVANCED_SECTION, {}) or {}
            user_input.update(advanced)
            region = make_region(
                user_input.get(CONF_REGION, DEFAULT_REGION),
                auth0_audience=_opt(user_input.get(CONF_AUTH0_AUDIENCE)),
                auth0_client_id=_opt(user_input.get(CONF_AUTH0_CLIENT_ID)),
                ayla_app_id=_opt(user_input.get(CONF_AYLA_APP_ID)),
                ayla_app_secret=_opt(user_input.get(CONF_AYLA_APP_SECRET)),
                aws_rest_base=_opt(user_input.get(CONF_AWS_API_BASE)),
                aws_api_key=_opt(user_input.get(CONF_AWS_API_KEY)),
            )
            client = AylaCloudClient(
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                region=region,
                session=async_get_clientsession(self.hass),
            )
            try:
                await client.login()
                self._devices = await client.get_devices()
            except AuthError:
                errors["base"] = "invalid_auth"
            except TransportError as err:
                _LOGGER.warning("transport error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected during login/get_devices")
                errors["base"] = "unknown"
            else:
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    self._user_input = user_input
                    return await self.async_step_pick_device()

        return self.async_show_form(
            step_id="cloud",
            data_schema=_user_schema(),
            errors=errors,
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Local-only setup: the grill's BLE address plus the pasted key bundle.

        Identifies the grill over Bluetooth (service fcbb). Nothing is derived
        from the address: the session and beacon keys come from the bundle the
        user extracts from their own copy of the vendor app (see the README).
        Needs a local Bluetooth adapter or an ESPHome Bluetooth proxy in range.
        """
        discovered: dict[str, str] = {}
        try:
            from homeassistant.components import bluetooth

            from .const import BLE_SERVICE_UUID

            discovered = {
                info.address: f"{_nice_name(info.name, info.address)} ({info.address})"
                for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
                if BLE_SERVICE_UUID in (info.service_uuids or [])
            }
        except Exception:  # noqa: BLE001 - discovery is best-effort; address can be typed
            discovered = {}
        errors: dict[str, str] = {}
        if user_input is not None:
            # One spelling per grill: HaBleLink upper-cases the address, so the
            # entry's unique id has to be canonical too or the same grill could
            # be added twice.
            address = (
                self._ble_address or user_input.get(CONF_BLE_ADDRESS) or ""
            ).strip().upper()
            blob = (user_input.get(CONF_BLE_SLOTS) or "").strip()
            if not address and len(discovered) == 1:
                address = next(iter(discovered))
            if not address:
                errors["base"] = "no_ble_device"
            elif not blob:
                errors["base"] = "no_keys"
            else:
                # The keys come from the user's own extraction (see README); we
                # only validate that the pasted bundle is well-formed.
                from ._lib.api import ble_proto
                try:
                    ble_proto.parse_slots_blob(blob)
                except ValueError:
                    errors["base"] = "bad_keys"
                else:
                    await self.async_set_unique_id(address)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=self._ble_name or _LOCAL_NAME,
                        data={CONF_DSN: address, CONF_BLE_ADDRESS: address,
                              CONF_BLE_SLOTS: blob},
                    )

        # Offer what was discovered, but always allow typing an address: a grill
        # reachable only through a proxy may not show up in the scan.
        addr_field = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=address, label=label)
                    for address, label in discovered.items()
                ],
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema({
                vol.Optional(CONF_BLE_ADDRESS, default=""): addr_field,
                vol.Required(CONF_BLE_SLOTS): str,
            }),
            errors=errors,
        )

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            dsn = user_input[CONF_DSN]
            await self.async_set_unique_id(dsn)
            self._abort_if_unique_id_configured()

            device = next((d for d in self._devices if d.get("dsn") == dsn), {})
            title = device.get("product_name") or f"Ninja Woodfire {dsn}"
            assert self._user_input is not None
            data = {
                CONF_EMAIL: self._user_input[CONF_EMAIL],
                CONF_PASSWORD: self._user_input[CONF_PASSWORD],
                CONF_REGION: self._user_input.get(CONF_REGION, DEFAULT_REGION),
                CONF_DSN: dsn,
            }
            # Persist credential overrides only when the user actually
            # supplied non-empty values. Otherwise we keep the entry
            # clean and the integration tracks vendor defaults.
            for key in (
                CONF_AUTH0_AUDIENCE,
                CONF_AUTH0_CLIENT_ID,
                CONF_AYLA_APP_ID,
                CONF_AYLA_APP_SECRET,
                CONF_AWS_API_BASE,
                CONF_AWS_API_KEY,
            ):
                v = _opt(self._user_input.get(key))
                if v:
                    data[key] = v
            return self.async_create_entry(title=title, data=data)

        choices = {
            d.get("dsn", ""): f"{d.get('product_name') or d.get('oem_model') or 'Grill'} ({d.get('dsn')})"
            for d in self._devices
        }
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema({vol.Required(CONF_DSN): vol.In(choices)}),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            client = AylaCloudClient(
                email=data[CONF_EMAIL],
                password=data[CONF_PASSWORD],
                region=make_region(
                    data.get(CONF_REGION, DEFAULT_REGION),
                    auth0_audience=data.get(CONF_AUTH0_AUDIENCE),
                    auth0_client_id=data.get(CONF_AUTH0_CLIENT_ID),
                    ayla_app_id=data.get(CONF_AYLA_APP_ID),
                    ayla_app_secret=data.get(CONF_AYLA_APP_SECRET),
                ),
                session=async_get_clientsession(self.hass),
            )
            try:
                await client.login()
            except AuthError:
                errors["base"] = "invalid_auth"
            except TransportError as err:
                _LOGGER.warning("transport error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected during reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )
