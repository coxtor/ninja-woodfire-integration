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
from dataclasses import dataclass, field
from typing import Any


def _parse_value(raw: Any) -> dict[str, Any]:
    """Ayla wraps the JSON state in a string. Tolerate both string + dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------- temps

@dataclass
class GrillTemps:
    """Live temperature readings from the grill MCU.

    grill/air/smoke are all in °C and reflect what the grill display shows.
    probeN_a/probeN_b are dual-element raw probe readings (averaged into
    the per-probe `temp` field).
    main/ui are PCB analog reads, NOT temperatures despite the unit confusion
    you'd think — values like 6542.4 are ADC counts × 0.1.
    """

    grill: float = 0.0
    air: float = 0.0
    smoke: float = 0.0
    probe0_a: float = 0.0
    probe0_b: float = 0.0
    probe1_a: float = 0.0
    probe1_b: float = 0.0
    main: float = 0.0  # PCB ADC, not °C
    ui: float = 0.0    # PCB ADC, not °C


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
            event_mask=str(d.get("eventmask", "")),
            lid_open=bool(io.get("lid open", 0)),
            sim=int(d.get("sim", 0)),
            temps=GrillTemps(
                grill=float(temps_raw.get("grill", 0)),
                air=float(temps_raw.get("air", 0)),
                smoke=float(temps_raw.get("smoke", 0)),
                probe0_a=float(temps_raw.get("probe0_a", 0)),
                probe0_b=float(temps_raw.get("probe0_b", 0)),
                probe1_a=float(temps_raw.get("probe1_a", 0)),
                probe1_b=float(temps_raw.get("probe1_b", 0)),
                main=float(temps_raw.get("main", 0)),
                ui=float(temps_raw.get("ui", 0)),
            ),
            mode=d.get("mode") if d.get("mode") else None,
            setpoint=int(d["setpoint"]) if "setpoint" in d else None,
            seconds_set=int(d["seconds set"]) if "seconds set" in d else None,
            seconds_left=int(d["seconds left"]) if "seconds left" in d else None,
            end_time_utc=int(d["endtimeutc"]) if "endtimeutc" in d else None,
            smoke=bool(d.get("smoke", 0)),
            error=int(d.get("error", 0)),
            probes_active=int(d.get("probes active", 0)),
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
            progress=int(progress) if progress is not None else None,
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
            setpoint=int(d["setpoint"]) if d and "setpoint" in d else None,
            preset_index=int(d["preset_index"]) if d and "preset_index" in d else None,
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
            temp=float(d.get("temp", 0)),
            progress=int(d.get("progress", 0)),
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
    # ISO-8601 timestamp from `property.data_updated_at` — when the grill
    # last reported any of these values. If older than ~60s the grill is
    # most likely off / in standby and the cached temps are stale.
    last_updated_at: Any = None  # datetime | None at runtime

    def is_stale(self, max_age_seconds: int = 60) -> bool:
        """True if the cloud's last-reported timestamp is too old to trust."""
        if self.last_updated_at is None:
            return False  # be lenient if we can't tell
        from datetime import datetime, timezone
        age = (datetime.now(tz=timezone.utc) - self.last_updated_at).total_seconds()
        return age > max_age_seconds

    # Modes that exercise each chamber sensor. Outside this set the
    # cloud may keep returning a (stale or noisy) value but the
    # chamber isn't being used — better to hide than to mislead.
    _AIR_TEMP_MODES = frozenset({
        "air crisp", "bake", "roast", "reheat", "dehydrate",
    })

    _ACTIVE_PHASES = frozenset({
        "preheat", "preheating", "heat",
        "cooking", "cook", "rest", "resting",
    })

    def temp_is_plausible(self, value: float, name: str = "grill") -> bool:
        """Whether a temperature reading should be shown.

        Args:
            value: the raw °C reading.
            name: which sensor — "grill", "air", "smoke". Drives
                  per-mode plausibility (e.g. smoke chamber only
                  reports meaningfully when smoke=on; air chamber
                  only matters in air-crisp / bake / dehydrate).
        """
        if value is None:
            return False
        if self.is_stale():
            return False
        # Idle grill: stale cache from the last cook session keeps
        # leaking — anything above ambient is implausible.
        if self.grill.state == "idle" and value > 50:
            return False
        # Active cook: gate sensors by which chamber is in play.
        if self.grill.state in self._ACTIVE_PHASES:
            mode = self.grill.mode
            if name == "smoke" and not self.grill.smoke:
                return False
            if name == "air" and mode and mode not in self._AIR_TEMP_MODES:
                return False
        return True
