"""I-12: the Node-RED example must keep two grills' notifications apart."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FLOWS = sorted((Path(__file__).parents[1] / "examples/node-red").glob("woodfire_flow*.json"))
NODE = shutil.which("node")


def _function_body(flow: Path) -> str:
    for node in json.loads(flow.read_text()):
        if node.get("type") == "function":
            return node["func"]
    raise AssertionError(f"no function node in {flow.name}")


def _run(flow: Path, event_type: str, event: dict) -> list[dict]:
    """Execute the flow's function node on one synthetic event."""
    script = (
        "const msg = " + json.dumps({"payload": {"event_type": event_type, "event": event}})
        + ";\nconst out = (function () {\n" + _function_body(flow) + "\n})();"
        + "\nconsole.log(JSON.stringify(out ? out[0].map((m) => m.payload) : []));"
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def _tags(payloads: list[dict]) -> set[str]:
    return {p["data"]["tag"] for p in payloads if "tag" in p.get("data", {})}


pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is needed to run the flow"),
    # V-08: every shipped language variant has to behave identically.
    pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.stem),
]

A = {"dsn": "AC000W1", "mode": "roast", "setpoint": 180, "seconds_set": 1800}
B = {"dsn": "AC000W2", "mode": "bake", "setpoint": 200, "seconds_set": 900}


def test_two_grills_use_disjoint_notification_tags(flow):
    for event_type in ("ninja_woodfire_cook_started", "ninja_woodfire_prompt"):
        a = _tags(_run(flow, event_type, {**A, "prompt": "flipfood"}))
        b = _tags(_run(flow, event_type, {**B, "prompt": "flipfood"}))
        assert a and b
        assert not (a & b), f"{event_type}: {a} collides with {b}"


def test_probe_tags_are_per_grill(flow):
    event = {"probe_index": 0, "target": 70, "current": 50}
    a = _tags(_run(flow, "ninja_woodfire_probe_halfway", {**A, **event}))
    b = _tags(_run(flow, "ninja_woodfire_probe_halfway", {**B, **event}))
    assert not (a & b)


def test_cook_done_clears_only_its_own_notifications_including_the_prompt(flow):
    payloads = _run(flow, "ninja_woodfire_cook_done", {**A, "reason": "done"})
    cleared = {p["data"]["tag"] for p in payloads if p.get("message") == "clear_notification"}
    assert len(cleared) == 3                       # both probes + the prompt
    assert all(A["dsn"] in tag for tag in cleared)

    other = _tags(_run(flow, "ninja_woodfire_prompt", {**B, "prompt": "flipfood"}))
    assert not (cleared & other)


def test_notification_groups_are_per_grill(flow):
    groups = {
        _run(flow, "ninja_woodfire_cook_started", ev)[0]["data"]["group"] for ev in (A, B)
    }
    assert len(groups) == 2


def test_every_notification_carries_a_tag(flow):
    events = {
        "ninja_woodfire_cook_started": A,
        "ninja_woodfire_preheat_complete": A,
        "ninja_woodfire_cook_halftime": {**A, "seconds_left": 600},
        "ninja_woodfire_probe_halfway": {**A, "probe_index": 0, "target": 70, "current": 50},
        "ninja_woodfire_probe_target_reached": {**A, "probe_index": 0, "target": 70, "current": 70},
        "ninja_woodfire_cook_done": {**A, "reason": "done"},
        "ninja_woodfire_prompt": {**A, "prompt": "flipfood"},
    }
    for event_type, event in events.items():
        for payload in _run(flow, event_type, event):
            tag = payload.get("data", {}).get("tag")
            assert isinstance(tag, str) and A["dsn"] in tag, (event_type, payload)
