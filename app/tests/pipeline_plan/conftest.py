"""Fixtures of the plan-loop tests (track B1).

Two kinds of tests live here:

* **unit** tests (lint glue, brief memory, patches, the age phrase): a runtime without threads (``unit_rt``) and, where fixture specs are
  used, the demo hair-kit inventory of track S (``demo_inv``; never autouse, because the integration tests below run the real pipeline against
  the kits the app installed itself);
* **mock-mode integration** tests: ONE app per module (``app`` / ``client`` / ``rt``, module scope), because a plan loop costs seconds of CPU;
  ``world`` builds several projects in parallel so that the module pays the planning time once.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spec"))

import specfix

from duoskin.engine import registry as eng_registry
from duoskin.engine.testkit import make_app, make_client, make_runtime
from duoskin.models import kitenums
from duoskin.pipeline import kits
from duoskin.providers import registry as provider_registry


@pytest.fixture(scope="module")
def isolated():
    """Process-wide state (handler registry, provider adapters, kit inventory) restored after a module that built an app."""
    saved = eng_registry.handlers_snapshot()
    provider_registry.reset()
    kits.DEMO_OVERRIDE = None
    yield
    kits.DEMO_OVERRIDE = None
    kitenums.set_default_inventory(None)
    provider_registry.reset()
    eng_registry.clear()
    for h in saved.values():
        eng_registry.register(h, replace=True)


@pytest.fixture(scope="module")
def app(isolated, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    return make_app(home / "data", providers_mode="mock")


@pytest.fixture(scope="module")
def client(app):
    with make_client(app) as c:
        yield c


@pytest.fixture(scope="module")
def rt(app, client):
    return app.state.rt


@pytest.fixture
def demo_inv():
    """The demo kit inventory of track S for the duration of one test (the fixture specs' hair ids need it)."""
    with kitenums.use_inventory(specfix.demo_inventory()) as inv:
        yield inv


@pytest.fixture
def unit_rt(tmp_path):
    """A started runtime without scheduler threads: the database, the repository and the bus, for tests that never run steps."""
    runtime = make_runtime(tmp_path / "home", providers_mode="mock")
    runtime.startup(start_threads=False, backup=False)
    yield runtime
    runtime.shutdown()
