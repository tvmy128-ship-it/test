"""Fixtures of the browser end-to-end test (``tests/e2e_ui``): the REAL app (``create_app`` with mock providers, the scheduler and every pipeline
step running) on a loopback port in a uvicorn thread, driven by Chromium through Playwright, brief to export. Chromium is pre-installed
(``PLAYWRIGHT_BROWSERS_PATH``, default ``/opt/pw-browsers``); never run ``playwright install``.

``start_app(faults=...)`` makes one app on a fresh DATA folder (exports and the Tripo inbox go to the test's temp folder); ``open_page()`` makes a
browser page that records console errors and Content-Security-Policy violations. ``shot(name)`` writes a palette-reduced PNG to
``tests/e2e_ui/screenshots/`` (one per stage; the files are for a human or a model to look at).

Process state is isolated per test: the mock providers keep their Tripo tasks in memory and read ``DUOSKIN_MOCK_FAULTS`` once, so the provider
registry is reset before and after, and the engine's step handlers are restored.
"""
from __future__ import annotations

import glob
import io
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from duoskin.engine import registry as eng_registry
from duoskin.engine.testkit import make_app, wait_for
from duoskin.models import kitenums
from duoskin.pipeline import export, kits
from duoskin.providers import registry as provider_registry

try:
    from playwright.sync_api import sync_playwright
except ImportError:   # pragma: no cover
    sync_playwright = None

SCREENSHOTS = Path(__file__).parent / "screenshots"


def chromium_path() -> str | None:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    found = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
    return found[-1] if found else None


@pytest.fixture(scope="session")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed")
    exe = chromium_path()
    if exe is None:
        pytest.skip("no pre-installed Chromium found")
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=exe, headless=True,
                               args=["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        yield b
        b.close()


@dataclass
class Live:
    url: str
    rt: object
    app: object
    home: Path
    exports: Path


@pytest.fixture
def isolated_process_state(monkeypatch):
    saved = eng_registry.handlers_snapshot()
    provider_registry.reset()
    monkeypatch.setenv("DUOSKIN_MESH_INPROC", "1")          # the mesh worker's code in this process: faster (the subprocess path has its own test)
    monkeypatch.setenv("DUOSKIN_NO_INBOX_THREAD", "1")      # a model is handed in through the Build page's drop box, not through the inbox folder
    monkeypatch.delenv("DUOSKIN_MOCK_FAULTS", raising=False)
    kits.DEMO_OVERRIDE = None
    export.ALLOW_MOCK_FOR_TESTS = False
    yield monkeypatch
    kits.DEMO_OVERRIDE = None
    export.ALLOW_MOCK_FOR_TESTS = False
    kitenums.set_default_inventory(None)
    provider_registry.reset()
    eng_registry.clear()
    for h in saved.values():
        eng_registry.register(h, replace=True)


@pytest.fixture
def start_app(tmp_path, isolated_process_state):
    """``start_app(faults="", demo=True)``: the app on a loopback port with the scheduler running; stopped at the end of the test."""
    import uvicorn

    from duoskin import winplat

    running: list[tuple] = []

    def start(faults: str = "", demo: bool = True) -> Live:
        if faults:
            isolated_process_state.setenv("DUOSKIN_MOCK_FAULTS", faults)
        provider_registry.reset()
        home = tmp_path / "home"
        exports = tmp_path / "exports"
        app = make_app(home, providers_mode="mock")
        rt = app.state.rt
        rt.update_settings({"paths": {"exports_root": str(exports), "tripo_inbox": str(exports / "TripoPacks" / "inbox")}, "demo_mode": demo})
        sock = winplat.bind_socket(0)
        rt.port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=2))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        wait_for(lambda: server.started, 30, message="uvicorn to start")
        running.append((rt, server, thread))
        return Live(f"http://127.0.0.1:{rt.port}", rt, app, home, exports)

    yield start
    for rt, server, thread in running:
        rt.bus.close()
        server.should_exit = True
        thread.join(15)


def _save_small(png: bytes, path: Path) -> None:
    """Quantise to 128 colours and optimise: UI screenshots are flat colours, so this is visually lossless and 3-4x smaller."""
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    im = im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    im.save(path, "PNG", optimize=True)


class UI:
    """A page plus what went wrong in it: console errors, page errors, Content-Security-Policy violations."""

    def __init__(self, page, live: Live, prefix: str = "") -> None:
        self.page = page
        self.live = live
        self.prefix = prefix
        self.errors: list[str] = []
        page.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));")
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))

    def _console(self, msg) -> None:
        if msg.type != "error":
            return
        text = msg.text
        # the API answers 4xx/5xx on purpose (a stale version, a blocked export, a missing pack): the UI handles each, and the test asserts the outcome
        if "Failed to load resource" in text and any(code in text for code in ("404", "409", "403", "422", "501")):
            return
        self.errors.append(text)

    def goto(self, hash_path: str = "/") -> None:
        self.page.goto(f"{self.live.url}/#{hash_path}")
        self.page.wait_for_selector("main h1, main .panel, main .empty", timeout=20000)

    def shot(self, name: str, *, full: bool = True) -> Path:
        SCREENSHOTS.mkdir(exist_ok=True)
        path = SCREENSHOTS / f"{self.prefix}{name}.png"
        _save_small(self.page.screenshot(full_page=full), path)
        return path

    def wait_until(self, predicate, timeout: float = 30.0, message: str = "condition"):
        """Poll ``predicate`` while letting Playwright run its handlers (``time.sleep`` would starve them)."""
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out after {timeout:g}s waiting for {message}")
            self.page.wait_for_timeout(100)

    def text(self, selector: str = "main") -> str:
        return self.page.locator(selector).inner_text()

    def no_errors(self) -> None:
        assert not self.errors, "browser reported errors:\n" + "\n".join(self.errors)
        violations = self.page.evaluate("window.__csp || []")
        assert not violations, f"Content-Security-Policy violations: {violations}"


@pytest.fixture
def open_page(browser):
    contexts = []

    def make(live: Live, *, prefix: str = "", width: int = 1280, height: int = 800) -> UI:
        ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme="light", device_scale_factor=1, accept_downloads=True)
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(20000)
        return UI(page, live, prefix)

    yield make
    for c in contexts:
        c.close()
