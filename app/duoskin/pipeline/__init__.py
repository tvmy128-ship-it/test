"""The pipeline lanes (APP_SPEC §8.2, §10): step handlers, job factories and gate appliers behind one hook.

``create_app`` imports this package and calls ``register(rt)`` once (``app.load_plugins``). ``register`` registers every lane of this
track (the asset loop, the part board, prints, clothing, colours, face, hair, accessories, build, manual 3D, polish, duo, export,
library) and, when they exist, the modules of the plan and concept lanes (``plan``, ``concept``, ``lint``, ``change``, ``drills``,
``regression``) that another track owns: each of them may define ``register(rt)``.

Handlers are process-wide (``engine.registry``); gate appliers belong to the runtime (``rt.gates``), which is why ``register`` takes it.
``register_all()`` registers only the handlers (tests that have no runtime yet).

Reading order for a new contributor: ``assetloop`` (the generic 2D asset sub-graph), ``parts`` (the board and Gate 2), the lanes
(``prints``, ``clothing``, ``colours``, ``face``, ``hair``, ``accessory``), ``build`` (BUILD after Gate 2, API and manual 3D),
``duo`` (renders, duo checks, Gate 3) and ``export`` (the upload kit).
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

log = logging.getLogger("duoskin.pipeline")

#: lane modules of this track, in registration order (a later module may rely on an earlier one's lane or callback)
OWN_MODULES = ("assetloop", "parts", "prints", "clothing", "colours", "multiview", "face", "hair", "accessory", "build", "manual_mesh",
               "polish", "partchange", "duo", "export", "library")
#: modules that another track may provide (plan loop, concept lock, change flow, calibration); each may define ``register(rt)``
FOREIGN_MODULES = ("plan", "lint", "concept", "change", "drills", "regression")


def _register(name: str, rt: Any, *, optional: bool) -> bool:
    full = f"{__name__}.{name}"
    try:
        module = importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full and optional:
            return False
        raise
    hook = getattr(module, "register", None)
    if callable(hook):
        try:
            hook(rt)
        except TypeError:                        # a hook that takes no runtime
            hook()
    return True


def register(rt: Any | None = None) -> list[str]:
    """Register every lane. Returns the module names that registered."""
    done: list[str] = []
    for name in FOREIGN_MODULES:                 # first: this track's change flow wraps whatever applier they installed (CHANGE_CONFIRM, CLARIFY)
        try:
            if _register(name, rt, optional=True):
                done.append(name)
        except Exception:                        # noqa: BLE001 - another track's module must never break this track's lanes
            log.exception("pipeline module %s failed to register", name)
    for name in OWN_MODULES:
        if _register(name, rt, optional=True):
            done.append(name)
    try:
        from duoskin.pipeline import kits

        if rt is not None:
            kits.install_inventory(rt)
    except Exception:                            # noqa: BLE001 - the kit enums keep their defaults
        log.exception("could not install the kit inventory")
    return done


def register_all() -> list[str]:
    """Register the step handlers and job factories only (no gate appliers: there is no runtime)."""
    return register(None)
