"""Cloud transport client."""
from __future__ import annotations

from typing import Any, Protocol

from ..models import CombinedState


class GrillTransport(Protocol):
    """What the coordinator needs from a backend to read state and cook."""

    async def get_combined_state(self, dsn: str) -> CombinedState: ...

    async def start_cook(
        self,
        dsn: str,
        *,
        mode: str,
        seconds: int,
        temp: int,
        smoke: bool = False,
        skip_preheat: bool = False,
        probe_0: dict[str, Any] | None = None,
        probe_1: dict[str, Any] | None = None,
        device_key: int | None = None,
    ) -> Any: ...

    async def stop_cook(self, dsn: str) -> Any: ...
