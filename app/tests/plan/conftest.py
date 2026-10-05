"""Fixtures of track S: the demo kit inventory (with a small hair kit) is installed for every test, and the shared spec fixtures are importable."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spec"))

import specfix  # noqa: E402

from duoskin.models import kitenums  # noqa: E402


@pytest.fixture(autouse=True)
def demo_kit_inventory():
    """Every spec, lint and prompt test sees the demo inventory: built-in kits plus a human-written demo hair kit."""
    with kitenums.use_inventory(specfix.demo_inventory()) as inv:
        yield inv


@pytest.fixture(scope="session")
def fixture_names() -> list[str]:
    return list(specfix.SPEC_NAMES)


@pytest.fixture()
def specs(demo_kit_inventory):
    return specfix.all_specs()


@pytest.fixture()
def spec_d(demo_kit_inventory):
    """A fresh dict of the complement fixture (mutate it, then ``parse``)."""
    return specfix.load_dict("spec_complement_gb")
