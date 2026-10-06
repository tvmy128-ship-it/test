"""Step handler registry (APP_SPEC §8.2) and the contract every pipeline lane codes against.

A *step handler* does one cached, resumable unit of work. Register one with ``register_handler`` (a plain function) or
``register`` (an object that implements ``StepHandler``). The registry is process-wide and in-memory; tests can
``unregister``/``clear`` it.

Function handlers
-----------------
::

    from duoskin.engine.registry import register_handler, StepResult, Pending

    def run(ctx: StepContext, params, inputs: list[Asset]) -> StepResult | Pending:
        ...
    register_handler("img.draft", run, version=1, pool="api", paid=True, provider="openai",
                     Params=DraftParams, estimate=lambda p: 0.04 * p.n,
                     cache_fields=lambda p, inputs: {"model": "...", "prompt_sha256": "..."},
                     poll=None, cacheable=True)

* ``params``: ``Params.model_validate(step.params)`` when a ``Params`` model is given, else the plain ``dict``.
* ``inputs``: the step's declared input assets, in order (``Asset`` rows from the CAS table; read bytes with
  ``ctx.read_asset(sha)``).
* Return ``StepResult(outputs=None | [sha, ...], result={...small JSON...}, message="")`` to succeed. ``outputs=None``
  means "every asset this step put with ``ctx.put_asset``"; give an explicit list to choose.
* Return ``Pending(delay_s=None, message="", progress=None)`` after ``ctx.set_remote_ref(...)`` to wait for a remote task;
  the engine then calls ``poll(ctx, params, remote_ref)`` after 5 s, then every 3 s growing x1.4 up to 15 s.
  ``poll`` returns ``StepResult`` (done) or ``Pending`` (keep waiting) and must never block longer than one HTTP call.
* Call ``ctx.open_gate(gate)`` to wait for the user: the step becomes WAITING_USER and whatever you return is ignored.
  A gate decision applier later completes the step (``engine.gates.complete_step``) or spawns new steps.
* Raise ``ProviderError`` (or ``StepFailure``) for failures; the engine applies the §8.6 retry/fail table. Raise
  ``Cancelled`` (``ctx.check_cancel()`` does) to stop quietly.
* Never import a provider SDK: get an adapter with ``ctx.provider("openai")`` (lazy ``providers.registry.get``).
* The result must be small JSON. Never put API keys, image bytes or full prompts in ``result``.

``StepContext`` (``duoskin.engine.context``), fields and methods handlers may use
-------------------------------------------------------------------------------
* ``ctx.step`` (the ``Step`` row, read-only snapshot), ``ctx.project`` (``Project | None``), ``ctx.params``,
  ``ctx.inputs``, ``ctx.hints`` (engine hints such as ``{"max_tokens_scale": 2}`` after a truncated answer),
  ``ctx.attempt``, ``ctx.rt`` (the ``Runtime``: ``.repo``, ``.db``, ``.cas``, ``.bus``, ``.budget``, ``.settings``,
  ``.paths``, ``.scheduler``).
* cancel flag: ``ctx.cancelled`` (bool) and ``ctx.check_cancel()`` (raises ``Cancelled``).
* progress: ``ctx.progress(frac, msg="")`` (throttled to 2/s), ``ctx.heartbeat()`` (extends the lease at once).
* cas access: ``ctx.put_asset(data, ext, *, role, prov, part_id=None, status="candidate") -> Asset``,
  ``ctx.get_asset(sha) -> Asset``, ``ctx.read_asset(sha) -> bytes``.
* cost recording: ``ctx.add_cost(entry)`` / ``ctx.cost(provider, model, operation, usd, ...)`` (idempotent per
  step, attempt and operation: ENG-03), ``ctx.record_checks(results) -> list[str]``.
* events: ``ctx.emit(type, payload)`` (the step's project is filled in).
* remote tasks: ``ctx.set_remote_ref(ref, state="submitted")`` commits at once, in its own transaction.
* gates and graph: ``ctx.open_gate(gate)``, ``ctx.spawn(steps)``.
* providers: ``ctx.provider(name)``, ``ctx.call_ctx()`` (a ``providers.base.CallCtx`` with heartbeat, check_cancel and
  progress), ``ctx.run_subprocess(argv, *, timeout_s)``.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from duoskin.engine.errors import Cancelled

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.models.asset import Asset

__all__ = ["Cancelled", "FunctionHandler", "Pending", "StepHandler", "StepResult", "UnknownStepKind", "clear",
           "get", "register", "register_handler", "registered_kinds", "unregister"]

Pool = Literal["api", "cpu", "proc", "none"]


@dataclass
class StepResult:
    """Success. ``outputs=None`` means every asset the step put through ``ctx.put_asset``."""

    outputs: list[str] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass
class Pending:
    """A remote task is running: poll again later (``delay_s=None`` uses the default backoff)."""

    delay_s: float | None = None
    message: str = ""
    progress: float | None = None


@runtime_checkable
class StepHandler(Protocol):
    kind: ClassVar[str]
    version: ClassVar[int]
    pool: ClassVar[Pool]
    paid: ClassVar[bool]
    provider: ClassVar[str | None]
    Params: ClassVar[type[BaseModel] | None]

    def cache_fields(self, p: Any, inputs: list[Asset]) -> dict[str, Any]: ...
    def estimate(self, p: Any) -> float: ...
    def run(self, ctx: StepContext, p: Any, inputs: list[Asset]) -> StepResult | Pending: ...
    def poll(self, ctx: StepContext, p: Any, remote_ref: str) -> StepResult | Pending: ...


class UnknownStepKind(KeyError):
    pass


class FunctionHandler:
    """Adapts plain functions to the ``StepHandler`` protocol (instances carry the class-level attributes)."""

    def __init__(self, kind: str, fn: Callable[..., StepResult | Pending], *, version: int = 1, pool: Pool = "cpu",
                 paid: bool = False, provider: str | None = None, Params: type[BaseModel] | None = None,
                 estimate: Callable[[Any], float] | None = None,
                 cache_fields: Callable[[Any, list[Asset]], dict[str, Any]] | None = None,
                 poll: Callable[..., StepResult | Pending] | None = None, cacheable: bool = True) -> None:
        if pool not in ("api", "cpu", "proc", "none"):
            raise ValueError(f"bad pool '{pool}'")
        self.kind = kind
        self.version = version
        self.pool: Pool = pool
        self.paid = paid
        self.provider = provider
        self.Params = Params
        self.cacheable = cacheable
        self._fn = fn
        self._estimate = estimate
        self._cache_fields = cache_fields
        self._poll = poll

    def cache_fields(self, p: Any, inputs: list[Asset]) -> dict[str, Any]:
        return self._cache_fields(p, inputs) if self._cache_fields else {}

    def estimate(self, p: Any) -> float:
        return float(self._estimate(p)) if self._estimate else 0.0

    def run(self, ctx: StepContext, p: Any, inputs: list[Asset]) -> StepResult | Pending:
        return self._fn(ctx, p, inputs)

    def poll(self, ctx: StepContext, p: Any, remote_ref: str) -> StepResult | Pending:
        if self._poll is None:
            raise NotImplementedError(f"step kind '{self.kind}' has no poll()")
        return self._poll(ctx, p, remote_ref)


_lock = threading.RLock()
_handlers: dict[str, StepHandler] = {}


def register(handler: StepHandler, *, replace: bool = False) -> StepHandler:
    kind = handler.kind
    with _lock:
        if kind in _handlers and not replace:
            raise ValueError(f"a handler for '{kind}' is already registered")
        _handlers[kind] = handler
    return handler


def register_handler(kind: str, fn: Callable[..., StepResult | Pending], *, version: int = 1, pool: Pool = "cpu",
                     paid: bool = False, provider: str | None = None, Params: type[BaseModel] | None = None,
                     estimate: Callable[[Any], float] | None = None,
                     cache_fields: Callable[[Any, list[Asset]], dict[str, Any]] | None = None,
                     poll: Callable[..., StepResult | Pending] | None = None, cacheable: bool = True,
                     replace: bool = True) -> FunctionHandler:
    """Register ``fn(ctx, params, inputs)`` as the handler of step ``kind`` (re-registering replaces by default)."""
    handler = FunctionHandler(kind, fn, version=version, pool=pool, paid=paid, provider=provider, Params=Params,
                              estimate=estimate, cache_fields=cache_fields, poll=poll, cacheable=cacheable)
    register(handler, replace=replace)
    return handler


def get(kind: str) -> StepHandler:
    with _lock:
        try:
            return _handlers[kind]
        except KeyError:
            raise UnknownStepKind(kind) from None


def find(kind: str) -> StepHandler | None:
    with _lock:
        return _handlers.get(kind)


def unregister(kind: str) -> None:
    with _lock:
        _handlers.pop(kind, None)


def registered_kinds() -> list[str]:
    with _lock:
        return sorted(_handlers)


def handlers_snapshot() -> dict[str, StepHandler]:
    with _lock:
        return dict(_handlers)


def clear() -> None:
    with _lock:
        _handlers.clear()
