"""Crash recovery (APP_SPEC §8.4, ENG-06, CHK-X01). The app is single-instance, so leases are reclaimed at startup.

* RUNNING with a ``remote_ref``  -> WAITING_REMOTE (resume polling; **never resubmit**: the remote task exists).
* RUNNING without one and paid   -> READY; the attempt's ``reserved`` ledger row becomes an ``orphan`` row (the call
  may have been billed); the retry is a new attempt with its own reservation.
* RUNNING and unpaid             -> READY.
* A step that was interrupted ``max_attempts`` times fails instead of looping forever (it may be what crashes the app).
* ``child_procs`` rows (pid + create time) are killed if the process is still alive, then cleared.
* Open gates are announced again (``gate.opened``) so the UI shows them.

``recover(rt)`` is the startup pass and the ``reset-leases`` CLI. ``recover(rt, expired_only=True)`` also reclaims only
steps whose lease already ran out (a periodic safety net when a heartbeat died); a late result from the replaced worker
is discarded because every completion is fenced by the attempt number.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from duoskin.engine.context import kill_process_tree
from duoskin.models.common import iso_utc, utcnow
from duoskin.models.job import Step, StepError, StepState

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.recovery")


@dataclass
class RecoveryReport:
    to_waiting_remote: list[str] = field(default_factory=list)
    requeued: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    orphaned_costs: int = 0
    killed_children: list[int] = field(default_factory=list)
    announced_gates: int = 0

    @property
    def touched(self) -> int:
        return len(self.to_waiting_remote) + len(self.requeued) + len(self.failed)


def recover(rt: Runtime, *, now: datetime | None = None, expired_only: bool = False) -> RecoveryReport:
    now = now or utcnow()
    report = RecoveryReport()
    sql = "SELECT id FROM steps WHERE state='running'"
    args: list[str] = []
    if expired_only:
        sql += " AND (lease_until IS NULL OR lease_until < ?)"
        args.append(iso_utc(now))
    ids = [r["id"] for r in rt.db.conn().execute(sql, args).fetchall()]
    for step_id in ids:
        step = rt.repo.find_step(step_id)
        if step is None or step.state != StepState.RUNNING:
            continue
        _recover_step(rt, step, report, now)
    if not expired_only:
        for row in rt.repo.list_child_procs():
            if kill_process_tree(int(row["pid"]), float(row["create_time"])):
                report.killed_children.append(int(row["pid"]))
            rt.repo.remove_child_proc(int(row["pid"]), float(row["create_time"]))
        report.announced_gates = rt.gates.reannounce_open_gates()
    if report.touched or report.killed_children:
        log.warning("recovery: %d to waiting_remote, %d requeued, %d failed, %d orphan cost rows, %d children killed",
                    len(report.to_waiting_remote), len(report.requeued), len(report.failed), report.orphaned_costs,
                    len(report.killed_children))
    rt.scheduler.notify()
    return report


def _recover_step(rt: Runtime, step: Step, report: RecoveryReport, now: datetime) -> None:
    ops = rt.ops
    if step.remote_ref:
        def to_remote(s: Step) -> None:
            s.not_before = now
            s.message = "resuming after a restart"
            if s.remote_state in ("none", "polling"):
                s.remote_state = "submitted"

        if ops.transition(step.id, StepState.WAITING_REMOTE, expect=StepState.RUNNING, attempt=step.attempt, update=to_remote):
            report.to_waiting_remote.append(step.id)
        return
    if step.paid:
        report.orphaned_costs += rt.budget.mark_orphan(step.id, step.attempt)
    if step.attempt >= step.max_attempts:
        def give_up(s: Step) -> None:
            s.error = StepError(kind="other", code="interrupted", retryable=False, billed="unknown",
                                message=f"interrupted {s.attempt} times by restarts or crashes",
                                user_hint="This step was interrupted too many times. Retry it by hand.")
            s.message = "interrupted too many times"

        if ops.transition(step.id, StepState.FAILED, expect=StepState.RUNNING, attempt=step.attempt, update=give_up):
            report.failed.append(step.id)
        return

    def requeue(s: Step) -> None:
        s.not_before = None
        s.message = "restarted after an interruption"
        s.child_pid = None
        s.child_create_time = None

    if ops.transition(step.id, StepState.READY, expect=StepState.RUNNING, attempt=step.attempt, update=requeue):
        report.requeued.append(step.id)
