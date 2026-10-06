"""``create_app``: the FastAPI app factory (APP_SPEC §5.2).

``create_app(home=None, providers_mode=None)`` builds a ``Runtime`` (DATA folder, SQLite with migrations, content store,
event bus, scheduler) and an app wired with the security middleware, the routers and the static files. Tests and other
tracks start the server in-process with a temp folder::

    from duoskin.engine.testkit import make_app, make_client
    app = make_app(tmp_path, providers_mode="mock")
    with make_client(app) as client:
        assert client.get("/api/health").json()["ok"]

Threads (scheduler, heartbeat) start in the app's lifespan, i.e. when the app is used as ``with TestClient(app)`` or run by
uvicorn; the scheduler can also be driven by hand (``rt.scheduler.tick()``). ``app.state.rt`` is the ``Runtime``.

Step handlers register themselves at import time of their module. After the runtime exists the app loads these plug-in
modules if present and calls their ``register(rt)`` hook when they define one: ``duoskin.pipeline``,
``duoskin.providers.registry`` is *not* imported here (providers are imported lazily by ``ctx.provider``).
"""
from __future__ import annotations

import importlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from duoskin import __version__, config, security, winplat
from duoskin.api import include_routers
from duoskin.db.errors import ConflictError, NotFound
from duoskin.engine.gates import GateError
from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.app")

WEB_DIR = Path(__file__).resolve().parent / "web"
PLUGIN_MODULES = ("duoskin.pipeline",)


def _jsonable(obj: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    try:
        return jsonable_encoder(obj)
    except Exception:  # noqa: BLE001
        return None


def load_plugins(rt: Runtime) -> list[str]:
    """Import optional plug-in packages (the pipeline registers its step handlers) and call ``register(rt)`` if defined."""
    loaded: list[str] = []
    for name in PLUGIN_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name or (exc.name and name.startswith(exc.name)):
                continue
            log.exception("plug-in %s failed to import", name)
            continue
        except Exception:
            log.exception("plug-in %s failed to import", name)
            continue
        hook = getattr(module, "register", None)
        if callable(hook):
            try:
                hook(rt)
            except Exception:
                log.exception("plug-in %s register() failed", name)
                continue
        loaded.append(name)
    return loaded


def create_app(home: Path | str | None = None, providers_mode: str | None = None, *, runtime: Runtime | None = None,
               start_threads: bool = True, recover: bool = True, backup: bool = True, run_doctor_on_start: bool = False,
               **runtime_kwargs: Any) -> FastAPI:
    """Build the app. ``start_threads=False`` leaves the scheduler stopped (drive it with ``rt.scheduler.tick()``)."""
    winplat.fix_mimetypes()
    rt = runtime or Runtime.create(home, providers_mode=providers_mode, **runtime_kwargs)
    settings = rt.effective_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt.shutting_down = False
        rt.db._closed = False
        rt.startup(recover=recover, start_threads=start_threads, backup=backup)
        if run_doctor_on_start:
            from duoskin.api.doctor import run_in_background

            run_in_background(rt, quick=False)
        try:
            yield
        finally:
            rt.shutdown()

    app = FastAPI(title="DuoSkin Studio", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs" if settings.dev_mode else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if settings.dev_mode else None)
    app.state.rt = rt
    config.register_runtime(rt)

    index_path = WEB_DIR / "index.html"
    importmap = None
    if index_path.exists():
        try:
            importmap = security.importmap_csp_source(index_path.read_text(encoding="utf-8"))
        except OSError:
            importmap = None
    security.install(app, rt.token, importmap, get_port=lambda: rt.port)
    _install_error_handlers(app)
    include_routers(app)
    app.state.plugins = load_plugins(rt)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index() -> HTMLResponse:
        try:
            html = index_path.read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse("<!doctype html><title>DuoSkin Studio</title><p>The web page files are missing.</p>", status_code=404,
                                headers={"Cache-Control": "no-store"})
        return HTMLResponse(security.inject_token(html, rt.token), headers={"Cache-Control": "no-store"})

    if WEB_DIR.exists():
        app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
        vendor = WEB_DIR / "vendor"
        if vendor.exists():
            app.mount("/vendor", StaticFiles(directory=vendor), name="vendor")
    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFound)
    async def _not_found(_request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def _conflict(_request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": str(exc), "current": _jsonable(exc.current)}, status_code=409)

    @app.exception_handler(GateError)
    async def _gate_error(_request: Request, exc: GateError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """One error shape everywhere: ``{"error": code, "message": text}`` (routes raise ``HTTPException(detail={...})``)."""
        detail = exc.detail
        body = detail if isinstance(detail, dict) and "error" in detail else {"error": f"http_{exc.status_code}", "message": str(detail)}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # ``input`` (the offending value) and ``ctx`` are left out: a rejected body may hold a pasted API key, and an answer must not echo it
        errors = [{k: v for k, v in e.items() if k not in ("input", "ctx", "url")} for e in exc.errors()]
        return JSONResponse({"error": "validation", "message": "the request is not valid", "detail": _jsonable(errors)}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error in a route", exc_info=exc)
        return JSONResponse({"error": "server_error", "message": "something went wrong; see the logs"}, status_code=500)


def default_home_env() -> str | None:
    return os.environ.get("DUOSKIN_HOME")
