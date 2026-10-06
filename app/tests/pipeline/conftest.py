"""Fixtures of the pipeline tests: an in-process mock-mode app on a temp DATA folder with the scheduler running, clean provider
adapters and the demo kit inventory restored afterwards."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from duoskin.engine import registry as eng_registry
from duoskin.engine.testkit import make_app, make_client
from duoskin.models import kitenums
from duoskin.pipeline import kits
from duoskin.providers import registry as provider_registry


@pytest.fixture(autouse=True)
def isolated_process_state():
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


@pytest.fixture
def app(tmp_path):
    app = make_app(tmp_path / "home", providers_mode="mock")
    app.state.rt.update_settings({"paths": {"exports_root": str(tmp_path / "exports"), "tripo_inbox": str(tmp_path / "exports" / "TripoPacks" / "inbox")}})
    return app


@pytest.fixture
def client(app):
    with make_client(app) as c:
        yield c


@pytest.fixture
def rt(client, app):
    return app.state.rt


@pytest.fixture
def demo_inventory(tmp_path):
    """The kit inventory with the demo kits installed (what the fixture specs' hair ids need); the autouse fixture restores it."""
    from duoskin.models import kitenums as K

    inv = K.inventory_from_manifest(kits.build_manifest(tmp_path / "nokits", include_demo=True))
    K.set_default_inventory(inv)
    return inv
