"""Fixtures of the learning tests (calibration, regression, drills): a threadless mock-mode runtime on a temp DATA folder, a clean handler
registry and calibrated-threshold state, plus the helpers in ``lfix``."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/ itself: ``web.conftest`` for the browser tests
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))     # pfix: the locked fixture spec the part pipeline starts from

from duoskin.checks import thresholds as TH
from duoskin.engine import registry as eng_registry
from duoskin.engine.testkit import make_app, make_client, make_runtime
from duoskin.models import kitenums
from duoskin.pipeline import kits
from duoskin.providers import registry as provider_registry


@pytest.fixture(autouse=True)
def isolated_process_state():
    saved = eng_registry.handlers_snapshot()
    provider_registry.reset()
    kits.DEMO_OVERRIDE = None
    TH.set_calibrated({})
    yield
    TH.set_calibrated({})
    kits.DEMO_OVERRIDE = None
    kitenums.set_default_inventory(None)
    provider_registry.reset()
    eng_registry.clear()
    for h in saved.values():
        eng_registry.register(h, replace=True)


@pytest.fixture
def rt(tmp_path):
    """A started-but-threadless runtime (drive it with ``rt.scheduler.tick()``); the plug-in lanes are NOT registered."""
    runtime = make_runtime(tmp_path / "home", providers_mode="mock")
    runtime.startup(start_threads=False, backup=False)
    yield runtime
    runtime.shutdown()


@pytest.fixture
def app(tmp_path):
    app = make_app(tmp_path / "home", providers_mode="mock")
    app.state.rt.update_settings({"paths": {"exports_root": str(tmp_path / "exports"), "tripo_inbox": str(tmp_path / "exports" / "inbox")}})
    return app


@pytest.fixture
def client(app):
    with make_client(app) as c:
        yield c


@pytest.fixture
def demo_inventory(tmp_path):
    """The kit inventory with the demo kits installed (what the fixture specs' hair ids need); the autouse fixture restores it."""
    from duoskin.models import kitenums as K

    inv = K.inventory_from_manifest(kits.build_manifest(tmp_path / "nokits", include_demo=True))
    K.set_default_inventory(inv)
    return inv


# the browser fixtures of the web tests (``live``: the app on a loopback port; ``ui``: Chromium on it) are reused as they are
from web.conftest import browser, live, ui, ui_factory  # noqa: F401
