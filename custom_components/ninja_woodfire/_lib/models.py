"""Transport-agnostic state models.

The grill firmware exposes its state as JSON-encoded strings in three Ayla
properties (`GET_GrillState`, `GET_CookState`, `GET_ProbeState`). The same
data eventually comes over BLE as bincode-encoded structs. Both transports
hydrate into the dataclasses below — entities don't care which transport
was used.

All fields are observed in real captures; semantics are documented in
docs/API.md § 3.1 (state schemas).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .const import ACTIVE_STATES, IDLE_STATES, PREHEAT_STATES


def _parse_value(raw: Any) -> dict[str, Any]:
    """Ayla wraps the JSON state in a string. Tolerate both string + dict."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _prompt(message: Any) -> str | None:
    name = str(message or "").rpartition(":")[2].strip().lower()
    return name or None


def _int(value: Any, default: int | None = None) -> int | None:
    f = _float(value, None)
    return default if f is None else int(f)


# ---------------------------------------------------------------- temps

FAHRENHEIT_TEMP_FIELDS = frozenset({
    "grill", "air", "smoke", "probe0_a", "probe0_b", "probe1_a", "probe1_b",
})


@dataclass
class GrillTemps:
    """Live temperature readings from the grill MCU, normalised to °C.

    The firmware sends grill/air/smoke/probeN_a/probeN_b in °F (unlike
    `setpoint` and `ProbeInfo.temp`, which are already °C); `from_wire`
    converts them. Missing, invalid or non-positive °F readings (unused
    channels, unplugged probe elements) become None. probeN_a/probeN_b are
    dual-element raw probe readings (averaged into the per-probe `temp` field).
    main/ui are PCB analog reads, NOT temperatures despite the unit confusion
    you'd think — values like 6542.4 are ADC counts × 0.1.
    """

    grill: float | None = None
    air: float | None = None
    smoke: float | None = None
    probe0_a: float | None = None
    probe0_b: float | None = None
    probe1_a: float | None = None
    probe1_b: float | None = None
    main: float = 0.0  # PCB ADC, not °C
    ui: float = 0.0    # PCB ADC, not °C

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> "GrillTemps":
        values = {}
        for name in vars(cls()):
            value = _float(raw.get(name), None)
            if name in FAHRENHEIT_TEMP_FIELDS:
                value = round((value - 32) * 5 / 9, 1) if value and value > 0 else None
            elif value is None:
                value = 0.0
            values[name] = value
        return cls(**values)


# ---------------------------------------------------------------- grill state

@dataclass
class GrillState:
    """Consolidated grill state — what the grill display shows.

    Populated from `GET_GrillState.value`. Most fields are only present
    while a cook is active; defaults are safe for `state == "idle"`.
    """

    # Always present
    state: str = "unknown"           # idle | preheat | cooking | rest | done | …
    message: str = ""
    event_mask: str = ""
    prompt: str | None = None        # addfood | flipfood | done | getfood, from "<bit>:<name>"
    lid_open: bool = False
    sim: int = 0
    temps: GrillTemps = field(default_factory=GrillTemps)
    raw: dict[str, Any] = field(default_factory=dict)

    # Only present when state != idle (None when idle)
    mode: str | None = None          # grill | smoker | bake | roast | broil | …
    setpoint: int | None = None      # target value, semantics depend on mode
    seconds_set: int | None = None   # original cook duration in seconds
    seconds_left: int | None = None  # remaining time in seconds
    end_time_utc: int | None = None  # UNIX timestamp of cook end
    smoke: bool = False              # True if "Woodfire-Aromatechnologie" active
    error: int = 0                   # firmware error code, 0 = OK
    probes_active: int = 0           # 0/1/2

    @classmethod
    def from_property_value(cls, raw: Any) -> "GrillState":
        d = _parse_value(raw)
        inputs = d.get("inputs", {})
        temps_raw = inputs.get("temps", {})
        io = inputs.get("io", {})
        return cls(
            state=str(d.get("state", "unknown")),
            message=str(d.get("message", "")),
            prompt=_prompt(d.get("message")),
            event_mask=str(d.get("eventmask", "")),
            lid_open=bool(io.get("lid open", 0)),
            sim=_int(d.get("sim"), 0),
            temps=GrillTemps.from_wire(temps_raw),
            mode=d.get("mode") if d.get("mode") else None,
            setpoint=_int(d.get("setpoint")),
            seconds_set=_int(d.get("seconds set")),
            seconds_left=_int(d.get("seconds left")),
            end_time_utc=_int(d.get("endtimeutc")),
            smoke=bool(d.get("smoke", 0)),
            error=_int(d.get("error"), 0),
            probes_active=_int(d.get("probes active"), 0),
            raw=d,
        )


# ---------------------------------------------------------------- cook state

@dataclass
class CookState:
    """The cook-state-machine's current step.

    Populated from `GET_CookState.value`. The nested `state` field can be
    either a plain string ("none") or an object with `state` + `progress`
    (e.g. {"state":"preheat","progress":75}).
    """

    state: str = "none"           # none | start | preheat | heat | cooking | flip | rest | done | error | lid_open
    progress: int | None = None   # 0-100, only when state has progress (preheat/cook/rest)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_property_value(cls, raw: Any) -> "CookState":
        d = _parse_value(raw)
        nested = d.get("state", {})
        if isinstance(nested, dict):
            state_name = str(nested.get("state", "none"))
            progress = nested.get("progress")
        else:
            state_name = str(nested)
            progress = None
        return cls(
            state=state_name,
            progress=_int(progress),
            raw=d,
        )


# ---------------------------------------------------------------- probes

@dataclass
class ProbeMode:
    """Probe target — either manual setpoint or a doneness preset."""

    mode: str = "none"             # none | manual | preset
    setpoint: int | None = None    # target temperature in °C (manual mode)
    preset_index: int | None = None
    protein: int | str | None = None     # ProteinKind enum or string: Beef|Poultry|Chicken|…
    cut: int | str | None = None
    doneness: int | str | None = None    # Doneness enum or string: Rare|MedRare|Med|MedWell|Well

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProbeMode":
        def _parse_int_or_str(value: Any) -> int | str | None:
            """Parse field that can be either int enum or string name."""
            if value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    return value
            return None

        return cls(
            mode=str(d.get("mode", "none")) if d else "none",
            setpoint=_int(d.get("setpoint")) if d else None,
            preset_index=_int(d.get("preset_index")) if d else None,
            protein=_parse_int_or_str(d.get("protein")) if d else None,
            cut=_parse_int_or_str(d.get("cut")) if d else None,
            doneness=_parse_int_or_str(d.get("doneness")) if d else None,
        )


@dataclass
class ProbeInfo:
    """Per-probe state, read from `GET_ProbeState.probes[i]`."""

    name: str = ""                 # "probe0" or "probe1"
    plugged_in: bool = False
    active: bool = False           # True when a cook is using this probe
    temp: float = 0.0              # current measured temp in °C
    progress: int = 0              # 0-100 progress towards setpoint
    target: ProbeMode = field(default_factory=ProbeMode)
    state: str = "none"            # cooking | done | none | get_food | flip_food | …

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProbeInfo":
        mode_obj = d.get("mode", {})
        state_obj = d.get("state", {})
        if isinstance(state_obj, dict):
            state = str(state_obj.get("state", "none"))
        else:
            state = str(state_obj)
        return cls(
            name=str(d.get("name", "")),
            plugged_in=bool(d.get("plugged in", 0)),
            active=bool(d.get("active", 0)),
            temp=_float(d.get("temp")),
            progress=_int(d.get("progress"), 0),
            target=ProbeMode.from_dict(mode_obj if isinstance(mode_obj, dict) else {}),
            state=state,
        )


@dataclass
class ProbeState:
    probes: list[ProbeInfo] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_property_value(cls, raw: Any) -> "ProbeState":
        d = _parse_value(raw)
        probes_raw = d.get("probes", []) or []
        return cls(
            probes=[ProbeInfo.from_dict(p) for p in probes_raw],
            raw=d,
        )


# ---------------------------------------------------------------- combined

@dataclass
class CombinedState:
    """Snapshot of the entire grill — what HA entities consume."""

    dsn: str
    grill: GrillState = field(default_factory=GrillState)
    cook: CookState = field(default_factory=CookState)
    probes: ProbeState = field(default_factory=ProbeState)
    online: bool = True
    connection_status: str | None = None
    # ISO-8601 timestamp from `property.data_updated_at` — when the grill
    # last reported any of these values. If older than ~60s the grill is
    # most likely off / in standby and the cached temps are stale.
    last_updated_at: Any = None  # datetime | None at runtime
    # Wi-Fi signal in dBm; only the AWS telemetry reports it.
    rssi: int | None = None

    @property
    def preheating(self) -> bool:
        """Whether the grill is still preheating.

        The firmware keeps CookState at preheat/100 until the add-food prompt is
        acknowledged by opening the lid, while the cook timer already runs.
        """
        g, c = self.grill, self.cook
        if g.prompt == "addfood":
            return False
        if g.state in PREHEAT_STATES:
            return True
        if c.state not in PREHEAT_STATES or (c.progress is not None and c.progress >= 100):
            return False
        return not (g.seconds_set and g.seconds_left and g.seconds_left < g.seconds_set - 10)

    def state_age_seconds(self) -> float | None:
        """Seconds since the grill last reported, or None if unknown."""
        if self.last_updated_at is None:
            return None
        from datetime import datetime, timezone
        return (datetime.now(tz=timezone.utc) - self.last_updated_at).total_seconds()

    def is_stale(self, max_age_seconds: int = 60) -> bool:
        """True if the cloud's last-reported timestamp is too old to trust."""
        age = self.state_age_seconds()
        return age is not None and age > max_age_seconds

    # Modes that exercise each chamber sensor. Outside this set the
    # cloud may keep returning a (stale or noisy) value but the
    # chamber isn't being used — better to hide than to mislead.
    _AIR_TEMP_MODES = frozenset({
        "air crisp", "bake", "roast", "reheat", "dehydrate",
    })

    def temp_is_plausible(self, value: float | None, name: str = "grill") -> bool:
        """Whether a temperature reading should be shown.

        Args:
            value: the °C reading.
            name: which sensor — "grill", "air", "smoke". Drives
                  per-mode plausibility (e.g. smoke chamber only
                  reports meaningfully when smoke=on; air chamber
                  only matters in air-crisp / bake / dehydrate).
        """
        if value is None or not self.online:
            return False
        if self.is_stale():
            return False
        # Idle grill: stale cache from the last cook session keeps
        # leaking — anything above ambient is implausible.
        if self.grill.state.casefold() in IDLE_STATES and value > 50:
            return False
        # Active cook: gate sensors by which chamber is in play.
        if self.grill.state in ACTIVE_STATES:
            mode = self.grill.mode
            if name == "smoke" and not self.grill.smoke:
                return False
            if name == "air" and mode and mode not in self._AIR_TEMP_MODES:
                return False
        return True
