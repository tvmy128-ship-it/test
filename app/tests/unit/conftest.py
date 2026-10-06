"""Fixtures for the foundation unit tests."""
from __future__ import annotations

import pytest

from duoskin.engine.testkit import make_runtime


@pytest.fixture
def rt(tmp_path):
    runtime = make_runtime(tmp_path / "home", providers_mode="mock")
    runtime.startup(start_threads=False, backup=False)
    yield runtime
    runtime.shutdown()
