"""Step state machine primitives and job-state derivation (APP_SPEC §8.3). No threads here: the scheduler drives these.

Every transition is *guarded*: it names the states it may start from and (for work done under a lease) the attempt
number that owns the lease. A stale worker whose lease was reclaimed therefore cannot overwrite a newer attempt.

::

    PENDING -> READY -> RUNNING -> SUCCEEDED | FAILED | CANCELLED
                          |-> WAITING_REMOTE -> RUNNING (poll) ...
                          |-> WAITING_USER   -> SUCCEEDED | READY | FAILED
                          '-> READY (retry with backoff)
    SUCCEEDED -> SUPERSEDED (an upstream change replaced its output; kept for history)
    FAILED -> READY (user "Retry", attempts reset)
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from duoskin.db.errors import NotFound
from duoskin.engine import registry
from duoskin.models.common import iso_utc, new_id, utcnow
from duoskin.models.job import (
    TERMINAL_JOB_STATES,
    TERMINAL_STEP_STATES,
    Job,
    JobKind,
    JobState,
    Step,
    StepError,
    StepState,
)

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.steps")

UPSTREAM_MESSAGE = "upstream step failed or was cancelled"
LEASE_SECONDS = 120


def step_event_payload(s: Step) -> dict[str, Any]:
    payload: dict[str, Any] = {"step_id": s.id, "job_id": s.job_id, "project_id": s.project_id, "part_id": s.part_id,
                               "kind": s.kind, "state": s.state.value, "progress": s.progress, "message": s.message,
                               "cached": s.cached, "attempt": s.attempt, "remote_state": s.remote_state}
    if s.error is not None:
        payload["error"] = s.error.model_dump(mode="json")
    if s.gate_id:
        payload["gate_id"] = s.gate_id
    return payload


def derive_job_state(states: Iterable[StepState]) -> JobState:
    """APP_SPEC §8.3: running-like beats waiting-on-user beats finished. An empty job is RUNNING (steps are coming)."""
    s = list(states)
    if not s:
        return JobState.RUNNING
    if any(x in (StepState.READY, StepState.RUNNING, StepState.WAITING_REMOTE) for x in s):
        return JobState.RUNNING
    if any(x == StepState.WAITING_USER for x in s):
        return JobState.WAITING_USER
    if any(x == StepState.PENDING for x in s):
        return JobState.RUNNING          # about to be promoted
    if any(x == StepState.FAILED for x in s):
        return JobState.FAILED
    if all(x in (StepState.SUCCEEDED, StepState.SUPERSEDED) for x in s):
        return JobState.SUCCEEDED
    return JobState.CANCELLED


class StepOps:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.db = rt.db
        self.repo = rt.repo
        self.bus = rt.bus

    # ------------------------------------------------------------------------------------------------ building
    def new_step(self, kind: str, *, job_id: str, project_id: str | None = None, part_id: str | None = None,
                 params: dict[str, Any] | None = None, inputs: list[str] | None = None, deps: list[str] | None = None,
                 nonce: str = "", priority: int = 100, max_attempts: int = 3, estimate_usd: float | None = None) -> Step:
        """Build a ``Step`` for a registered handler (pool, paid and version come from the handler)."""
        handler = registry.get(kind)
        params = params or {}
        est = estimate_usd
        if est is None and handler.paid:
            try:
                p: Any = handler.Params.model_validate(params) if handler.Params is not None else params
                est = float(handler.estimate(p))
            except Exception:   # noqa: BLE001 - an estimate that cannot be computed is re-checked at run time
                est = 0.0
        return Step(id=new_id("stp"), job_id=job_id, project_id=project_id, part_id=part_id, kind=kind,   # type: ignore[arg-type]
                    handler_version=handler.version, pool=handler.pool, state=StepState.PENDING,
                    deps=list(deps or []), params=params, inputs=list(inputs or []), nonce=nonce, paid=handler.paid,
                    max_attempts=max_attempts, priority=priority, cost_estimate_usd=est or 0.0, created_at=utcnow())

    def insert(self, steps: list[Step]) -> list[Step]:
        """Insert new steps. A step with no unfinished dependencies starts READY (pool ``none`` placeholders stay put)."""
        if not steps:
            return steps
        with self.db.tx() as c:
            known = {s.id for s in steps}
            for s in steps:
                unfinished = []
                for d in s.deps:
                    if d in known:
                        unfinished.append(d)
                        continue
                    row = c.execute("SELECT state FROM steps WHERE id=?", (d,)).fetchone()
                    if row is None:
                        raise NotFound("dependency step", d)
                    if row["state"] not in ("succeeded", "superseded"):
                        unfinished.append(d)
                s.state = StepState.PENDING if unfinished else StepState.READY
            self.repo.insert_steps(steps)
            for s in steps:
                self.bus.emit("step.state", step_event_payload(s), s.project_id)
            for job_id in {s.job_id for s in steps}:
                self.refresh_job(job_id)
        return steps

    # ------------------------------------------------------------------------------------------------ claim
    def claim_one(self, pool: str, owner: str, *, now: datetime | None = None, lease_s: float = LEASE_SECONDS,
                  exclude_kinds: Iterable[str] = (), exclude_projects: Iterable[str] = (), polls: bool = False,
                  emit: bool = True) -> Step | None:
        """Atomically claim the best claimable step of ``pool`` (APP_SPEC §8.4). At most one worker ever gets a step.

        ``polls=False`` claims READY steps (a fresh attempt: ``attempt`` + 1); ``polls=True`` claims WAITING_REMOTE steps
        whose ``not_before`` has passed (a poll: same attempt). Returns the claimed ``Step`` (state RUNNING) or None.
        """
        now = now or utcnow()
        now_s, lease_s_ = iso_utc(now), iso_utc(now + timedelta(seconds=lease_s))
        state = "waiting_remote" if polls else "ready"
        extra, args = "", [state, pool, now_s]
        kinds = list(exclude_kinds)
        if kinds:
            extra += f" AND kind NOT IN ({','.join('?' for _ in kinds)})"
            args += kinds
        projects = list(exclude_projects)
        if projects:
            extra += f" AND (project_id IS NULL OR project_id NOT IN ({','.join('?' for _ in projects)}))"
            args += projects
        attempt_expr = "json_extract(json,'$.attempt')" if polls else "json_extract(json,'$.attempt')+1"
        remote_state = "'polling'" if polls else "json_extract(json,'$.remote_state')"
        sql = (
            "UPDATE steps SET state='running', lease_until=?, lease_owner=?, "
            "json=json_set(json,'$.state','running','$.lease_until',?,'$.lease_owner',?,"
            f"'$.started_at',COALESCE(json_extract(json,'$.started_at'),?),'$.attempt',{attempt_expr},"
            f"'$.remote_state',{remote_state}) "
            "WHERE id=(SELECT id FROM steps WHERE state=? AND pool=? AND (not_before IS NULL OR not_before<=?)"
            f"{extra} ORDER BY priority, created_at, id LIMIT 1) AND state=? RETURNING json"
        )
        params = [lease_s_, owner, lease_s_, owner, now_s, *args, state]
        with self.db.tx() as c:
            row = c.execute(sql, params).fetchone()
            if row is None:
                return None
            step = Step.model_validate_json(row["json"])
            if emit:
                self.bus.emit("step.state", step_event_payload(step), step.project_id)
        if emit:
            self.refresh_job(step.job_id)
        return step

    # ------------------------------------------------------------------------------------------------ transitions
    def transition(self, step_id: str, to: StepState, *, expect: StepState | tuple[StepState, ...] | None = None,
                   attempt: int | None = None, update: Callable[[Step], None] | None = None,
                   where: Callable[[Step], bool] | None = None, refresh: bool = True) -> Step | None:
        """Guarded state change. Returns the updated step, or None when the guard failed (someone else moved it).

        ``expect`` lists the allowed current states, ``attempt`` the attempt that must own the step, ``where`` is any
        extra predicate. ``update`` mutates the step (inside the same transaction) after the state is set."""
        expected = (expect,) if isinstance(expect, StepState) else expect

        def guard(s: Step) -> bool:
            return ((expected is None or s.state in expected) and (attempt is None or s.attempt == attempt)
                    and (where is None or where(s)))

        def apply(s: Step) -> None:
            s.state = to
            if to in TERMINAL_STEP_STATES and to != StepState.SUPERSEDED:
                s.finished_at = utcnow()
            if to != StepState.RUNNING:
                s.lease_until = None
                s.lease_owner = None
            if update is not None:
                update(s)

        with self.db.tx():
            new = self.repo.mutate_step(step_id, apply, guard=guard)
            if new is None:
                return None
            self.bus.emit("step.state", step_event_payload(new), new.project_id)
            if refresh:
                self.refresh_job(new.job_id)
        if to in TERMINAL_STEP_STATES:
            self.bus.forget_step(step_id)
        return new

    # ------------------------------------------------------------------------------------------------ promotion
    def promote_pending(self) -> int:
        """PENDING -> READY for steps whose dependencies all finished; PENDING -> CANCELLED when one failed/cancelled."""
        promoted = 0
        with self.db.tx() as c:
            rows = c.execute(
                "UPDATE steps SET state='ready', json=json_set(json,'$.state','ready') WHERE state='pending' AND NOT EXISTS ("
                " SELECT 1 FROM step_deps d JOIN steps s ON s.id=d.dep_id WHERE d.step_id=steps.id"
                " AND s.state NOT IN ('succeeded','superseded')) RETURNING json").fetchall()
            touched: set[str] = set()
            for r in rows:
                step = Step.model_validate_json(r["json"])
                self.bus.emit("step.state", step_event_payload(step), step.project_id)
                touched.add(step.job_id)
                promoted += 1
            for job_id in touched:
                self.refresh_job(job_id)
        promoted += self._cancel_orphans()
        return promoted

    def _cancel_orphans(self) -> int:
        n = 0
        while True:
            rows = self.db.conn().execute(
                "SELECT id FROM steps WHERE state='pending' AND EXISTS (SELECT 1 FROM step_deps d JOIN steps s ON s.id=d.dep_id"
                " WHERE d.step_id=steps.id AND s.state IN ('failed','cancelled'))").fetchall()
            if not rows:
                return n
            for r in rows:
                def mark(s: Step) -> None:
                    s.message = UPSTREAM_MESSAGE
                    s.error = StepError(kind="other", message=UPSTREAM_MESSAGE, retryable=False, billed="no",
                                        user_hint="A step this one needs did not finish.")

                if self.transition(r["id"], StepState.CANCELLED, expect=StepState.PENDING, update=mark):
                    n += 1

    # ------------------------------------------------------------------------------------------------ jobs
    def submit_job(self, kind: JobKind | str, project_id: str | None, params: dict[str, Any] | None = None, *,
                   spec_id: str | None = None, parent_job_id: str | None = None) -> Job:
        job = Job(id=new_id("job"), project_id=project_id, kind=JobKind(kind), state=JobState.RUNNING, spec_id=spec_id,
                  params=params or {}, created_at=utcnow(), parent_job_id=parent_job_id)
        with self.db.tx():
            self.repo.insert_job(job)
            self.bus.emit("job.state", {"job_id": job.id, "project_id": project_id, "kind": job.kind.value,
                                        "state": job.state.value}, project_id)
        return job

    def refresh_job(self, job_id: str) -> Job | None:
        """Recompute the job's state from its steps; emits ``job.state`` when it changes."""
        with self.db.tx():
            job = self.repo.find_job(job_id)
            if job is None or job.state == JobState.CANCELLED:
                return job
            states = [StepState(r["state"]) for r in self.db.conn().execute(
                "SELECT state FROM steps WHERE job_id=?", (job_id,)).fetchall()]
            new_state = derive_job_state(states)
            if new_state == JobState.RUNNING and job.project_id and self._project_paused(job.project_id):
                new_state = JobState.PAUSED
            if new_state == job.state:
                return job
            job = job.model_copy(update={"state": new_state, "finished_at": utcnow() if new_state in TERMINAL_JOB_STATES else None})
            self.repo.save_job(job)
            self.bus.emit("job.state", {"job_id": job.id, "project_id": job.project_id, "kind": job.kind.value,
                                        "state": new_state.value}, job.project_id)
            return job

    def _project_paused(self, project_id: str) -> bool:
        p = self.repo.find_project(project_id)
        return bool(p and p.paused)

    def refresh_project_jobs(self, project_id: str) -> None:
        for job in self.repo.list_jobs(project_id=project_id, limit=500):
            self.refresh_job(job.id)

    def cancel_job(self, job_id: str) -> Job:
        """Cancel every unfinished step of the job and mark the job CANCELLED (§8.3: any non-terminal -> CANCELLED)."""
        job = self.repo.get_job(job_id)
        for s in self.repo.list_steps(job_id=job_id):
            if s.state not in TERMINAL_STEP_STATES:
                self.cancel_step(s.id, refresh=False)
        with self.db.tx():
            job = self.repo.get_job(job_id)
            if job.state != JobState.CANCELLED:
                job = job.model_copy(update={"state": JobState.CANCELLED, "finished_at": utcnow()})
                self.repo.save_job(job)
                self.bus.emit("job.state", {"job_id": job.id, "project_id": job.project_id, "kind": job.kind.value,
                                            "state": "cancelled"}, job.project_id)
        return job

    def cancel_step(self, step_id: str, *, refresh: bool = True) -> Step | None:
        step = self.repo.get_step(step_id)
        if step.state in TERMINAL_STEP_STATES:
            return step
        self.rt.scheduler.signal_cancel(step_id)
        return self.transition(step_id, StepState.CANCELLED, expect=tuple(s for s in StepState if s not in TERMINAL_STEP_STATES),
                               update=lambda s: setattr(s, "message", "cancelled"), refresh=refresh)

    def retry_step(self, step_id: str) -> Step:
        """FAILED -> READY with attempts reset; dependents cancelled because of it go back to PENDING."""
        def reset(s: Step) -> None:
            s.attempt = 0
            s.error = None
            s.not_before = None
            s.budget_ok = False
            s.message = "retrying"
            s.remote_state = "none"
            s.polls = 0
            s.finished_at = None
            s.progress = 0.0
            s.result = {k: v for k, v in s.result.items() if not k.startswith("_")}

        step = self.transition(step_id, StepState.READY, expect=StepState.FAILED, update=reset)
        if step is None:
            raise ValueError("only a FAILED step can be retried")
        self._revive_dependents(step_id)
        # the job may have been FAILED/finished: refresh clears finished_at when it is running again
        self.refresh_job(step.job_id)
        return step

    def _revive_dependents(self, step_id: str) -> None:
        frontier = [step_id]
        while frontier:
            nxt: list[str] = []
            for dep in frontier:
                for r in self.db.conn().execute(
                        "SELECT s.id FROM steps s JOIN step_deps d ON d.step_id=s.id WHERE d.dep_id=? AND s.state='cancelled'",
                        (dep,)).fetchall():
                    revived = self.transition(
                        r["id"], StepState.PENDING, expect=StepState.CANCELLED, where=lambda s: s.message == UPSTREAM_MESSAGE,
                        update=lambda s: (setattr(s, "error", None), setattr(s, "message", ""), setattr(s, "finished_at", None)))
                    if revived is not None:
                        nxt.append(revived.id)
            frontier = nxt

    def supersede(self, step_ids: Iterable[str]) -> int:
        """SUCCEEDED -> SUPERSEDED: an upstream change replaced these results (kept for history)."""
        n = 0
        for sid in step_ids:
            if self.transition(sid, StepState.SUPERSEDED, expect=StepState.SUCCEEDED):
                n += 1
        return n

    def set_priority(self, project_id: str, part_id: str | None, priority: int, *, only_lower_than: int | None = None) -> int:
        """Re-prioritise the unclaimed steps of a part (``POST /api/focus``)."""
        sql = ("UPDATE steps SET priority=?, json=json_set(json,'$.priority',?) WHERE project_id=? "
               "AND state IN ('pending','ready')")
        args: list[Any] = [priority, priority, project_id]
        if part_id is not None:
            sql += " AND part_id=?"
            args.append(part_id)
        if only_lower_than is not None:
            sql += " AND priority < ?"
            args.append(only_lower_than)
        with self.db.tx() as c:
            return c.execute(sql, args).rowcount

    # ------------------------------------------------------------------------------------------------ lease
    def extend_leases(self, owner: str, *, lease_s: float = LEASE_SECONDS, now: datetime | None = None) -> int:
        now = now or utcnow()
        until = iso_utc(now + timedelta(seconds=lease_s))
        with self.db.tx() as c:
            return c.execute(
                "UPDATE steps SET lease_until=?, json=json_set(json,'$.lease_until',?) WHERE state='running' AND lease_owner=?",
                (until, until, owner)).rowcount

    def extend_lease(self, step_id: str, attempt: int, owner: str, *, lease_s: float = LEASE_SECONDS) -> bool:
        until = iso_utc(utcnow() + timedelta(seconds=lease_s))
        with self.db.tx() as c:
            return c.execute(
                "UPDATE steps SET lease_until=?, json=json_set(json,'$.lease_until',?) WHERE id=? AND state='running'"
                " AND lease_owner=? AND json_extract(json,'$.attempt')=?", (until, until, step_id, owner, attempt)).rowcount > 0

    def counts(self) -> dict[str, int]:
        return self.repo.step_counts()


def not_before_in(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


Outcome = Literal["success", "failed", "cancelled", "waiting"]
