from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ninja_woodfire._lib.api.ayla import AuthError, TransportError

from .conftest import DEVICE, DOMAIN, DSN, mk_state, setup_integration


def _client(**overrides):
    c = AsyncMock()
    c.get_devices = AsyncMock(return_value=[DEVICE])
    c.get_combined_state = AsyncMock(return_value=mk_state())
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


async def _setup(hass, entry, client):
    await setup_integration(hass, entry, client)


async def test_setup_and_unload(hass, entry):
    await _setup(hass, entry, _client())
    assert entry.state is ConfigEntryState.LOADED
    assert len(hass.states.async_all()) > 30
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_network_down_at_boot_retries(hass, entry):
    await _setup(hass, entry, _client(login=AsyncMock(side_effect=TransportError("dns"))))
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_auth_failure_during_polling_starts_reauth(hass, entry):
    client = _client()
    await _setup(hass, entry, client)
    client.get_combined_state.side_effect = AuthError("revoked")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


async def test_all_entities_have_names(hass, entry):
    await _setup(hass, entry, _client())
    unnamed = [s.entity_id for s in hass.states.async_all() if s.entity_id.endswith(".grill")]
    assert unnamed == []


async def test_user_flow(hass):
    with patch("custom_components.ninja_woodfire.config_flow.AylaCloudClient", return_value=_client()), \
         patch("custom_components.ninja_woodfire.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "cloud"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"email": "a@b.c", "password": "x"})
        assert r["step_id"] == "pick_device"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"dsn": DSN})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["result"].unique_id == DSN


async def test_reauth_flow_updates_password(hass, entry):
    with patch("custom_components.ninja_woodfire.config_flow.AylaCloudClient", return_value=_client()), \
         patch("custom_components.ninja_woodfire.async_setup_entry", return_value=True):
        r = await entry.start_reauth_flow(hass)
        assert r["step_id"] == "reauth_confirm"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"password": "new"})
    assert r["type"] is FlowResultType.ABORT
    assert r["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"


async def test_reauth_flow_rejects_bad_password(hass, entry):
    client = _client(login=AsyncMock(side_effect=AuthError("bad")))
    with patch("custom_components.ninja_woodfire.config_flow.AylaCloudClient", return_value=client):
        r = await entry.start_reauth_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"password": "bad"})
    assert r["errors"] == {"base": "invalid_auth"}


async def test_reauth_flow_unexpected_error(hass, entry):
    client = _client(login=AsyncMock(side_effect=RuntimeError("boom")))
    with patch("custom_components.ninja_woodfire.config_flow.AylaCloudClient", return_value=client):
        r = await entry.start_reauth_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"password": "x"})
    assert r["errors"] == {"base": "unknown"}


async def test_local_only_flow_without_cloud(hass):
    # Local setup takes only the grill's BLE address; session keys derive from it.
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "local"})
    assert r["step_id"] == "local"
    # Local setup needs the key bundle the user extracted themselves (144 bytes).
    blob = (
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
        "MDEyMzQ1Njc4OTo7PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5f"
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8AAQIDBAUGBwgJCgsMDQ4P"
    )
    with patch("custom_components.ninja_woodfire.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"ble_address": "AA:BB:CC:DD:EE:FF", "ble_slots": blob}
        )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["result"].unique_id == "AA:BB:CC:DD:EE:FF"
    assert "email" not in r["result"].data
    assert r["result"].data["ble_address"] == "AA:BB:CC:DD:EE:FF"
    assert r["result"].data["ble_slots"] == blob
    # A manually-entered grill gets the friendly name, never a raw MAC.
    assert r["result"].title == "Ninja Woodfire"


def test_nice_name_ignores_mac():
    from custom_components.ninja_woodfire.config_flow import _nice_name
    # The grill advertises its MAC as its BLE name -> use the friendly label.
    assert _nice_name("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF") == "Ninja Woodfire"
    assert _nice_name("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66") == "Ninja Woodfire"
    assert _nice_name(None, "AA:BB:CC:DD:EE:FF") == "Ninja Woodfire"
    assert _nice_name("", "x") == "Ninja Woodfire"
    # A genuine name (not MAC-shaped) is kept.
    assert _nice_name("Patio Grill", "AA:BB:CC:DD:EE:FF") == "Patio Grill"
