"""Helpers for tests (ours and other tracks'): in-process runtimes, apps and clients with the right ``Host``.

::

    from duoskin.engine.testkit import make_app, make_client, wait_for, png_bytes

    app = make_app(tmp_path, providers_mode="mock")          # create_app with a temp DATA folder
    with make_client(app) as client:                         # lifespan runs (scheduler thread starts); token attached
        client.get("/api/health")

Starlette's default ``testserver`` host is rejected by the Host check (SYS-22), so clients use ``http://127.0.0.1:8765``.
"""
from __future__ import annotations

import io
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

BASE_URL = "http://127.0.0.1:8765"


def make_runtime(home: Path, *, providers_mode: str | None = "mock", start: bool = False, **kw: Any):
    """A ``Runtime`` on ``home`` (not started unless ``start=True``; ``kw`` goes to ``Runtime``)."""
    from duoskin.engine.runtime import Runtime

    rt = Runtime.create(home, providers_mode=providers_mode, **kw)
    if start:
        rt.startup(backup=False)
    return rt


def make_app(home: Path | None = None, *, providers_mode: str | None = "mock", **kw: Any):
    from duoskin.app import create_app

    return create_app(home, providers_mode=providers_mode, **kw)


def make_client(app: Any, *, authed: bool = True, **kw: Any):
    """``TestClient`` bound to ``127.0.0.1``; ``authed`` adds the ``X-DuoSkin-Token`` header. Use ``with`` to run the
    lifespan (scheduler thread)."""
    from starlette.testclient import TestClient

    client = TestClient(app, base_url=BASE_URL, **kw)
    if authed:
        client.headers["X-DuoSkin-Token"] = app.state.rt.token
    return client


def wait_for(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02, message: str = "condition") -> Any:
    """Poll ``predicate`` until it returns something truthy; raises ``AssertionError`` on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {message}")
        time.sleep(interval)


def png_bytes(width: int = 8, height: int = 8, color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(buf, "PNG")
    return buf.getvalue()
