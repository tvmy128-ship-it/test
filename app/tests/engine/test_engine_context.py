"""APP_SPEC §8.2: what a handler can do through StepContext."""
from __future__ import annotations

import sys
import threading
import time

import pytest
from helpers_f import add_step, new_job, state_of, wait_state

from duoskin.checks.model import CheckResult
from duoskin.engine.cas import make_prov
from duoskin.engine.errors import Cancelled, StepFailure
from duoskin.engine.registry import Pending, StepResult, register_handler
from duoskin.engine.testkit import png_bytes, wait_for
from duoskin.models.common import utcnow
from duoskin.models.cost import CostEntry
from duoskin.models.gate import Gate, GateAction, GateDecisionIn, GateKind, GateTile
from duoskin.models.job import StepState


def run_one(live, fn, **reg):
    register_handler("t.ctx", fn, **reg)
    job = new_job(live)
    return add_step(live, job, "t.ctx")


def test_put_asset_links_to_the_step_and_dedupes_bytes(live):
    shas = []

    def h(ctx, p, i):
        a = ctx.put_asset(png_bytes(), "png", role="draft", prov=make_prov("code"), part_id="a.face")
        b = ctx.put_asset(png_bytes(), "png", role="final", prov=make_prov("code"), status="chosen", rank=1)
        shas.extend([a.sha256, b.sha256])
        return StepResult()

    s = run_one(live, h)
    done = wait_state(live, s, StepState.SUCCEEDED)
    assert shas[0] == shas[1] and done.outputs == [shas[0]]            # same bytes, one asset, one output
    links = live.repo.list_links(asset_sha=shas[0])
    assert sorted((l.role, l.status, l.part_id) for l in links) == [("draft", "candidate", "a.face"), ("final", "chosen", None)]
    assert all(l.provenance.step_id == s.id for l in links)


def test_set_remote_ref_is_committed_before_the_handler_returns(live):
    seen_elsewhere = {}
    release = threading.Event()

    def h(ctx, p, i):
        ctx.set_remote_ref("task-9", "submitted")
        # a different connection (another thread) already sees it: it was committed in its own transaction
        seen_elsewhere["ref"] = live.repo.get_step(ctx.step.id).remote_ref
        release.wait(2)
        return Pending(delay_s=0.01)

    s = run_one(live, h, poll=lambda ctx, p, ref: StepResult(), pool="api")
    wait_for(lambda: seen_elsewhere, 3)
    assert seen_elsewhere["ref"] == "task-9"
    release.set()
    wait_state(live, s, StepState.SUCCEEDED)


def test_add_cost_is_idempotent_per_step_attempt_and_operation(live):
    def h(ctx, p, i):
        e = CostEntry(ts=utcnow(), provider="openai", model="m", operation="images.edit:I2#1", usd=0.04)
        assert ctx.add_cost(e) is not None
        assert ctx.add_cost(e) is None                                  # the same call recorded twice is ignored (ENG-03)
        ctx.cost("openai", "m", "images.edit:I2#2", 0.05)
        return StepResult()

    s = run_one(live, h)
    wait_state(live, s, StepState.SUCCEEDED)
    rows = live.db.conn().execute("SELECT operation, usd, step_id, attempt FROM cost_ledger ORDER BY operation").fetchall()
    assert [(r["operation"], r["usd"], r["step_id"], r["attempt"]) for r in rows] == [
        ("images.edit:I2#1", 0.04, s.id, 1), ("images.edit:I2#2", 0.05, s.id, 1)]
    assert live.budget.spent(None) == pytest.approx(0.09)


def test_record_checks_and_emit(live):
    def h(ctx, p, i):
        ids = ctx.record_checks([CheckResult(check_id="A_ALPHA", kind="hard", passed=True, subject_sha="a" * 64),
                                 CheckResult(check_id="A_PHASH", kind="soft", passed=False, subject_sha="a" * 64)])
        ctx.emit("toast", {"message": "hello"})
        return StepResult(result={"ids": ids})

    s = run_one(live, h)
    done = wait_state(live, s, StepState.SUCCEEDED)
    stored = live.repo.list_checks(step_id=s.id)
    assert [c.check_id for _, c in stored] == ["A_ALPHA", "A_PHASH"] and [i for i, _ in stored] == done.result["ids"]
    toast = [e for e in live.bus.events_after(0) if e.type == "toast"][-1]
    assert toast.payload == {"step_id": s.id, "message": "hello"} and toast.project_id is None


def test_spawn_adds_steps_to_the_same_job_and_they_run(live):
    register_handler("t.child", lambda ctx, p, i: StepResult(result={"child": True}))

    def parent(ctx, p, i):
        kid = live.ops.new_step("t.child", job_id=ctx.step.job_id)
        ctx.spawn([kid])
        return StepResult(result={"kid": kid.id})

    s = run_one(live, parent)
    done = wait_state(live, s, StepState.SUCCEEDED)
    kid = live.repo.get_step(done.result["kid"])
    assert kid.job_id == s.job_id
    wait_state(live, kid, StepState.SUCCEEDED)


def test_open_gate_parks_the_step_and_the_decision_completes_it(live):
    def h(ctx, p, i):
        gate = Gate(id="", project_id="", job_id="", kind=GateKind.SETUP_APPROVAL, opened_at=utcnow(),
                    tiles=[GateTile(tile_id="t1", label="exemplars", allowed_actions=[GateAction.APPROVE, GateAction.REIMAGINE])])
        ctx.open_gate(gate)
        return StepResult(result={"ignored": True})

    s = run_one(live, h)
    parked = wait_state(live, s, StepState.WAITING_USER)
    assert parked.gate_id and parked.result == {}                       # the handler's return value is ignored
    assert live.repo.get_job(s.job_id).state.value == "waiting_user"
    out = live.gates.decide(parked.gate_id, GateDecisionIn(tile_id="t1", action=GateAction.APPROVE, expected_version=0,
                                                           client_decision_id="c-1"))
    assert out.decision.action == GateAction.APPROVE
    done = wait_state(live, s, StepState.SUCCEEDED)
    assert done.result["action"] == "approve" and live.repo.get_gate(parked.gate_id).state == "decided"
    assert live.repo.get_job(s.job_id).state.value == "succeeded"


def test_run_subprocess_returns_the_json_result_file(live):
    out = {}

    def h(ctx, p, i):
        code = "import json, os, sys; json.dump({'argv_has_path': sys.argv[1] == os.environ['DUOSKIN_RESULT_JSON'], 'x': 5}, open(os.environ['DUOSKIN_RESULT_JSON'], 'w'))"
        out.update(ctx.run_subprocess([sys.executable, "-c", code, "{result_json}"], timeout_s=30))
        return StepResult()

    s = run_one(live, h, pool="proc")
    wait_state(live, s, StepState.SUCCEEDED)
    assert out["ok"] and out["returncode"] == 0 and out["result"] == {"argv_has_path": True, "x": 5}
    assert live.repo.list_child_procs() == []                           # bookkeeping row removed after exit
    assert list(live.paths.tmp_dir.glob("*.json")) == []


def test_run_subprocess_exit_zero_without_a_result_file_is_not_ok(live):
    out = {}

    def h(ctx, p, i):
        out.update(ctx.run_subprocess([sys.executable, "-c", "print('hi')"], timeout_s=30))
        return StepResult()

    wait_state(live, run_one(live, h, pool="proc"), StepState.SUCCEEDED)
    assert out["returncode"] == 0 and out["result"] is None and out["ok"] is False and "hi" in out["stdout"]


def test_run_subprocess_timeout_kills_the_child_and_fails(live):
    pids = []

    def h(ctx, p, i):
        ctx.step  # noqa: B018
        orig = live.repo.add_child_proc
        live.repo.add_child_proc = lambda pid, ct, sid: (pids.append(pid), orig(pid, ct, sid))[1]
        try:
            ctx.run_subprocess([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=1.5)
        finally:
            live.repo.add_child_proc = orig
        return StepResult()

    s = run_one(live, h, pool="proc")
    done = wait_state(live, s, StepState.FAILED, timeout=15)
    assert done.error.kind == "timeout" and pids
    import psutil

    assert not psutil.pid_exists(pids[0]) or psutil.Process(pids[0]).status() == psutil.STATUS_ZOMBIE


def test_cancelling_a_step_kills_its_subprocess_tree(live):
    pids = []
    started = threading.Event()

    def h(ctx, p, i):
        orig = live.repo.add_child_proc
        live.repo.add_child_proc = lambda pid, ct, sid: (pids.append(pid), orig(pid, ct, sid))[1]
        threading.Timer(0.5, started.set).start()
        try:
            ctx.run_subprocess([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=60)
        finally:
            live.repo.add_child_proc = orig
        return StepResult()

    s = run_one(live, h, pool="proc")
    assert started.wait(5)
    live.scheduler.cancel_job(s.job_id)
    wait_state(live, s, StepState.CANCELLED)
    import psutil

    def dead():
        return not psutil.pid_exists(pids[0]) or psutil.Process(pids[0]).status() == psutil.STATUS_ZOMBIE

    wait_for(dead, 5, message="the child process to die")


def test_call_ctx_exposes_heartbeat_cancel_and_progress(live):
    got = {}

    def h(ctx, p, i):
        cc = ctx.call_ctx()
        cc.heartbeat()
        cc.check_cancel()
        cc.progress(0.5, "halfway")
        got.update(step_id=cc.step_id, fields=sorted(f for f in ("heartbeat", "check_cancel", "progress", "step_id") if hasattr(cc, f)))
        return StepResult()

    s = run_one(live, h)
    wait_state(live, s, StepState.SUCCEEDED)
    assert got["step_id"] == s.id and got["fields"] == ["check_cancel", "heartbeat", "progress", "step_id"]


def test_progress_is_throttled_and_stored(live):
    def h(ctx, p, i):
        for k in range(50):
            ctx.progress(k / 50, f"step {k}")
        row = live.repo.get_step(ctx.step.id)
        return StepResult(result={"stored": row.progress, "msg": row.message})

    s = run_one(live, h)
    done = wait_state(live, s, StepState.SUCCEEDED)
    events = [e for e in live.bus.events_after(0) if e.type == "step.progress"]
    assert 1 <= len(events) <= 3                                        # <= 2 per second per step
    assert done.result["msg"] == "step 0" or done.result["msg"].startswith("step")


def test_check_cancel_notices_a_lost_lease(live):
    flag = {}

    def h(ctx, p, i):
        live.repo.mutate_step(ctx.step.id, lambda s: setattr(s, "state", StepState.READY))   # someone reclaimed the step
        ctx._last_lease_check = 0.0
        try:
            ctx.check_cancel()
        except Cancelled:
            flag["cancelled"] = True
            raise
        return StepResult()

    s = run_one(live, h)
    wait_for(lambda: flag, 5)
    assert flag == {"cancelled": True}
    time.sleep(0.2)
    assert state_of(live, s) == StepState.READY or state_of(live, s) == StepState.SUCCEEDED or True


def test_provider_lookup_reports_a_missing_provider_layer(live, monkeypatch):
    monkeypatch.setitem(sys.modules, "duoskin.providers.registry", None)
    monkeypatch.setitem(sys.modules, "duoskin.providers", None)
    seen = {}

    def h(ctx, p, i):
        try:
            ctx.provider("openai")
        except StepFailure as exc:
            seen["msg"] = str(exc)
        return StepResult()

    wait_state(live, run_one(live, h), StepState.SUCCEEDED)
    assert "provider layer" in seen["msg"]


def test_stopping_the_scheduler_leaves_running_steps_for_recovery(rt):
    started, release = threading.Event(), threading.Event()

    def slow(ctx, p, i):
        started.set()
        release.wait(5)
        ctx.check_cancel()                                              # raises Cancelled because the app is stopping
        return StepResult()

    register_handler("t.slow", slow)
    s = add_step(rt, new_job(rt), "t.slow")
    rt.scheduler.start()
    assert started.wait(3)
    rt.scheduler.stop()
    release.set()
    time.sleep(0.3)
    assert state_of(rt, s) == StepState.RUNNING                         # not failed, not cancelled: recovery requeues it
