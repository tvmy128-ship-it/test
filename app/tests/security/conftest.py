"""Fixtures of the security tests: an in-process app on a temp DATA folder (the same shape as ``tests/api/conftest.py``)."""
from __future__ import annotations

import pytest

from duoskin.engine import registry
from duoskin.engine.testkit import make_app, make_client


@pytest.fixture(autouse=True)
def clean_registry():
    saved = registry.handlers_snapshot()
    yield
    registry.clear()
    for h in saved.values():
        registry.register(h, replace=True)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path / "home", providers_mode="mock")


@pytest.fixture
def client(app):
    with make_client(app) as c:
        yield c


@pytest.fixture
def rt(client, app):
    return app.state.rt


@pytest.fixture
def rt_bare(tmp_path):
    """A runtime without threads or an HTTP client (for the stores)."""
    from duoskin.engine.testkit import make_runtime

    runtime = make_runtime(tmp_path / "bare-home")
    yield runtime
    runtime.shutdown()
