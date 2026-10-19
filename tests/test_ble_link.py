"""I-05: the real notify queue must not drop frames that arrive early."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ninja_woodfire._lib.api.ble_link import HaBleLink

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _link(hass):
    link = HaBleLink(hass, ADDRESS)
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(return_value=bytes(96))
    client.write_gatt_char = AsyncMock()
    client.disconnect = AsyncMock()
    link._client = client
    return link, client


@pytest.mark.asyncio
async def test_a_reply_arriving_during_the_write_is_delivered_once(hass):
    link, client = _link(hass)

    async def write(char, frame, response=True):
        link._on_notify(None, bytearray(b"connect-reply"))

    client.write_gatt_char.side_effect = write

    await link.write_b002(b"x")
    assert await link.collect(0) == [b"connect-reply"]
    assert await link.collect(0) == []        # consumed, not replayed


@pytest.mark.asyncio
async def test_frames_arriving_during_collect_are_returned(hass):
    link, _client = _link(hass)

    async def late():
        await asyncio.sleep(0)
        link._on_notify(None, bytearray(b"state"))

    asyncio.create_task(late())
    assert await link.collect(0.05) == [b"state"]


@pytest.mark.asyncio
async def test_a_new_connection_discards_the_previous_session(hass):
    link, _client = _link(hass)
    link._on_notify(None, bytearray(b"old-session"))
    link._client = None

    with patch(
        "custom_components.ninja_woodfire._lib.api.ble_link.bluetooth"
        ".async_ble_device_from_address",
        return_value=MagicMock(),
    ), patch(
        "custom_components.ninja_woodfire._lib.api.ble_link.establish_connection",
        AsyncMock(return_value=MagicMock(start_notify=AsyncMock())),
    ):
        await link.connect()

    assert await link.collect(0) == []
