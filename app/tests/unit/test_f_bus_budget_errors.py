"""Event bus (§8.7), budget ledger (§8.8) and error policy (§8.6)."""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta

import pytest
from helpers_unit import make_project

from duoskin.engine import errors
from duoskin.engine.bus import EVENT_TYPES, THINKING_MAX_CHARS
from duoskin.models.common import iso_utc, new_id, utcnow
from duoskin.models.cost import CostEntry, Estimate
from duoskin.models.job import Step


# ------------------------------------------------------------------------------------------------- bus
def test_emit_inserts_first_and_ids_increase(rt):
    a = rt.bus.emit("toast", {"message": "one"})
    b = rt.bus.emit("toast", {"message": "two"}, project_id="prj_1")
    assert b == a + 1 and rt.bus.max_event_id() == b
    events = rt.bus.events_after(a - 1)
    assert [e.id for e in events] == [a, b] and events[1].project_id == "prj_1" and events[1].payload == {"message": "two"}
    assert rt.bus.events_after(b) == [] and [e.id for e in rt.bus.events_after(a)] == [b]
    assert [e.id for e in rt.bus.events_after(0, project_id="prj_1")] == [b]


def test_unknown_event_types_are_refused(rt):
    with pytest.raises(ValueError):
        rt.bus.emit("made.up", {})
    assert {"step.state", "step.progress", "job.state", "gate.opened", "gate.updated", "tile.updated", "part.state", "spec.updated",
            "cost.added", "budget.low", "project.stage", "llm.thinking", "inbox.file", "doctor.result", "toast", "warning.released",
            "server.restarting"} == set(EVENT_TYPES)


def test_thinking_text_is_cut_to_400_characters(rt):
    rt.bus.emit("llm.thinking", {"text": "x" * 1000})
    assert len(rt.bus.events_after(0)[0].payload["text"]) == THINKING_MAX_CHARS


def test_progress_is_throttled_to_two_per_second_per_step(rt):
    sent = [rt.bus.emit_progress("stp_1", {"progress": i / 10}) for i in range(20)]
    assert sum(1 for x in sent if x is not None) == 1                           # all within 0.5 s of each other
    assert rt.bus.emit_progress("stp_1", {"progress": 1.0}, force=True) is not None
    assert rt.bus.emit_progress("stp_2", {"progress": 0.1}) is not None          # another step has its own budget
    rt.bus.forget_step("stp_1")
    assert rt.bus.emit_progress("stp_1", {"progress": 0.2}) is not None


def test_subscribers_get_events_after_commit_only(rt):
    async def scenario():
        sub = rt.bus.subscribe()
        rt.bus.emit("toast", {"n": 1})
        first = await asyncio.wait_for(sub.queue.get(), 2)
        try:
            with rt.db.tx():
                rt.bus.emit("toast", {"n": 2})
                await asyncio.sleep(0.05)
                assert sub.queue.empty()                                         # not pushed before the commit
                raise RuntimeError("rolled back")
        except RuntimeError:
            pass
        await asyncio.sleep(0.05)
        assert sub.queue.empty() and rt.bus.max_event_id() == first.id           # the rolled-back event does not exist anywhere
        with rt.db.tx():
            rt.bus.emit("toast", {"n": 3})
        third = await asyncio.wait_for(sub.queue.get(), 2)
        sub.close()
        assert rt.bus.subscriber_count() == 0
        return first.payload["n"], third.payload["n"]

    assert asyncio.run(scenario()) == (1, 3)


def test_emitting_from_another_thread_reaches_the_loop(rt):
    import threading

    async def scenario():
        sub = rt.bus.subscribe()
        threading.Thread(target=lambda: rt.bus.emit("toast", {"from": "thread"})).start()
        e = await asyncio.wait_for(sub.queue.get(), 2)
        sub.close()
        return e.payload

    assert asyncio.run(scenario()) == {"from": "thread"}


def test_a_slow_subscriber_is_closed_with_a_none_sentinel(rt):
    from duoskin.engine import bus as bus_mod

    async def scenario():
        sub = rt.bus.subscribe()
        for i in range(bus_mod.SUBSCRIBER_QUEUE_SIZE + 5):
            sub._deliver(bus_mod.Event(id=i + 1, ts="t", type="toast", payload={}))
        assert rt.bus.subscriber_count() == 0                                    # dropped; the client reconnects with Last-Event-ID
        last = None
        while not sub.queue.empty():
            last = sub.queue.get_nowait()
        return last

    assert asyncio.run(scenario()) is None


def test_close_ends_every_stream(rt):
    async def scenario():
        sub = rt.bus.subscribe()
        rt.bus.close()
        return await asyncio.wait_for(sub.queue.get(), 2)

    assert asyncio.run(scenario()) is None


def test_prune_deletes_old_events_and_old_thinking(rt):
    old_ts = iso_utc(utcnow() - timedelta(days=31))
    stale_thinking = iso_utc(utcnow() - timedelta(hours=25))
    with rt.db.tx() as c:
        c.execute("INSERT INTO events (ts, project_id, type, payload) VALUES (?,?,?,?)", (old_ts, None, "toast", "{}"))
        c.execute("INSERT INTO events (ts, project_id, type, payload) VALUES (?,?,?,?)", (stale_thinking, None, "llm.thinking", "{}"))
        c.execute("INSERT INTO events (ts, project_id, type, payload) VALUES (?,?,?,?)", (stale_thinking, None, "toast", "{}"))
    rt.bus.emit("toast", {"fresh": True})
    assert rt.bus.prune() == 2
    assert sorted(e.type for e in rt.bus.events_after(0)) == ["toast", "toast"]


# ------------------------------------------------------------------------------------------------- budget
def entry(step=None, op="op", usd=1.0, project=None, attempt=1, provider="openai", state="committed", **kw):
    return CostEntry(ts=utcnow(), project_id=project, step_id=step, attempt=attempt, provider=provider, model="m", operation=op,
                     usd=usd, state=state, **kw)


def test_reserve_commit_release_cycle(rt):
    p = make_project(rt, budget_usd=10)
    res = rt.budget.reserve(p.id, Estimate(usd=3.0, provider="anthropic"), step_id="stp_1", attempt=1)
    assert rt.budget.reserved(p.id) == 3.0 and rt.budget.remaining(p.id) == 7.0
    rt.budget.commit(res, entry("stp_1", "messages.stream:L3", 2.5, p.id))
    assert (rt.budget.reserved(p.id), rt.budget.spent(p.id), rt.budget.remaining(p.id)) == (0.0, 2.5, 7.5)
    res2 = rt.budget.reserve(p.id, 1.0, step_id="stp_2", attempt=1)
    rt.budget.release(res2)
    assert rt.budget.reserved(p.id) == 0.0 and rt.budget.spent(p.id) == 2.5
    assert rt.repo.get_project(p.id).spent_usd == 2.5


def test_add_entry_shrinks_the_hold_and_is_idempotent(rt):
    p = make_project(rt)
    rt.budget.reserve(p.id, 2.0, step_id="s", attempt=1)
    assert rt.budget.add_entry(entry("s", "a", 0.5, p.id)) is not None
    assert (rt.budget.reserved(p.id), rt.budget.spent(p.id)) == (1.5, 0.5)       # no double counting while the step runs
    assert rt.budget.add_entry(entry("s", "a", 0.5, p.id)) is None               # the same call twice: ignored
    assert rt.budget.add_entry(entry("s", "b", 1.9, p.id)) is not None
    assert rt.budget.reserved(p.id) == 0.0 and rt.budget.spent(p.id) == pytest.approx(2.4)


def test_money_is_rounded_to_a_millionth(rt):
    rt.budget.add_entry(entry("s", "x", 0.1234567891))
    assert rt.budget.spent(None) == 0.123457


@pytest.mark.parametrize("est,spent,kwargs,reason", [
    (1.0, 0.0, {}, None), (2.0, 0.0, {}, None), (2.5, 0.0, {}, "ask"), (11.0, 0.0, {}, "cap"), (4.0, 7.0, {}, "cap"),
    (3.0, 0.0, {"budget_ok": True}, None), (11.0, 0.0, {"budget_ok": True}, None),
])
def test_check_reasons(rt, est, spent, kwargs, reason):
    p = make_project(rt, budget_usd=10, ask_above_usd=2)
    if spent:
        rt.budget.add_entry(entry("s", "x", spent, p.id))
    c = rt.budget.check(p.id, est, **kwargs)
    assert c.reason == reason and c.ok is (reason is None)
    assert c.facts()["cap_usd"] == 10 and c.facts()["estimate_usd"] == est


def test_tripo_credit_balance_is_checked_before_a_submit(rt):
    p = make_project(rt, budget_usd=50, ask_above_usd=50)
    rt.budget.add_entry(entry("t", "balance", 0.0, p.id, provider="tripo", balance_after=50.0))
    assert rt.budget.tripo_available_credits() == 50.0
    assert rt.budget.check(p.id, 0.4, provider="tripo").ok                        # 40 credits <= 50
    c = rt.budget.check(p.id, 0.8, provider="tripo")                              # 80 credits > 50
    assert not c.ok and c.reason == "credits"
    assert rt.budget.check(p.id, 0.8, provider="openai").ok                       # only Tripo checks credits


def test_projectless_steps_have_no_cap_but_keep_the_ask_threshold(rt):
    assert rt.budget.check(None, 1.0).ok and rt.budget.check(None, 1000.0).reason == "ask"


def test_settle_step_outcomes(rt):
    p = make_project(rt)
    cases = {"success_no_cost": ("success", "unknown", 0.7, "committed"), "failed_no": ("failed", "no", 0.0, "released"),
             "failed_yes": ("failed", "yes", 0.7, "committed"), "failed_unknown": ("failed", "unknown", 0.7, "orphan"),
             "cancelled": ("cancelled", "unknown", 0.7, "orphan")}
    for name, (outcome, billed, spent, state) in cases.items():
        step = Step(id=new_id("stp"), job_id="j", project_id=p.id, kind="k", created_at=utcnow(), attempt=1, paid=True)
        before = rt.budget.spent(p.id)
        rt.budget.reserve(p.id, 0.7, step_id=step.id, attempt=1)
        rt.budget.settle_step(step, outcome=outcome, billed=billed)
        row = rt.db.conn().execute("SELECT state FROM cost_ledger WHERE step_id=?", (step.id,)).fetchone()
        assert row["state"] == state, name
        assert rt.budget.spent(p.id) - before == pytest.approx(spent), name
        assert rt.budget.reserved(p.id) == 0.0
    waiting = Step(id="stp_w", job_id="j", project_id=p.id, kind="k", created_at=utcnow(), attempt=1)
    rt.budget.reserve(p.id, 0.7, step_id="stp_w", attempt=1)
    rt.budget.settle_step(waiting, outcome="waiting")
    assert rt.budget.reserved(p.id) == 0.7                                          # a remote task still holds its reservation


def test_ledger_and_totals(rt):
    p = make_project(rt)
    rt.budget.add_entry(entry("a", "x", 1.0, p.id, provider="openai"))
    rt.budget.add_entry(entry("b", "x", 2.0, p.id, provider="tripo"))
    rt.budget.add_entry(entry("c", "x", 0.5, p.id, provider="openai", state="orphan", basis="orphan"))
    rt.budget.reserve(p.id, 4.0, step_id="d", attempt=1)
    t = rt.budget.totals(p.id)
    assert t["by_provider"] == {"openai": 1.5, "tripo": 2.0} and t["spent_usd"] == 3.5 and t["reserved_usd"] == 4.0
    assert t["orphan_usd"] == 0.5 and t["today_usd"] == 3.5
    rows = rt.budget.ledger(p.id)
    assert len(rows) == 4 and {r.state for r in rows} == {"committed", "orphan", "reserved"}
    yesterday = iso_utc(utcnow() - timedelta(days=2))
    assert rt.budget.ledger(p.id, ts_to=yesterday) == []


def test_daily_cap(rt):
    assert rt.budget.daily_cap_reached() is False
    rt.update_settings({"budgets": {"daily_cap_usd": 2.0}})
    rt.budget.add_entry(entry("a", "x", 1.5))
    assert rt.budget.daily_cap_reached() is False
    rt.budget.add_entry(entry("b", "x", 0.5))
    assert rt.budget.daily_cap_reached() is True


# ------------------------------------------------------------------------------------------------- errors
class E(Exception):
    def __init__(self, kind, **kw):
        super().__init__(kw.pop("message", "boom"))
        self.kind = kind
        for k, v in kw.items():
            setattr(self, k, v)


def step(attempt=1, max_attempts=3, result=None):
    return Step(id="s", job_id="j", kind="k", created_at=utcnow(), attempt=attempt, max_attempts=max_attempts, result=result or {})


def test_backoff_schedule():
    assert errors.backoff_s(1, rng=random.Random(0)) == pytest.approx(1 + 0.25 * random.Random(0).random())   # jitter is injectable
    for n, base in ((1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (9, 32)):
        d = errors.backoff_s(n)
        assert base <= d <= base * 1.25


@pytest.mark.parametrize("kind", ["rate_limit", "overloaded", "server", "network", "timeout", "concurrency"])
def test_retryable_kinds_back_off_until_attempts_run_out(kind):
    d = errors.decide(step(1), E(kind, retryable=True))
    assert d.action == "retry" and d.delay_s >= 1
    last = errors.decide(step(3), E(kind, retryable=True))
    assert last.action == "fail" and last.error.retryable is False and "ran out of retries" in last.error.user_hint


def test_retry_after_is_honoured_and_concurrency_waits_longer():
    assert errors.decide(step(1), E("rate_limit", retryable=True, retry_after_s=45.0)).delay_s == 45.0
    assert errors.decide(step(1), E("concurrency", retryable=True)).delay_s >= 10


@pytest.mark.parametrize("kind", ["refusal", "moderation", "bad_request", "permission", "not_found", "recitation", "schema_too_complex", "other"])
def test_fail_kinds(kind):
    d = errors.decide(step(1), E(kind))
    assert d.action == "fail" and d.error.user_hint


def test_billing_pauses_the_queue_and_auth_pauses_the_provider():
    b = errors.decide(step(1), E("billing", provider="openai"), "openai")
    assert b.action == "fail" and b.pause_queue and b.pause_provider is None
    a = errors.decide(step(1), E("auth", provider="recraft"), "openai")
    assert a.action == "fail" and a.pause_provider == "recraft" and not a.pause_queue


def test_truncated_and_capability_retry_exactly_once():
    t1 = errors.decide(step(1), E("truncated"))
    assert t1.action == "retry" and t1.hints["max_tokens_scale"] == 2 and t1.delay_s == 0
    assert errors.decide(step(2, result={"_truncated_retry": True}), E("truncated")).action == "fail"
    c1 = errors.decide(step(1), E("capability", code="mask_multi"))
    assert c1.action == "retry" and c1.hints["drop_param"] == "mask_multi"
    assert errors.decide(step(2, result={"_capability_retry": True}), E("capability")).action == "fail"


def test_validation_and_submission_uncertain():
    assert errors.decide(step(1), E("validation", message="bad palette")).action == "validation_ok"
    d = errors.decide(step(1), E("submission_uncertain"))
    assert d.action == "wait_remote" and d.remote_state == "submission_uncertain"


def test_remote_failed_retries_only_when_the_adapter_allows_it():
    assert errors.decide(step(1), E("remote_failed", retryable=True)).action == "retry"
    assert errors.decide(step(1), E("remote_failed", retryable=False)).action == "fail"


def test_plain_python_exceptions_are_classified():
    assert errors.classify(TimeoutError("slow"), step()).kind == "timeout"
    assert errors.classify(ConnectionResetError("reset"), step()).kind == "network"
    assert errors.classify(ZeroDivisionError("x"), step()).kind == "other"
    d = errors.decide(step(1), ConnectionError("down"))
    assert d.error.kind == "network"


@pytest.mark.parametrize("kind,billed_in,billed_out", [("rate_limit", "unknown", "no"), ("auth", "unknown", "no"), ("server", "unknown", "no"),
                                                       ("timeout", "unknown", "unknown"), ("truncated", "unknown", "unknown"),
                                                       ("rate_limit", "yes", "yes")])
def test_requests_that_never_reach_the_provider_are_never_billed(kind, billed_in, billed_out):
    assert errors.classify(E(kind, billed=billed_in), step()).billed == billed_out


def test_step_failure_and_cancelled_helpers():
    sf = errors.StepFailure("no way", kind="bad_request", user_hint="fix it")
    e = errors.classify(sf, step())
    assert (e.kind, e.billed, e.user_hint, e.message) == ("bad_request", "no", "fix it", "no way")

    class Cancelled(Exception):                                                      # the provider layer's own class
        pass

    assert errors.is_cancelled(errors.Cancelled()) and errors.is_cancelled(Cancelled())
    assert not errors.is_cancelled(ValueError())
