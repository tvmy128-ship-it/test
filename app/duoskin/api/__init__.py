"""HTTP API (APP_SPEC §13). Every router module exposes ``router``; ``app.py`` includes them all.

Owned by the foundation track: ``health``, ``state``, ``events``, ``settings``, ``keys``, ``doctor``, ``projects``,
``jobs``, ``costs``, ``shutdown``, ``focus``, ``gates`` (the generic lifecycle) and ``assets`` (CAS serving).
Stubs for later tracks to fill, each answering ``501 not_implemented``: ``parts``, ``exports`` (routes marked ``# STUB``).
Optional routers other tracks add by creating ``duoskin/api/<name>.py`` with a ``router`` (loaded automatically if
present): ``specs``, ``uploads``, ``imports``, ``library``, ``calibration``, ``learning``, ``os_open``, ``regression``,
``versions``.
"""
from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request

if TYPE_CHECKING:
    from fastapi import FastAPI

    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.api")

CORE_ROUTERS = ("health", "state", "events", "settings", "keys", "doctor", "projects", "jobs", "costs", "shutdown",
                "focus", "gates", "parts", "assets", "exports")
OPTIONAL_ROUTERS = ("specs", "briefs", "plans", "uploads", "imports", "library", "calibration", "learning", "os_open", "regression",
                    "versions")


def get_rt(request: Request) -> Runtime:
    """FastAPI dependency: the ``Runtime`` of this app."""
    return request.app.state.rt


RT = Depends(get_rt)   # use as ``rt: Runtime = RT`` (a shared dependency object keeps the signatures free of calls)


def not_implemented(feature: str, track: str) -> dict[str, Any]:
    return {"error": "not_implemented", "message": f"{feature} is not available in this build yet ({track} track)"}


def include_routers(app: FastAPI) -> list[str]:
    """Include the core routers and every optional router module that exists. Returns the module names loaded."""
    loaded: list[str] = []
    for name in CORE_ROUTERS:
        module = importlib.import_module(f"duoskin.api.{name}")
        app.include_router(module.router)
        loaded.append(name)
    for name in OPTIONAL_ROUTERS:
        try:
            module = importlib.import_module(f"duoskin.api.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"duoskin.api.{name}":
                continue
            log.exception("optional router %s failed to import", name)
            continue
        except Exception:
            log.exception("optional router %s failed to import", name)
            continue
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
            loaded.append(name)
    return loaded
