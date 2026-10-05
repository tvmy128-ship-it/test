"""The scheduler (APP_SPEC §5.1, §8.3, §8.4).

One scheduler thread claims READY steps (and WAITING_REMOTE steps whose poll time came) and hands them to three pools of
daemon worker threads: ``api`` (6), ``cpu`` (min(4, cpu_count - 1)) and ``proc`` (1). Per-provider concurrency limits
(anthropic 3, openai 3, recraft 2, tripo 2, gemini 2 by default) are applied *before* the claim by excluding the kinds of
saturated providers from the claim query, so a rate-limited provider never blocks other work.

Claiming is one ``UPDATE ... WHERE id=(SELECT ... LIMIT 1) AND state='ready' RETURNING`` inside ``BEGIN IMMEDIATE``
(``StepOps.claim_one``): no step is ever claimed twice. Every completion is fenced by the attempt number, so a worker that
lost its lease cannot overwrite a newer attempt.

Public interface (APP_SPEC §5.4)::

    scheduler.start() / stop()
    scheduler.submit_job(kind, project_id, params) -> Job
    scheduler.spawn(job_id, steps) -> list[Step]
    scheduler.pause(project_id) / resume(project_id)

plus ``register_job_factory(kind, fn)`` so the pipeline can say which steps a new job of that kind consists of.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from duoskin import winplat
from duoskin.engine import errors, registry
from duoskin.engine.cache import CachedResult, cache_key
from duoskin.engine.context import StepContext, kill_process_tree
from duoskin.engine.errors import Cancelled, StepFailure, is_cancelled
from duoskin.engine.registry import Pending, StepResult
from duoskin.models.common import utcnow
from duoskin.models.cost import Estimate
from duoskin.models.job import Job, JobKind, Step, StepState

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.scheduler")

POOLS = ("api", "cpu", "proc")
FIRST_POLL_DELAY_S = 5.0
POLL_START_S, POLL_MAX_S, POLL_GROWTH = 3.0, 15.0, 1.4
RECLAIM_EVERY_S = 15.0

JobFactory = Callable[["Runtime", Job, Any], "list[Step]"]
_factories: dict[str, JobFactory] = {}


def register_job_factory(kind: JobKind | str, fn: JobFactory) -> None:
    """``fn(rt, job, project) -> list[Step]``: the steps of a new job of ``kind`` (called by ``submit_job``)."""
    _factories[JobKind(kind).value] = fn


def has_job_factory(kind: JobKind | str) -> bool:
    return JobKind(kind).value in _factories


def poll_delay_s(polls: int) -> float:
    """5 s before the first poll, then 3 s growing x1.4 up to 15 s (§8.3)."""
    if polls <= 0:
        return FIRST_POLL_DELAY_S
    return min(POLL_START_S * (POLL_GROWTH ** (polls - 1)), POLL_MAX_S)


class WorkerPool:
    """A fixed set of daemon threads (daemon, so a stuck SDK call can never keep the process alive)."""

    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = max(1, size)
        self._q: queue.SimpleQueue[Callable[[], None] | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._inflight = 0
        self._threads: list[threading.Thread] = []
        self._closed = False

    def _ensure_threads(self) -> None:
        while len(self._threads) < self.size:
            t = threading.Thread(target=self._work, name=f"duoskin-{self.name}-{len(self._threads)}", daemon=True)
            t.start()
            self._threads.append(t)

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                item()
            except Exception:
                log.exception("worker crashed in pool %s", self.name)
            finally:
                with self._lock:
                    self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def has_capacity(self) -> bool:
        return not self._closed and self.inflight < self.size

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no work item is running or queued. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while self.inflight > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def submit(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._inflight += 1
            self._ensure_threads()
        self._q.put(fn)

    def shutdown(self) -> None:
        """Drop queued work and let the threads end (``executor.shutdown(wait=False, cancel_futures=True)``)."""
        self._closed = True
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        for _ in self._threads:
            self._q.put(None)


class Scheduler:
    def __init__(self, rt: Runtime, *, api_threads: int = 6, cpu_threads: int | None = None, proc_threads: int = 1,
                 tick_interval_s: float = 0.2) -> None:
        self.rt = rt
        self.tick_interval_s = tick_interval_s
        cpu = cpu_threads if cpu_threads is not None else max(1, min(4, (os.cpu_count() or 2) - 1))
        self._sizes = {"api": api_threads, "cpu": cpu, "proc": proc_threads}
        self.pools: dict[str, WorkerPool] = self._make_pools()
        self.stopping = False
        self._wake = threading.Condition()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._provider_inflight: dict[str, int] = defaultdict(int)
        self._cancel_events: dict[str, threading.Event] = {}
        self.paused_providers: dict[str, str] = {}
        self.queue_paused: str | None = None
        self._focus: tuple[str, str | None] | None = None
        self._last_reclaim = 0.0
        self._awake = False
        self.claimed_total = 0

    # ------------------------------------------------------------------------------------------------ lifecycle
    def _make_pools(self) -> dict[str, WorkerPool]:
        return {name: WorkerPool(name, size) for name, size in self._sizes.items()}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if any(pool._closed for pool in self.pools.values()):   # restarted after stop()
            self.pools = self._make_pools()
        self.stopping = False
        self._thread = threading.Thread(target=self._loop, name="duoskin-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop claiming, set every cancel flag, drop queued work. Running steps stay RUNNING in the DB: leases and
        recovery make a hard stop safe (§4.3)."""
        self.stopping = True
        with self._lock:
            for ev in self._cancel_events.values():
                ev.set()
        self.notify()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)
        self._thread = None
        deadline = time.monotonic() + max(timeout, 0.0)
        for pool in self.pools.values():
            pool.wait_idle(max(deadline - time.monotonic(), 0.0))   # cancelled steps usually finish within milliseconds
        for pool in self.pools.values():
            pool.shutdown()
        if self._awake:
            winplat.keep_awake(False)
            self._awake = False

    def notify(self) -> None:
        with self._wake:
            self._wake.notify_all()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self.stopping

    def _loop(self) -> None:
        while not self.stopping:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            with self._wake:
                if self.stopping:
                    break
                self._wake.wait(self.tick_interval_s)

    # ------------------------------------------------------------------------------------------------ one tick
    def tick(self) -> int:
        """Promote ready steps and claim what the pools can take. Returns the number of steps dispatched.
        (Public so tests can drive the scheduler deterministically without the thread.)"""
        ops = self.rt.ops
        ops.promote_pending()
        now = time.monotonic()
        if now - self._last_reclaim >= RECLAIM_EVERY_S:
            self._last_reclaim = now
            from duoskin.engine.recovery import recover

            recover(self.rt, expired_only=True)
        dispatched = 0
        if self.queue_paused is None:
            paused_projects = self._paused_projects()
            for pool_name in POOLS:
                pool = self.pools[pool_name]
                while pool.has_capacity() and not self.stopping:
                    step, polling = None, False
                    excl = self._excluded_kinds()
                    step = ops.claim_one(pool_name, self.rt.instance_id, polls=True, exclude_kinds=excl,
                                         exclude_projects=paused_projects)
                    polling = step is not None
                    if step is None:
                        step = ops.claim_one(pool_name, self.rt.instance_id, exclude_kinds=excl,
                                             exclude_projects=paused_projects)
                    if step is None:
                        break
                    self._dispatch(step, polling)
                    dispatched += 1
        self._update_keep_awake()
        self.claimed_total += dispatched
        return dispatched

    def _paused_projects(self) -> list[str]:
        rows = self.rt.db.conn().execute("SELECT id FROM projects WHERE json_extract(json,'$.paused')=1").fetchall()
        return [r["id"] for r in rows]

    def _excluded_kinds(self) -> list[str]:
        settings = self.rt.effective_settings()
        blocked_reason = self.rt.paid_blocked_reason()
        excluded: list[str] = []
        with self._lock:
            inflight = dict(self._provider_inflight)
        for kind, handler in registry.handlers_snapshot().items():
            prov = handler.provider
            if prov is None:
                continue
            paused = prov in self.paused_providers
            saturated = inflight.get(prov, 0) >= settings.providers.concurrency_for(prov)
            held_back = bool(handler.paid and blocked_reason and settings.mode_of(prov).value == "real")
            if paused or saturated or held_back:
                excluded.append(kind)
        return excluded

    def _update_keep_awake(self) -> None:
        busy = self.rt.db.conn().execute(
            "SELECT 1 FROM steps WHERE state IN ('ready','running','waiting_remote') LIMIT 1").fetchone() is not None
        if busy != self._awake:
            self._awake = busy
            winplat.keep_awake(busy)   # the flag is per thread: this runs on the scheduler thread

    # ------------------------------------------------------------------------------------------------ dispatch
    def _cancel_event(self, step_id: str) -> threading.Event:
        with self._lock:
            ev = self._cancel_events.get(step_id)
            if ev is None:
                ev = self._cancel_events[step_id] = threading.Event()
            return ev

    def signal_cancel(self, step_id: str) -> None:
        """Set the cancel flag of a running step and kill its child process tree, if any."""
        with self._lock:
            ev = self._cancel_events.get(step_id)
        if ev is not None:
            ev.set()
        step = self.rt.repo.find_step(step_id)
        if step is not None and step.child_pid:
            kill_process_tree(step.child_pid, step.child_create_time or None)

    def _dispatch(self, step: Step, polling: bool) -> None:
        handler = registry.find(step.kind)
        provider = handler.provider if handler is not None else None
        self._cancel_event(step.id)
        if provider:
            with self._lock:
                self._provider_inflight[provider] += 1

        def run() -> None:
            try:
                self._execute(step, polling)
            except Exception:
                log.exception("step %s crashed the worker", step.id)
            finally:
                with self._lock:
                    if provider:
                        self._provider_inflight[provider] -= 1
                    self._cancel_events.pop(step.id, None)
                self.notify()

        self.pools[step.pool].submit(run)

    # ------------------------------------------------------------------------------------------------ execute
    def _execute(self, step: Step, polling: bool) -> None:
        rt = self.rt
        handler = registry.find(step.kind)
        ctx: StepContext | None = None
        key: str | None = step.cache_key
        try:
            if handler is None:
                raise StepFailure(f"no handler is registered for step kind '{step.kind}'", kind="bad_request", billed="no")
            project = rt.repo.find_project(step.project_id) if step.project_id else None
            inputs = [rt.cas.get_asset(sha) for sha in step.inputs]
            try:
                params: Any = handler.Params.model_validate(step.params) if handler.Params is not None else dict(step.params)
            except ValidationError as exc:
                raise StepFailure(f"bad step parameters: {exc}", kind="bad_request", billed="no") from exc
            ctx = StepContext(rt, step, project, params, inputs, self._cancel_event(step.id),
                              hints=step.result.get("_hints") if isinstance(step.result.get("_hints"), dict) else None)
            if ctx.cancelled:
                raise Cancelled(step.id)
            if polling:
                out = handler.poll(ctx, params, step.remote_ref or "")
            else:
                cacheable = getattr(handler, "cacheable", True)
                if cacheable:
                    key = cache_key(step, handler, params, inputs)
                    hit = rt.cache.lookup(key)
                    if hit is not None:
                        self._finish_cached(step, key, hit)
                        return
                    step = self._store_key(step, key)
                if handler.paid:
                    est = float(handler.estimate(params))
                    estimate = Estimate(usd=est, provider=handler.provider or "mock", operation="reserve")   # type: ignore[arg-type]
                    job = rt.repo.find_job(step.job_id)
                    if job is not None and job.kind == JobKind.REGRESSION:
                        # decided once for the whole job (APP_SPEC §3.9): above ``regression_ask_usd`` it waits for the user
                        job_est = float(job.params.get("estimate_usd") or est)
                        check = rt.budget.regression_check(job_est, confirmed=step.budget_ok or bool(job.params.get("budget_confirmed")))
                        if not check.ok:
                            self._open_budget_gate(step, job_est, check)
                            return
                        rt.budget.check_and_reserve(step.project_id, estimate, step_id=step.id, attempt=step.attempt, budget_ok=True)
                    else:
                        check, _reservation = rt.budget.check_and_reserve(
                            step.project_id, estimate, step_id=step.id, attempt=step.attempt, budget_ok=step.budget_ok)
                        if not check.ok:
                            self._open_budget_gate(step, est, check)
                            return
                out = handler.run(ctx, params, inputs)
            self._finish(step, handler, ctx, out, key, polling)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._handle_exception(step, handler, ctx, exc)

    def _store_key(self, step: Step, key: str) -> Step:
        updated = self.rt.repo.mutate_step(step.id, lambda s: setattr(s, "cache_key", key),
                                           guard=lambda s: s.state == StepState.RUNNING and s.attempt == step.attempt)
        if updated is None:
            raise Cancelled(step.id)
        return updated

    def _finish_cached(self, step: Step, key: str, hit: CachedResult) -> None:
        def apply(s: Step) -> None:
            s.cached = True
            s.outputs = list(hit.outputs)
            s.result = dict(hit.result)
            s.progress = 1.0
            s.message = "cached ($0)"
            s.cache_key = key
            s.error = None

        self.rt.ops.transition(step.id, StepState.SUCCEEDED, expect=StepState.RUNNING, attempt=step.attempt, update=apply)
        self.notify()

    def _open_budget_gate(self, step: Step, est: float, check: Any) -> None:
        step = self.rt.repo.mutate_step(step.id, lambda s: setattr(s, "cost_estimate_usd", est)) or step
        self.rt.gates.open_budget_gate(step, check.facts())

    def _finish(self, step: Step, handler: Any, ctx: StepContext, out: StepResult | Pending, key: str | None,
                polling: bool) -> None:
        rt = self.rt
        if ctx.gate_opened:
            return   # the step is WAITING_USER; the gate decision completes it
        if isinstance(out, Pending):
            fresh = rt.repo.get_step(step.id)
            if not fresh.remote_ref and fresh.remote_state != "submission_uncertain":
                raise StepFailure("a handler returned Pending without calling ctx.set_remote_ref()", kind="bad_request",
                                  billed="unknown")
            delay = out.delay_s if out.delay_s is not None else poll_delay_s(fresh.polls)

            def to_waiting(s: Step) -> None:
                s.polls += 1
                s.not_before = utcnow() + timedelta(seconds=max(0.0, delay))
                if out.message:
                    s.message = out.message
                if out.progress is not None:
                    s.progress = max(0.0, min(1.0, out.progress))
                if s.remote_state in ("none", "submitted"):
                    s.remote_state = "polling"

            rt.ops.transition(step.id, StepState.WAITING_REMOTE, expect=StepState.RUNNING, attempt=step.attempt,
                              update=to_waiting)
            return
        if out is None:
            out = StepResult()
        if not isinstance(out, StepResult):
            raise StepFailure(f"handler '{step.kind}' returned {type(out).__name__}, expected StepResult or Pending",
                              kind="bad_request", billed="unknown")
        outputs = list(out.outputs) if out.outputs is not None else list(ctx.put_shas)
        for sha in outputs:
            if not rt.cas.exists(sha):
                raise StepFailure(f"output {sha[:12]} is not in the content store", kind="bad_request", billed="unknown")

        def apply(s: Step) -> None:
            s.outputs = outputs
            s.result = dict(out.result)
            s.progress = 1.0
            s.message = out.message or "done"
            s.error = None
            s.cached = False

        with rt.db.tx():
            done = rt.ops.transition(step.id, StepState.SUCCEEDED, expect=StepState.RUNNING, attempt=step.attempt, update=apply)
            if done is not None:
                cache_key_ = key or done.cache_key
                if (cache_key_ and getattr(handler, "cacheable", True) and not out.result.get("validation_errors")):
                    rt.cache.store(cache_key_, CachedResult(step_kind=step.kind, outputs=outputs, result=dict(out.result)))
                rt.budget.settle_step(done, outcome="success")
        if done is None:
            log.info("step %s attempt %s finished after its lease was lost; result discarded", step.id, step.attempt)
            rt.budget.settle_step(step, outcome="cancelled", billed="yes" if ctx.cost_recorded else "unknown")
        self.notify()

    # ------------------------------------------------------------------------------------------------ errors
    def _handle_exception(self, step: Step, handler: Any, ctx: StepContext | None, exc: BaseException) -> None:
        rt = self.rt
        ops = rt.ops
        provider = getattr(handler, "provider", None)
        recorded = bool(ctx and ctx.cost_recorded)
        if is_cancelled(exc):
            if self.stopping:
                return   # leave it RUNNING: recovery requeues it at the next start
            ops.transition(step.id, StepState.CANCELLED, expect=StepState.RUNNING, attempt=step.attempt,
                           update=lambda s: setattr(s, "message", "cancelled"))
            rt.budget.settle_step(step, outcome="cancelled", billed="yes" if recorded else "unknown")
            self.notify()
            return
        decision = errors.decide(step, exc, provider)
        err = decision.error
        if decision.action == "fail" or decision.action == "retry":
            log.warning("step %s (%s) attempt %d failed: %s: %s", step.id, step.kind, step.attempt, err.kind, err.message)
        if not isinstance(exc, (errors.StepFailure,)) and err.kind == "other":
            log.error("step %s (%s) raised an unexpected error", step.id, step.kind, exc_info=exc)
        billed = "yes" if recorded and err.billed == "no" else err.billed
        try:
            if decision.action == "validation_ok":
                def ok(s: Step) -> None:
                    s.result = {"validation_errors": [err.message]}
                    s.progress = 1.0
                    s.message = "answer did not validate"
                    s.error = None

                with rt.db.tx():
                    ops.transition(step.id, StepState.SUCCEEDED, expect=StepState.RUNNING, attempt=step.attempt, update=ok)
                    rt.budget.settle_step(step, outcome="success")
            elif decision.action == "wait_remote":
                def uncertain(s: Step) -> None:
                    s.remote_state = "submission_uncertain"
                    s.not_before = utcnow() + timedelta(seconds=decision.delay_s)
                    s.message = err.user_hint
                    s.error = err

                ops.transition(step.id, StepState.WAITING_REMOTE, expect=StepState.RUNNING, attempt=step.attempt, update=uncertain)
            elif decision.action == "retry":
                def again(s: Step) -> None:
                    s.not_before = utcnow() + timedelta(seconds=decision.delay_s) if decision.delay_s > 0 else None
                    s.error = err
                    s.message = f"retrying in {decision.delay_s:.0f}s: {err.user_hint}" if decision.delay_s else err.user_hint
                    s.result = {**s.result, **{k: v for k, v in decision.hints.items() if k.startswith("_")},
                                "_hints": {**(s.result.get("_hints") or {}), **{k: v for k, v in decision.hints.items()
                                                                                if not k.startswith("_")}}}

                with rt.db.tx():
                    ops.transition(step.id, StepState.READY, expect=StepState.RUNNING, attempt=step.attempt, update=again)
                    rt.budget.settle_step(step, outcome="failed", billed=billed)
            else:
                def failed(s: Step) -> None:
                    s.error = err
                    s.message = err.user_hint or err.message

                with rt.db.tx():
                    ops.transition(step.id, StepState.FAILED, expect=StepState.RUNNING, attempt=step.attempt, update=failed)
                    rt.budget.settle_step(step, outcome="failed", billed=billed)
                    if decision.pause_queue:
                        self.pause_queue(f"{provider or 'a provider'} reported a billing problem")
                    if decision.pause_provider:
                        self.pause_provider(decision.pause_provider, err.user_hint or "the key was rejected")
        except Exception:
            log.exception("could not record the outcome of step %s", step.id)
        self.notify()

    # ------------------------------------------------------------------------------------------------ pausing
    def pause_queue(self, reason: str) -> None:
        self.queue_paused = reason
        self.rt.bus.emit("toast", {"message": f"Queue paused: {reason}", "level": "error", "code": "queue_paused"})

    def resume_queue(self) -> None:
        was = self.queue_paused
        self.queue_paused = None
        if was:
            self.rt.bus.emit("toast", {"message": "Queue resumed", "level": "info", "code": "queue_resumed"})
        self.notify()

    def pause_provider(self, provider: str, reason: str) -> None:
        self.paused_providers[provider] = reason
        self.rt.bus.emit("toast", {"message": f"{provider} paused: {reason}", "level": "error", "code": "provider_paused",
                                   "provider": provider})

    def resume_provider(self, provider: str) -> None:
        if self.paused_providers.pop(provider, None) is not None:
            self.notify()

    def pause(self, project_id: str) -> None:
        self.rt.repo.mutate_project(project_id, lambda p: setattr(p, "paused", True))
        self.rt.ops.refresh_project_jobs(project_id)

    def resume(self, project_id: str) -> None:
        self.rt.repo.mutate_project(project_id, lambda p: setattr(p, "paused", False))
        self.rt.ops.refresh_project_jobs(project_id)
        self.notify()

    def focus(self, project_id: str, part_id: str | None) -> None:
        """The UI is looking at this tile: its steps get priority 10 (the previous focus goes back to 100)."""
        if self._focus is not None and self._focus != (project_id, part_id):
            old_p, old_part = self._focus
            self.rt.ops.set_priority(old_p, old_part, 100, only_lower_than=100)
        self._focus = (project_id, part_id)
        self.rt.ops.set_priority(project_id, part_id, 10)
        self.notify()

    # ------------------------------------------------------------------------------------------------ jobs
    def submit_job(self, kind: JobKind | str, project_id: str | None, params: dict[str, Any] | None = None, *,
                   steps: list[Step] | None = None, spec_id: str | None = None, parent_job_id: str | None = None) -> Job:
        """Create a job. ``steps`` (or the registered job factory for ``kind``) provide its steps."""
        job = self.rt.ops.submit_job(kind, project_id, params, spec_id=spec_id, parent_job_id=parent_job_id)
        if steps is None and JobKind(kind).value in _factories:
            project = self.rt.repo.find_project(project_id) if project_id else None
            steps = _factories[JobKind(kind).value](self.rt, job, project)
        if steps:
            self.spawn(job.id, steps)
        return self.rt.repo.get_job(job.id)

    def spawn(self, job_id: str, steps: list[Step]) -> list[Step]:
        for s in steps:
            s.job_id = job_id
        self.rt.ops.insert(steps)
        self.notify()
        return steps

    def new_step(self, kind: str, job_id: str, **kw: Any) -> Step:
        """``StepOps.new_step`` shortcut: a ``Step`` for a registered handler (pool, paid, version filled in)."""
        return self.rt.ops.new_step(kind, job_id=job_id, **kw)

    def cancel_job(self, job_id: str) -> Job:
        job = self.rt.ops.cancel_job(job_id)
        self.notify()
        return job

    def retry_step(self, step_id: str) -> Step:
        step = self.rt.ops.retry_step(step_id)
        self.notify()
        return step
