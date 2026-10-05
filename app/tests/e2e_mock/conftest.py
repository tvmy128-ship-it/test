"""Fixtures of the mock-mode end-to-end tests (APP_SPEC §17.4).

Process state is isolated per MODULE (not per test): the mock providers keep their Tripo tasks in memory, so a board made once (PARTS to Gate 2,
about 80 s) can be copied into every test that needs one, and the copy's later BUILD steps still find the multiview tasks of the first run.
``make_runtime`` starts a fresh app on an empty home; ``board`` returns a copy of the shared Gate 2 board. Both send the exports and the Tripo inbox
to the test's temp folder (the real ones belong to the user).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from duoskin.engine import registry as eng_registry
from duoskin.engine.testkit import make_app, make_client
from duoskin.models import kitenums
from duoskin.pipeline import kits
from duoskin.providers import registry as provider_registry


@pytest.fixture(scope="module", autouse=True)
def module_process_state():
    saved = eng_registry.handlers_snapshot()
    provider_registry.reset()
    mp = pytest.MonkeyPatch()
    mp.setenv("DUOSKIN_MESH_INPROC", "1")           # the same worker code, in this process: faster tests (the subprocess path has its own test)
    mp.setenv("DUOSKIN_NO_INBOX_THREAD", "1")       # the tests poll the inbox by hand
    kits.DEMO_OVERRIDE = None
    yield
    mp.undo()
    kits.DEMO_OVERRIDE = None
    kitenums.set_default_inventory(None)
    provider_registry.reset()
    eng_registry.clear()
    for h in saved.values():
        eng_registry.register(h, replace=True)


@pytest.fixture(autouse=True)
def per_test_state():
    kits.DEMO_OVERRIDE = None
    from duoskin.pipeline import export

    export.ALLOW_MOCK_FOR_TESTS = False
    yield
    kits.DEMO_OVERRIDE = None
    export.ALLOW_MOCK_FOR_TESTS = False


def _paths(tmp_path: Path) -> dict:
    return {"paths": {"exports_root": str(tmp_path / "exports"), "tripo_inbox": str(tmp_path / "exports" / "TripoPacks" / "inbox")}}


@pytest.fixture
def make_runtime(tmp_path):
    """``make(**settings)``: an app on an empty home whose exports go to the temp folder; the scheduler runs while the client is open."""
    clients = []

    def make(providers_mode: str = "mock", **settings):
        app = make_app(tmp_path / "home", providers_mode=providers_mode)
        rt = app.state.rt
        rt.update_settings({**_paths(tmp_path), **settings})
        c = make_client(app)
        c.__enter__()
        clients.append(c)
        return rt, c

    yield make
    for c in reversed(clients):
        c.__exit__(None, None, None)


@pytest.fixture(scope="module")
def board_snapshot(tmp_path_factory):
    """PARTS to Gate 2 once (every tile settled), then the app is closed and its home folder is kept: ``(home, project_id)``."""
    import time

    from pfix import make_project, wait_gate, wait_tiles_settled

    from duoskin.pipeline import parts

    base = tmp_path_factory.mktemp("board")
    app = make_app(base / "home", providers_mode="mock")
    rt = app.state.rt
    rt.update_settings(_paths(base))
    with make_client(app):
        p, _ = make_project(rt, mesh_mode="api")
        parts.start_parts_job(rt, p.id)
        t0 = time.time()
        gate = wait_gate(rt, p.id, "part_board", timeout=400)
        gate = wait_tiles_settled(rt, p.id, gate.id, timeout=400)
        states = {t.tile_id: t.state.value for t in gate.tiles}
        print(f"\n[board] gate 2 after {time.time() - t0:.0f} s: {states}")
        assert all(v == "ready" for v in states.values()), states
    return base / "home", p.id


@pytest.fixture
def board(board_snapshot, tmp_path):
    """``(rt, client, project)`` on a copy of the shared Gate 2 board (a project in mock mode, API 3D mode)."""
    home, project_id = board_snapshot
    shutil.copytree(home, tmp_path / "home")
    app = make_app(tmp_path / "home", providers_mode="mock")
    rt = app.state.rt
    rt.update_settings(_paths(tmp_path))
    c = make_client(app)
    c.__enter__()
    try:
        yield rt, c, rt.repo.get_project(project_id)
    finally:
        c.__exit__(None, None, None)
