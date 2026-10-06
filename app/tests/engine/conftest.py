"""Fixtures for the engine tests: a temp-folder Runtime, a clean handler registry and a few helpers."""
from __future__ import annotations

import pytest

from duoskin.engine import errors, registry
from duoskin.engine.testkit import make_runtime


@pytest.fixture(autouse=True)
def clean_registry():
    saved = registry.handlers_snapshot()
    registry.clear()
    yield
    registry.clear()
    for h in saved.values():
        registry.register(h, replace=True)


@pytest.fixture
def fast_backoff(monkeypatch):
    """Retries wait 20 ms instead of 1-32 s."""
    monkeypatch.setattr(errors, "backoff_s", lambda attempt, rng=None: 0.02)


@pytest.fixture
def rt(tmp_path):
    """A started-but-threadless runtime: drive it with ``rt.scheduler.tick()`` or start the scheduler thread yourself."""
    runtime = make_runtime(tmp_path / "home", providers_mode="mock")
    runtime.startup(start_threads=False, backup=False)
    yield runtime
    runtime.shutdown()


@pytest.fixture
def live(rt):
    """The same runtime with the scheduler thread running."""
    rt.scheduler.start()
    return rt
