"""Home Assistant BLE I/O for the Woodfire transport.

A persistent connection over HA's Bluetooth stack (so ESPHome BT proxies work
transparently), feeding frames to `BleTransport` through the `BleLink` shape.
Kept separate from `ble.py` so the transport logic stays import-light for tests.
"""
from __future__ import annotations

import asyncio
from collections import deque

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .errors import TransportError

_B001 = "0000b001-0000-1000-8000-00805f9b34fb"  # read: challenge (kicks off stream)
_B002 = "0000b002-0000-1000-8000-00805f9b34fb"  # write: app -> grill
_B003 = "0000b003-0000-1000-8000-00805f9b34fb"  # notify
_B004 = "0000b004-0000-1000-8000-00805f9b34fb"  # indicate: grill -> app


class HaBleLink:
    """Persistent BLE link to one grill via HA Bluetooth."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self._address = address
        self._client: BleakClientWithServiceCache | None = None
        self._frames: deque[bytes] = deque(maxlen=256)

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._frames.append(bytes(data))

    async def connect(self) -> None:
        if self._client is not None and self._client.is_connected:
            return
        device = bluetooth.async_ble_device_from_address(
            self._hass, self._address.upper(), connectable=True
        )
        if device is None:
            raise TransportError(f"grill {self._address} not in BLE range / no proxy")
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache, device, self._address
            )
            for char in (_B004, _B003):
                try:
                    await self._client.start_notify(char, self._on_notify)
                except Exception:  # noqa: BLE001 - b003 may be absent
                    pass
        except Exception as err:  # noqa: BLE001
            raise TransportError(f"BLE connect failed: {err}") from err

    async def read_b001(self) -> bytes:
        assert self._client is not None
        return await self._client.read_gatt_char(_B001)

    async def write_b002(self, frame: bytes) -> None:
        assert self._client is not None
        await self._client.write_gatt_char(_B002, frame, response=True)

    async def collect(self, seconds: float) -> list[bytes]:
        self._frames.clear()
        await asyncio.sleep(seconds)
        return list(self._frames)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
