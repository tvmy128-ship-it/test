"""Fixtures for the API tests: an in-process app on a temp DATA folder, an authenticated client and a real server for SSE."""
from __future__ import annotations

import threading

import pytest

from duoskin.engine import registry
from duoskin.engine.testkit import make_app, make_client, wait_for


@pytest.fixture(autouse=True)
def clean_registry():
    saved = registry.handlers_snapshot()
    registry.clear()
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
def anon(app):
    """A client without the token (no lifespan: use together with ``client`` when the scheduler matters)."""
    return make_client(app, authed=False)


@pytest.fixture
def live_server(tmp_path):
    """The app on a real loopback port (uvicorn in a thread) for streaming tests. Yields (base_url, runtime, token)."""
    import uvicorn

    from duoskin import winplat

    app = make_app(tmp_path / "live-home", providers_mode="mock")
    rt = app.state.rt
    sock = winplat.bind_socket(0)
    port = sock.getsockname()[1]
    rt.port = port
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    wait_for(lambda: server.started, 10, message="uvicorn to start")
    yield f"http://127.0.0.1:{port}", rt, rt.token
    rt.bus.close()
    server.should_exit = True
    thread.join(10)
