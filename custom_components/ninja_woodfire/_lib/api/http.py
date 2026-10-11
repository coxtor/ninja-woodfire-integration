"""HTTP request helper shared by the cloud clients."""
from __future__ import annotations

from typing import Any

import aiohttp

from .errors import TransportError

TIMEOUT = aiohttp.ClientTimeout(total=20)


async def send(
    http: aiohttp.ClientSession, method: str, url: str, **kwargs: Any
) -> tuple[int, Any]:
    """Return (status, parsed JSON on 2xx else body text)."""
    try:
        async with http.request(method, url, timeout=TIMEOUT, **kwargs) as r:
            if r.status in (200, 201):
                return r.status, await r.json(content_type=None)
            return r.status, await r.text()
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise TransportError(f"{method} {url}: {err!r}") from err
