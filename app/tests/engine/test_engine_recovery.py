"""APP_SPEC §8.4, CHK-X01, ENG-06: crash recovery. Lease expiry re-queues a step; a committed remote_ref prevents a second
paid submit; a paid call without a remote id becomes an orphan cost row and a new attempt."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import psutil
from helpers_f import add_step, new_job, ok_handler, state_of, wait_state

from duoskin import config
from duoskin.engine.recovery import recover
from duoskin.engine.registry import Pending, StepResult, register_handler
from duoskin.engine.runtime import Runtime
from duoskin.models.common import utcnow
from duoskin.models.cost import Estimate
from duoskin.models.gate import Gate, GateKind, GateTile
from duoskin.models.job import Step, StepState


def _make_running(rt, step: Step, *, owner="dead-instance", expired=True, remote_ref=None, attempt=1):
    """Put a READY step into the state a crash leaves behind."""
    def apply(s: Step) -> None:
        s.state = StepState.RUNNING
        s.lease_owner = owner
        s.lease_until = utcnow() - timedelta(seconds=30) if expired else utcnow() + timedelta(seconds=100)
        s.attempt = attempt
        s.remote_ref = remote_ref
        s.remote_state = "submitted" if remote_ref else "none"

    return rt.repo.mutate_step(step.id, apply)


def test_expired_lease_requeues_an_unpaid_step_and_it_runs_again(rt):
    register_handler("t.ok", ok_handler)
    s = add_step(rt, new_job(rt), "t.ok")
    _make_running(rt, s)
    report = recover(rt, expired_only=True)
    assert report.requeued == [s.id] and state_of(rt, s) == StepState.READY
    rt.scheduler.start()
    done = wait_state(rt, s, StepState.SUCCEEDED)
    assert done.attempt == 2                                  # the crashed attempt 1 was not counted as success


def test_expired_only_leaves_live_leases_alone_but_startup_recovery_takes_everything(rt):
    register_handler("t.ok", ok_handler)
    job = new_job(rt)
    live_lease = add_step(rt, job, "t.ok")
    _make_running(rt, live_lease, expired=False)
    assert recover(rt, expired_only=True).touched == 0
    assert state_of(rt, live_lease) == StepState.RUNNING
    assert recover(rt).requeued == [live_lease.id]            # startup: single instance, so every RUNNING step is dead


def test_remote_ref_prevents_a_second_submit(rt):
    submits, polled = [], []

    def run(ctx, p, i):
        submits.append(1)
        ctx.set_remote_ref("r-1")
        return Pending()

    def poll(ctx, p, ref):
        polled.append(ref)
        return StepResult(result={"model": "glb"})

    register_handler("t.remote", run, poll=poll, pool="api", paid=True, provider="tripo", estimate=lambda p: 1.1)
    s = add_step(rt, new_job(rt), "t.remote")
    _make_running(rt, s, remote_ref="r-1")                    # crashed after set_remote_ref committed
    report = recover(rt)
    assert report.to_waiting_remote == [s.id] and state_of(rt, s) == StepState.WAITING_REMOTE
    rt.scheduler.start()
    done = wait_state(rt, s, StepState.SUCCEEDED)
    assert submits == [] and polled == ["r-1"] and done.result == {"model": "glb"}


def test_paid_step_without_remote_ref_is_requeued_and_its_reservation_becomes_an_orphan(rt):
    calls = []
    register_handler("t.paid", lambda ctx, p, i: calls.append(ctx.attempt) or StepResult(), pool="api", paid=True,
                     provider="openai", estimate=lambda p: 0.5)
    s = add_step(rt, new_job(rt), "t.paid")
    _make_running(rt, s)
    rt.budget.reserve(None, Estimate(usd=0.5, provider="openai"), step_id=s.id, attempt=1)
    assert rt.budget.reserved(None) == 0.5
    report = recover(rt)
    assert report.requeued == [s.id] and report.orphaned_costs == 1
    assert rt.budget.reserved(None) == 0.0 and rt.budget.spent(None) == 0.5     # the possibly-billed call is counted
    row = rt.db.conn().execute("SELECT state, json_extract(json,'$.basis') AS basis FROM cost_ledger").fetchone()
    assert (row["state"], row["basis"]) == ("orphan", "orphan")
    rt.scheduler.start()
    wait_state(rt, s, StepState.SUCCEEDED)
    assert calls == [2]                                       # a new attempt with its own reservation
    rows = rt.db.conn().execute("SELECT attempt, state FROM cost_ledger ORDER BY attempt, state").fetchall()
    assert ("1", "orphan") in {(str(r["attempt"]), r["state"]) for r in rows}


def test_step_interrupted_max_attempts_times_fails_instead_of_crash_looping(rt):
    register_handler("t.ok", ok_handler)
    s = add_step(rt, new_job(rt), "t.ok", max_attempts=3)
    _make_running(rt, s, attempt=3)
    report = recover(rt)
    assert report.failed == [s.id]
    done = rt.repo.get_step(s.id)
    assert done.state == StepState.FAILED and done.error.code == "interrupted"


def test_late_result_from_a_replaced_worker_is_fenced_off(rt):
    register_handler("t.ok", ok_handler)
    s = add_step(rt, new_job(rt), "t.ok")
    first = rt.ops.claim_one("cpu", rt.instance_id)           # attempt 1, held by a worker that will turn out to be stuck
    assert first.attempt == 1
    recover(rt, now=utcnow() + timedelta(seconds=500), expired_only=True)   # its lease ran out
    second = rt.ops.claim_one("cpu", rt.instance_id)
    assert second.attempt == 2
    stale = rt.ops.transition(s.id, StepState.SUCCEEDED, expect=StepState.RUNNING, attempt=1)
    assert stale is None and state_of(rt, s) == StepState.RUNNING   # the old worker cannot overwrite attempt 2
    assert rt.ops.transition(s.id, StepState.SUCCEEDED, expect=StepState.RUNNING, attempt=2) is not None


def test_orphan_child_processes_are_killed_at_startup(rt):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ps = psutil.Process(child.pid)
        rt.repo.add_child_proc(child.pid, ps.create_time(), None)
        report = recover(rt)
        assert child.pid in report.killed_children
        child.wait(timeout=5)
        assert child.returncode is not None and rt.repo.list_child_procs() == []
    finally:
        if child.poll() is None:
            child.kill()


def test_a_reused_pid_with_another_create_time_is_not_killed(rt):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        rt.repo.add_child_proc(child.pid, psutil.Process(child.pid).create_time() - 1000, None)
        assert recover(rt).killed_children == []
        assert child.poll() is None
    finally:
        child.kill()


def test_open_gates_are_announced_again(rt):
    gate = Gate(id="", project_id="", job_id="j", kind=GateKind.SETUP_APPROVAL, opened_at=utcnow(),
                tiles=[GateTile(tile_id="t", label="x", allowed_actions=[])])
    opened = rt.gates.open_gate(gate)
    before = len([e for e in rt.bus.events_after(0) if e.type == "gate.opened"])
    report = recover(rt)
    after = [e for e in rt.bus.events_after(0) if e.type == "gate.opened"]
    assert report.announced_gates == 1 and len(after) == before + 1 and after[-1].payload["gate_id"] == opened.id


def test_real_process_crash_resumes_without_a_second_paid_submit(tmp_path):
    """The CHK-X01 e2e: kill a running app mid-step, restart, nothing is submitted twice."""
    home = tmp_path / "home"
    worker = Path(__file__).with_name("crash_worker_f.py")
    env = {**os.environ, "PYTHONPATH": str(config.APP_ROOT), "PYTHONUTF8": "1"}
    proc = subprocess.run([sys.executable, str(worker), str(home)], check=False, capture_output=True, encoding="utf-8", timeout=60,
                          cwd=str(config.APP_ROOT), env=env)
    assert proc.returncode == 17, proc.stderr[-2000:]
    ids = json.loads(proc.stdout.strip().splitlines()[-1])
    calls = (home / "calls.txt").read_text(encoding="utf-8").split()
    assert sorted(calls) == ["paid-call", "remote-submit"]

    from duoskin.engine import registry

    registry.clear()
    parent_calls = []
    polled = []
    register_handler("t.remote", lambda ctx, p, i: parent_calls.append("remote-submit") or Pending(), pool="api", paid=True,
                     provider="tripo", estimate=lambda p: 1.1,
                     poll=lambda ctx, p, ref: polled.append(ref) or StepResult(result={"ok": True}))
    register_handler("t.paid", lambda ctx, p, i: parent_calls.append(f"paid-attempt-{ctx.attempt}") or StepResult(), pool="api",
                     paid=True, provider="openai", estimate=lambda p: 0.5)
    rt = Runtime.create(home, providers_mode="mock")
    try:
        before = rt.repo.get_step(ids["remote"])
        assert before.state == StepState.RUNNING and before.remote_ref == "remote-xyz"      # committed before the crash
        rt.startup(start_threads=True, backup=False)
        wait_state(rt, rt.repo.get_step(ids["remote"]), StepState.SUCCEEDED, timeout=15)
        wait_state(rt, rt.repo.get_step(ids["paid"]), StepState.SUCCEEDED, timeout=15)
        assert "remote-submit" not in parent_calls and polled == ["remote-xyz"]            # no second paid submit
        assert parent_calls == ["paid-attempt-2"]                                          # unknown paid call: a new attempt
        orphan = rt.db.conn().execute("SELECT COUNT(*) FROM cost_ledger WHERE state='orphan'").fetchone()[0]
        assert orphan == 1 and rt.budget.spent(None) >= 0.5
    finally:
        rt.shutdown()
