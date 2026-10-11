"""Cook command payloads, in the firmware's spelling."""
from __future__ import annotations

from typing import Any

from ..const import COOK_MODES, PROBE_MODES


def cook_command(
    *,
    mode: str,
    seconds: int,
    temp: int,
    smoke: bool,
    skip_preheat: bool,
    probe_0: dict[str, Any] | None,
    probe_1: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and build a cook command without its "id"."""
    if mode not in COOK_MODES:
        raise ValueError(f"unknown cook mode {mode!r}; expected one of {COOK_MODES}")
    for label, p in (("probe_0", probe_0), ("probe_1", probe_1)):
        if p is not None and p.get("mode") not in PROBE_MODES:
            raise ValueError(
                f"{label}.mode {p.get('mode')!r} invalid; expected one of {PROBE_MODES}"
            )
    payload: dict[str, Any] = {
        "mode": mode,
        "seconds set": int(seconds),
        "temp": int(temp),
        "smoke": 1 if smoke else 0,
        "skip preheat": 1 if skip_preheat else 0,
    }
    if probe_0 is not None:
        payload["probe0"] = probe_0
    if probe_1 is not None:
        payload["probe1"] = probe_1
    return payload


def stop_command() -> dict[str, Any]:
    return {
        "id": 1000,
        "mode": "grill",
        "temp": 0,
        "seconds set": 0,
        "smoke": 0,
        "skip preheat": 0,
    }
