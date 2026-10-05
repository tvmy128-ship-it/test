"""APP_SPEC §8.8: estimates, reservations, the BUDGET gate, provider limits and paid-step blocking, through the scheduler."""
from __future__ import annotations

import threading
import time

import pytest
from helpers_f import add_step, make_project, new_job, ok_handler, state_of, wait_state

from duoskin.engine.registry import StepResult, register_handler
from duoskin.engine.testkit import wait_for
from duoskin.models.gate import GateAction, GateDecisionIn, GateKind
from duoskin.models.job import StepState


class Billed(Exception):
    def __init__(self, kind="server", billed="no", retryable=False):
        super().__init__("x")
        self.kind, self.billed, self.retryable = kind, billed, retryable


def decide(rt, gate_id, tile_id, action, *, choice=None, version=0, cid=None):
    return rt.gates.decide(gate_id, GateDecisionIn(tile_id=tile_id, action=action, choice=choice, expected_version=version,
                                                    client_decision_id=cid or f"c-{time.monotonic_ns()}"))


def open_budget_gate(rt, project):
    return wait_for(lambda: next(iter(rt.repo.list_gates(project.id, "open")), None), 5, message="a BUDGET gate")


def paid(ctx, p, i):
    ctx.cost("openai", "m", "images.edit:t", 0.5)
    return StepResult(result={"ran": True})


def test_cheap_paid_step_runs_without_asking_and_commits_its_cost(live):
    project = make_project(live, budget_usd=15, ask_above_usd=2)
    register_handler("t.paid", paid, paid=True, provider="openai", estimate=lambda p: 0.6, pool="api")
    s = add_step(live, new_job(live, project.id), "t.paid")
    wait_state(live, s, StepState.SUCCEEDED)
    assert live.budget.spent(project.id) == 0.5 and live.budget.reserved(project.id) == 0.0   # reservation fully released
    assert live.repo.get_project(project.id).spent_usd == 0.5
    assert live.repo.list_gates(project.id) == []


def test_paid_step_that_records_no_cost_is_committed_at_its_estimate(live):
    project = make_project(live)
    register_handler("t.paid", ok_handler, paid=True, provider="openai", estimate=lambda p: 0.75, pool="api")
    wait_state(live, add_step(live, new_job(live, project.id), "t.paid"), StepState.SUCCEEDED)
    assert live.budget.spent(project.id) == 0.75 and live.budget.reserved(project.id) == 0.0


def test_estimate_above_the_ask_threshold_opens_a_budget_gate_and_continue_runs_it(live):
    project = make_project(live, budget_usd=15, ask_above_usd=2)
    runs = []
    register_handler("t.big", lambda ctx, p, i: runs.append(1) or StepResult(), paid=True, provider="anthropic",
                     estimate=lambda p: 3.0, pool="api")
    s = add_step(live, new_job(live, project.id), "t.big")
    gate = open_budget_gate(live, project)
    parked = wait_state(live, s, StepState.WAITING_USER)
    assert gate.kind == GateKind.BUDGET and parked.gate_id == gate.id and parked.cost_estimate_usd == 3.0
    tile = gate.tiles[0]
    assert tile.tile_id == s.id and tile.facts["reason"] == "ask" and tile.facts["estimate_usd"] == 3.0
    assert {a for a in tile.allowed_actions} == {GateAction.CONTINUE, GateAction.RAISE_CAP, GateAction.STOP}
    assert runs == []                                              # nothing was spent before the user answered
    decide(live, gate.id, s.id, GateAction.CONTINUE)
    wait_state(live, s, StepState.SUCCEEDED)
    assert runs == [1] and live.repo.get_gate(gate.id).state == "decided"
    assert live.repo.get_step(s.id).budget_ok is True


def test_estimate_above_the_remaining_cap_opens_a_gate_and_raise_cap_lets_it_run(live):
    project = make_project(live, budget_usd=1.0, ask_above_usd=5)
    register_handler("t.big", ok_handler, paid=True, provider="anthropic", estimate=lambda p: 1.5, pool="api")
    s = add_step(live, new_job(live, project.id), "t.big")
    gate = open_budget_gate(live, project)
    assert gate.tiles[0].facts["reason"] == "cap"
    with pytest.raises(Exception, match="new cap"):
        decide(live, gate.id, s.id, GateAction.RAISE_CAP)           # needs the number
    decide(live, gate.id, s.id, GateAction.RAISE_CAP, choice="4.5", version=0)
    wait_state(live, s, StepState.SUCCEEDED)
    assert live.repo.get_project(project.id).settings.budget_usd == 4.5


def test_stop_fails_the_step_with_reason_budget(live):
    project = make_project(live, budget_usd=1.0)
    register_handler("t.big", ok_handler, paid=True, provider="anthropic", estimate=lambda p: 1.5, pool="api")
    job = new_job(live, project.id)
    s = add_step(live, job, "t.big")
    gate = open_budget_gate(live, project)
    decide(live, gate.id, s.id, GateAction.STOP)
    done = wait_state(live, s, StepState.FAILED)
    assert done.error.code == "budget" and done.message == "budget"
    assert live.repo.get_job(job.id).state.value == "failed"


def test_a_gate_opens_before_the_step_that_would_cross_the_cap_not_after(live):
    project = make_project(live, budget_usd=1.0, ask_above_usd=5)
    register_handler("t.half", paid, paid=True, provider="openai", estimate=lambda p: 0.5, pool="api")   # actual 0.5 each
    job = new_job(live, project.id)
    steps = [add_step(live, job, "t.half", params={"n": n}) for n in range(3)]
    # the steps run concurrently, so which one is asked is not fixed; exactly one must be (check + reserve is atomic)
    wait_for(lambda: sorted(state_of(live, s).value for s in steps) == ["succeeded", "succeeded", "waiting_user"], 8,
             message="two steps to finish and one to ask")
    assert live.budget.spent(project.id) == 1.0


@pytest.mark.parametrize("billed,final_state,spent", [("no", "released", 0.0), ("yes", "committed", 0.4), ("unknown", "orphan", 0.4)])
def test_failed_paid_step_settles_its_reservation_by_the_billed_flag(live, billed, final_state, spent):
    project = make_project(live)

    def boom(ctx, p, i):
        raise Billed("bad_request" if billed != "unknown" else "timeout", billed=billed)

    register_handler("t.boom", boom, paid=True, provider="openai", estimate=lambda p: 0.4, pool="api")
    wait_state(live, add_step(live, new_job(live, project.id), "t.boom", max_attempts=1), StepState.FAILED)
    row = live.db.conn().execute("SELECT state FROM cost_ledger").fetchone()
    # "no" is released for a rejected request; "unknown" (a timeout) counts as spent because the call may have been billed
    assert row["state"] == final_state
    assert live.budget.spent(project.id) == pytest.approx(spent)
    assert live.budget.reserved(project.id) == 0.0


def test_retry_after_a_billed_failure_does_not_double_count(live, fast_backoff):
    project = make_project(live)
    n = []

    def flaky(ctx, p, i):
        n.append(1)
        if len(n) == 1:
            ctx.cost("openai", "m", "images.edit:a", 0.1)
            raise Billed("server", billed="yes", retryable=True)
        ctx.cost("openai", "m", "images.edit:a", 0.1)             # same operation name, but a different attempt
        return StepResult()

    register_handler("t.flaky", flaky, paid=True, provider="openai", estimate=lambda p: 0.1, pool="api")
    wait_state(live, add_step(live, new_job(live, project.id), "t.flaky"), StepState.SUCCEEDED)
    assert live.budget.spent(project.id) == pytest.approx(0.2)   # two real calls, two ledger rows (attempt 1 and 2)


def test_provider_concurrency_limit_is_applied_before_claiming(live):
    live.update_settings({"providers": {"openai_concurrency": 1}})
    running, peak, release = [0], [0], threading.Event()
    lock = threading.Lock()

    def slow(ctx, p, i):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(3)
        with lock:
            running[0] -= 1
        return StepResult()

    register_handler("t.openai", slow, provider="openai", pool="api")
    register_handler("t.free", ok_handler, pool="api")
    job = new_job(live)
    slow_steps = [add_step(live, job, "t.openai", params={"n": n}) for n in range(3)]
    free = add_step(live, job, "t.free")
    wait_state(live, free, StepState.SUCCEEDED)                     # other work is not blocked by the saturated provider
    wait_for(lambda: running[0] == 1, 3)
    time.sleep(0.3)
    assert peak[0] == 1
    release.set()
    for s in slow_steps:
        wait_state(live, s, StepState.SUCCEEDED)
    assert peak[0] == 1


def test_doctor_failure_blocks_real_paid_steps_but_not_mock_or_free_ones(live):
    live.update_settings({"providers": {"modes": {"anthropic": "real", "openai": "mock"}}})
    live.provider_override = {}
    live._effective = None
    live.set_doctor_report({"blocks_paid_features": True, "checks": [{"id": "CHK-S04", "blocking": True}]})
    assert "CHK-S04" in live.paid_blocked_reason()
    register_handler("t.real", ok_handler, paid=True, provider="anthropic", estimate=lambda p: 0.1)
    register_handler("t.mock", ok_handler, paid=True, provider="openai", estimate=lambda p: 0.1)
    register_handler("t.free", ok_handler)
    job = new_job(live)
    real, mock, free = (add_step(live, job, k) for k in ("t.real", "t.mock", "t.free"))
    wait_state(live, mock, StepState.SUCCEEDED)
    wait_state(live, free, StepState.SUCCEEDED)
    time.sleep(0.3)
    assert state_of(live, real) == StepState.READY                  # real + paid + a failed doctor = held back
    live.set_doctor_report({"blocks_paid_features": False, "checks": []})
    wait_state(live, real, StepState.SUCCEEDED)


def test_a_fresh_install_doctor_report_never_blocks(rt):
    from duoskin.api.doctor_checks import run_doctor

    report = run_doctor(rt, quick=True)
    assert report["blocks_paid_features"] is False and report["broken_install"] is False
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["CHK-S15"]["status"] == "warn" and by_id["CHK-S14"]["status"] == "warn" and "CHK-S11b" not in by_id
    assert report["exit_code"] == 0 and not any(c["blocking"] for c in report["checks"])
    rt.set_doctor_report(report)
    assert rt.paid_blocked_reason() is None


def test_daily_cap_blocks_real_paid_steps(live):
    live.update_settings({"budgets": {"daily_cap_usd": 0.5}, "providers": {"modes": {"anthropic": "real"}}})
    live.provider_override = {}
    live._effective = None
    register_handler("t.real", paid, paid=True, provider="anthropic", estimate=lambda p: 0.1, pool="api")
    job = new_job(live)
    first = add_step(live, job, "t.real", params={"n": 1})
    wait_state(live, first, StepState.SUCCEEDED)                    # spends 0.5 = the daily cap
    second = add_step(live, job, "t.real", params={"n": 2})
    time.sleep(0.4)
    assert state_of(live, second) == StepState.READY and "daily" in live.paid_blocked_reason()


def test_budget_low_event_fires_once_per_threshold(live):
    project = make_project(live, budget_usd=10, ask_above_usd=10)

    def spend(ctx, p, i):
        ctx.cost("openai", "m", f"op{p['n']}", 8.5)
        return StepResult()

    register_handler("t.spend", spend, paid=True, provider="openai", estimate=lambda p: 8.5, pool="api")
    wait_state(live, add_step(live, new_job(live, project.id), "t.spend", params={"n": 1}), StepState.SUCCEEDED)
    lows = [e for e in live.bus.events_after(0) if e.type == "budget.low"]
    assert len(lows) == 1 and lows[0].payload["threshold"] == 0.2 and lows[0].payload["remaining_usd"] == 1.5
