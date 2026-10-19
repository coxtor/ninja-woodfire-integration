"""I-15 and the report's smaller follow-ups (unload order, flow, last_error)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er

from custom_components.ninja_woodfire._lib.api.errors import TransportError
from custom_components.ninja_woodfire._lib.capabilities import WOODFIRE_PRO_XL
from custom_components.ninja_woodfire.coordinator import NinjaWoodfireCoordinator

from .conftest import DEVICE, DOMAIN, DSN, mk_state, setup_integration

SLOTS = (" ".join(f"{b:02x}" for b in bytes(range(144))))


def _ayla():
    client = AsyncMock()
    client.get_devices = AsyncMock(return_value=[DEVICE])
    client.get_combined_state = AsyncMock(return_value=mk_state())
    return client


# ------------------------------------------------------------- last_error


async def test_a_recovered_poll_clears_the_last_error(hass, entry):
    client = _ayla()
    c = NinjaWoodfireCoordinator(hass, entry, client, DSN, WOODFIRE_PRO_XL, 42)
    client.get_combined_state.side_effect = TransportError("down")
    await c.async_refresh()
    assert c.last_error and c.failed_polls == 1

    client.get_combined_state.side_effect = None
    await c.async_refresh()
    assert c.failed_polls == 0
    assert c.last_error is None


# ------------------------------------------------------------- unload order


async def test_a_failed_platform_unload_keeps_the_transport(hass, entry):
    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    await setup_integration(hass, entry, _ayla())
    coordinator = entry.runtime_data
    coordinator.ble = ble
    coordinator.beacon_unsub = lambda: None

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        from custom_components.ninja_woodfire import async_unload_entry

        assert await async_unload_entry(hass, entry) is False

    assert coordinator.beacon_unsub is not None
    ble.close.assert_not_called()


async def test_a_successful_unload_releases_the_transport(hass, entry):
    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    await setup_integration(hass, entry, _ayla())
    coordinator = entry.runtime_data
    coordinator.ble = ble
    coordinator.beacon_unsub = lambda: None

    from custom_components.ninja_woodfire import async_unload_entry

    assert await async_unload_entry(hass, entry) is True
    assert coordinator.beacon_unsub is None
    ble.close.assert_awaited_once()


# ------------------------------------------------------------- BLE config flow


async def _local_flow(hass, user_input, discovered=()):
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=discovered,
    ), patch("custom_components.ninja_woodfire.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local"}
        )
        return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_a_ble_address_is_stored_canonically(hass):
    result = await _local_flow(
        hass, {"ble_address": " aa:bb:cc:dd:ee:ff ", "ble_slots": SLOTS}
    )
    assert result["type"] == "create_entry"
    assert result["result"].data["ble_address"] == "AA:BB:CC:DD:EE:FF"
    assert result["result"].unique_id == "AA:BB:CC:DD:EE:FF"


async def test_the_same_grill_in_another_spelling_is_not_added_twice(hass):
    await _local_flow(hass, {"ble_address": "AA:BB:CC:DD:EE:FF", "ble_slots": SLOTS})
    result = await _local_flow(
        hass, {"ble_address": "aa:bb:cc:dd:ee:ff", "ble_slots": SLOTS}
    )
    assert result["type"] == "abort"


async def test_an_address_can_be_typed_even_when_others_were_discovered(hass):
    class _Info:
        address = "11:22:33:44:55:66"
        name = "Woodfire"
        service_uuids = ["0000fcbb-0000-1000-8000-00805f9b34fb"]

    result = await _local_flow(
        hass, {"ble_address": "AA:BB:CC:DD:EE:FF", "ble_slots": SLOTS}, discovered=[_Info()]
    )
    assert result["type"] == "create_entry"
    assert result["result"].data["ble_address"] == "AA:BB:CC:DD:EE:FF"


# ------------------------------------------------------------- I-15 diagnostics


async def test_the_ble_diagnostic_states_which_connection_it_means(hass, entry):
    ble = AsyncMock()
    ble.get_combined_state.return_value = mk_state()
    with patch("custom_components.ninja_woodfire._build_ble_transport",
               return_value=(ble, None)):
        await setup_integration(hass, entry, _ayla())
    eid = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{DSN}_ble_connected")
    state = hass.states.get(eid)
    assert state.state == "on"
    # The value is the last *GATT* connection attempt, not passive beacon
    # reception — say so, with the time it refers to.
    assert state.attributes.get("last_gatt_attempt") is not None


async def test_the_cloud_diagnostic_names_the_backend_it_reports(hass, entry):
    await setup_integration(hass, entry, _ayla())
    eid = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DSN}_cloud_connected"
    )
    assert hass.states.get(eid).attributes.get("backend") == "ayla"
