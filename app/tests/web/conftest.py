"""Fixtures for the web UI tests: the real app on a loopback port (uvicorn in a thread, mock providers) and Chromium via
Playwright. Chromium is pre-installed (PLAYWRIGHT_BROWSERS_PATH); never run ``playwright install``.

``ui`` gives a page that records console errors and Content-Security-Policy violations, so a test fails when the UI breaks
the strict policy (an inline style or script) or throws.
"""
from __future__ import annotations

import glob
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from duoskin.engine.testkit import make_app, wait_for

SCREENSHOTS = Path(__file__).parent / "screenshots"

try:
    from playwright.sync_api import sync_playwright
except ImportError:   # pragma: no cover
    sync_playwright = None


def chromium_path() -> str | None:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    found = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
    return found[-1] if found else None


@dataclass
class Live:
    url: str
    rt: object
    app: object
    token: str


@pytest.fixture
def live(tmp_path):
    """The app on a real loopback port, scheduler running. ``live.rt`` is the Runtime."""
    import uvicorn

    from duoskin import winplat

    app = make_app(tmp_path / "home", providers_mode="mock")
    rt = app.state.rt
    sock = winplat.bind_socket(0)
    port = sock.getsockname()[1]
    rt.port = port
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    wait_for(lambda: server.started, 15, message="uvicorn to start")
    yield Live(f"http://127.0.0.1:{port}", rt, app, rt.token)
    rt.bus.close()
    server.should_exit = True
    thread.join(10)


@pytest.fixture
def plain_gates(live):
    """Use the engine's generic gate appliers (record the decision, approve the tile) instead of the pipeline's, for the tests
    that are about the UI and need decisions to be stored without a real spec, kits or providers behind them."""
    from duoskin.models.gate import GateKind

    gs = live.rt.gates
    for kind in (GateKind.PART_BOARD, GateKind.CONCEPT, GateKind.FINAL_PICK, GateKind.MANUAL_IMPORT):
        gs.register_applier(kind, gs._apply_default)
    return gs


@pytest.fixture
def fake_plan(monkeypatch):
    """A PLAN job made of two registered fake steps (named like the real ones, so the UI shows their plain-English labels).
    The first step holds until ``fp.release`` is set, so a test can watch it run. Registry state is restored afterwards."""
    from duoskin.engine import registry
    from duoskin.engine import scheduler as scheduler_mod
    from duoskin.engine.registry import StepResult, register_handler

    class FakePlan:
        release = threading.Event()
        fail_lint = False
        runs = 0

    fp = FakePlan()
    saved = registry.handlers_snapshot()
    monkeypatch.setattr(scheduler_mod, "_factories", dict(scheduler_mod._factories))

    def planner(ctx, params, inputs):
        ctx.progress(0.4, "Drafting three plans")
        fp.release.wait(20)
        return StepResult()

    def lint(ctx, params, inputs):
        fp.runs += 1
        if fp.fail_lint:
            from duoskin.engine.errors import StepFailure

            raise StepFailure("lint blew up", kind="bad_request", user_hint="The rules check could not run. Try again in a moment.")
        return StepResult()

    register_handler("plan.planner", planner, cacheable=False)
    register_handler("plan.lint", lint, cacheable=False)

    def factory(rt_, job, project):
        a = rt_.ops.new_step("plan.planner", job_id=job.id, project_id=project.id)
        b = rt_.ops.new_step("plan.lint", job_id=job.id, project_id=project.id, deps=[a.id])
        return [a, b]

    scheduler_mod.register_job_factory("plan", factory)
    yield fp
    fp.release.set()
    registry.clear()
    for h in saved.values():
        registry.register(h, replace=True)


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


def _save_small(png: bytes, path: Path) -> None:
    """Quantise to 128 colours and optimise: UI screenshots are flat colours, so this is visually lossless and 3-4x smaller."""
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    im = im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    im.save(path, "PNG", optimize=True)


class UI:
    """A page plus what went wrong in it."""

    def __init__(self, page, live: Live) -> None:
        self.page = page
        self.live = live
        self.errors: list[str] = []
        self.requests: list[str] = []
        # every Content-Security-Policy violation the page reports is collected (inline style/script, blob: fetch, ...)
        page.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));")
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("request", lambda r: self.requests.append(r.url))

    def _console(self, msg) -> None:
        if msg.type == "error":
            text = msg.text
            # a 404/501 answer from an API route that is not built yet is expected, and the UI handles it
            if "Failed to load resource" in text and ("404" in text or "501" in text or "409" in text or "403" in text or "422" in text):
                return
            self.errors.append(text)

    def goto(self, hash_path: str = "/") -> None:
        self.page.goto(f"{self.live.url}/#{hash_path}")
        self.page.wait_for_selector("main h1, main .panel, main .empty", timeout=15000)

    def shot(self, name: str, *, full: bool = False) -> Path:
        """Save a small PNG of the viewport to tests/web/screenshots/ (palette-reduced so it stays commit-worthy). With
        DUOSKIN_REVIEW_DIR set, a full-page copy is also written there for a human (or a model) to look at."""
        SCREENSHOTS.mkdir(exist_ok=True)
        path = SCREENSHOTS / f"{name}.png"
        png = self.page.screenshot(full_page=full)
        _save_small(png, path)
        review = os.environ.get("DUOSKIN_REVIEW_DIR")
        if review:
            Path(review).mkdir(parents=True, exist_ok=True)
            (Path(review) / f"{name}_full.png").write_bytes(self.page.screenshot(full_page=True))
        return path

    def shot_element(self, locator, name: str) -> Path:
        """A small PNG of one element (a viewer, a dialog)."""
        SCREENSHOTS.mkdir(exist_ok=True)
        path = SCREENSHOTS / f"{name}.png"
        _save_small(locator.screenshot(), path)
        return path

    def wait_until(self, predicate, timeout: float = 8.0, message: str = "condition"):
        """Poll ``predicate`` while letting Playwright run its route handlers (``time.sleep`` would starve them)."""
        import time

        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for {message}")
            self.page.wait_for_timeout(50)

    def clear_toasts(self) -> None:
        self.page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")

    def no_errors(self) -> None:
        assert not self.errors, "browser reported errors:\n" + "\n".join(self.errors)
        violations = self.page.evaluate("window.__csp || []")
        assert not violations, f"Content-Security-Policy violations: {violations}"


@pytest.fixture
def ui(browser, live):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="light", device_scale_factor=1)
    page = ctx.new_page()
    page.set_default_timeout(15000)
    helper = UI(page, live)
    yield helper
    ctx.close()


@pytest.fixture
def ui_factory(browser, live):
    """Make more pages (other tabs, other colour schemes, other sizes) against the same server."""
    contexts = []

    def make(**kw):
        kw.setdefault("viewport", {"width": 1280, "height": 800})
        ctx = browser.new_context(**kw)
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(15000)
        return UI(page, live), ctx

    yield make
    for c in contexts:
        c.close()
