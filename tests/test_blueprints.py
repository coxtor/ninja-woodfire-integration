"""I-10: the shipped blueprints must resolve every input they use."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

BLUEPRINTS = sorted(
    (Path(__file__).parents[1] / "blueprints/automation/ninja_woodfire").glob("*.yaml")
)


class _InputLoader(yaml.SafeLoader):
    """Reads blueprints without resolving HA's !input tag."""


_InputLoader.add_constructor("!input", lambda loader, node: ("!input", node.value))


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.stem)
def test_templated_inputs_are_bound_to_variables(path):
    doc = yaml.load(path.read_text(), Loader=_InputLoader)
    declared = set(doc["blueprint"]["input"])
    bound = {
        name
        for name, value in (doc.get("variables") or {}).items()
        if isinstance(value, tuple) and value[0] == "!input"
    }
    # Blueprint inputs are not automatically available to Jinja; anything used
    # in a template has to be bound through a top-level `variables:` entry.
    used = {
        name
        for name in declared
        if re.search(rf"{{{{[^}}]*\b{name}\b", yaml.dump(doc))
    }
    assert used <= bound, f"{path.name}: {sorted(used - bound)} used but not bound"


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.stem)
def test_blueprint_has_the_expected_shape(path):
    doc = yaml.load(path.read_text(), Loader=_InputLoader)
    assert doc["blueprint"]["domain"] == "automation"
    assert doc["trigger"]
    assert doc["action"]
