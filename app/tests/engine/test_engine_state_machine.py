"""APP_SPEC §8.3: every step transition, job state derivation, cache, retries and errors."""
from __future__ import annotations

import threading
import time

import pytest
from helpers_f import add_step, make_project, new_job, ok_handler, png_handler, state_of, wait_state

from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import Pending, StepResult, register_handler
from duoskin.engine.steps import derive_job_state
from duoskin.models.job import JobState, StepState


class FakeProviderError(Exception):
    """Same attributes as providers.base.ProviderError (the engine reads them by name)."""

    def __init__(self, kind, message="boom", *, retryable=False, billed="unknown", retry_after_s=None, code=None, provider=None):
        super().__init__(message)
        self.kind, self.retryable, self.billed, self.retry_after_s, self.code, self.provider = kind, retryable, billed, retry_after_s, code, provider
        self.user_hint = ""
        self.request_id = None


# ------------------------------------------------------------------------------------------------ happy path
def test_pending_to_ready_to_succeeded_with_deps(live):
    gate = threading.Event()
    register_handler("t.ok", ok_handler)
    register_handler("t.held", lambda ctx, p, i: gate.wait(5) and StepResult())
    job = new_job(live)
    a = add_step(live, job, "t.held")
    b = live.ops.new_step("t.ok", job_id=job.id, params={"n": 2}, deps=[a.id])
    live.scheduler.spawn(job.id, [b])
    assert state_of(live, b) == StepState.PENDING             # its dependency is still running
    gate.set()
    wait_state(live, a, StepState.SUCCEEDED)
    wait_state(live, b, StepState.SUCCEEDED)
    assert live.repo.get_job(job.id).state == JobState.SUCCEEDED
    assert live.repo.get_job(job.id).finished_at is not None


def test_step_without_deps_starts_ready(rt):
    register_handler("t.ok", ok_handler)
    job = new_job(rt)
    s = add_step(rt, job, "t.ok")
    assert state_of(rt, s) == StepState.READY


def test_outputs_are_collected_from_put_asset_and_linked(live):
    register_handler("t.png", png_handler)
    job = new_job(live)
    s = add_step(live, job, "t.png")
    done = wait_state(live, s, StepState.SUCCEEDED)
    assert len(done.outputs) == 1 and done.result["sha"] == done.outputs[0]
    links = live.repo.list_links(asset_sha=done.outputs[0])
    assert links[0].step_id == s.id and links[0].provenance.step_kind == "t.png" and links[0].provenance.handler_version == 1


def test_cache_hit_succeeds_without_running_and_costs_nothing(live):
    calls = []

    def handler(ctx, p, inputs):
        calls.append(1)
        return png_handler(ctx, p, inputs)

    register_handler("t.png", handler, paid=False)
    job = new_job(live)
    first = add_step(live, job, "t.png", params={"k": 1})
    wait_state(live, first, StepState.SUCCEEDED)
    second = add_step(live, job, "t.png", params={"k": 1})
    done = wait_state(live, second, StepState.SUCCEEDED)
    assert done.cached is True and len(calls) == 1 and done.outputs == live.repo.get_step(first.id).outputs
    third = add_step(live, job, "t.png", params={"k": 1}, nonce="reimagine-1")   # a new nonce = Reimagine: no cache hit
    wait_state(live, third, StepState.SUCCEEDED)
    assert len(calls) == 2


def test_cache_miss_when_output_left_the_cas(live):
    register_handler("t.png", png_handler)
    job = new_job(live)
    a = wait_state(live, add_step(live, job, "t.png"), StepState.SUCCEEDED)
    live.cas.path(a.outputs[0]).unlink()
    b = wait_state(live, add_step(live, job, "t.png"), StepState.SUCCEEDED)
    assert b.cached is False


def test_non_cacheable_handler_always_runs(live):
    calls = []
    register_handler("t.nc", lambda ctx, p, i: calls.append(1) or StepResult(), cacheable=False)
    job = new_job(live)
    wait_state(live, add_step(live, job, "t.nc"), StepState.SUCCEEDED)
    wait_state(live, add_step(live, job, "t.nc"), StepState.SUCCEEDED)
    assert len(calls) == 2


# ------------------------------------------------------------------------------------------------ remote
def test_remote_step_waits_then_polls_to_success_and_never_resubmits(live):
    submits = []

    def run(ctx, p, inputs):
        submits.append(1)
        ctx.set_remote_ref("remote-42")
        return Pending(delay_s=0.05, message="submitted")

    polls = []

    def poll(ctx, p, remote_ref):
        polls.append(remote_ref)
        return Pending(delay_s=0.05) if len(polls) < 3 else StepResult(result={"done": True})

    register_handler("t.remote", run, poll=poll, pool="api")
    job = new_job(live)
    s = add_step(live, job, "t.remote")
    waiting = wait_state(live, s, StepState.WAITING_REMOTE)
    assert waiting.remote_ref == "remote-42"
    done = wait_state(live, s, StepState.SUCCEEDED)
    assert submits == [1] and polls == ["remote-42"] * 3 and done.result == {"done": True}
    assert done.attempt == 1 and done.polls >= 2


def test_pending_without_remote_ref_is_a_bug_and_fails(live):
    register_handler("t.bad", lambda ctx, p, i: Pending())
    job = new_job(live)
    s = wait_state(live, add_step(live, job, "t.bad"), StepState.FAILED)
    assert s.error.kind == "bad_request"


def test_poll_delay_schedule():
    from duoskin.engine.scheduler import poll_delay_s

    assert poll_delay_s(0) == 5.0
    assert poll_delay_s(1) == 3.0 and poll_delay_s(2) == pytest.approx(4.2) and poll_delay_s(20) == 15.0


# ------------------------------------------------------------------------------------------------ errors and retries
def test_retryable_error_goes_back_to_ready_with_backoff_then_succeeds(live, fast_backoff):
    attempts = []

    def flaky(ctx, p, i):
        attempts.append(ctx.attempt)
        if len(attempts) < 3:
            raise FakeProviderError("rate_limit", retryable=True, billed="no")
        return StepResult(result={"n": len(attempts)})

    register_handler("t.flaky", flaky)
    job = new_job(live)
    s = add_step(live, job, "t.flaky")
    done = wait_state(live, s, StepState.SUCCEEDED)
    assert attempts == [1, 2, 3] and done.attempt == 3 and done.error is None


def test_retries_run_out_then_fail(live, fast_backoff):
    register_handler("t.down", lambda ctx, p, i: (_ for _ in ()).throw(FakeProviderError("server", retryable=True, billed="no")))
    job = new_job(live)
    s = add_step(live, job, "t.down", max_attempts=2)
    done = wait_state(live, s, StepState.FAILED)
    assert done.attempt == 2 and done.error.kind == "server" and "retries" in done.error.user_hint
    assert live.repo.get_job(job.id).state == JobState.FAILED


@pytest.mark.parametrize("kind", ["refusal", "moderation", "bad_request", "permission", "not_found", "recitation",
                                  "schema_too_complex"])
def test_non_retryable_kinds_fail_at_once(live, kind):
    register_handler("t.fail", lambda ctx, p, i: (_ for _ in ()).throw(FakeProviderError(kind, billed="no")))
    job = new_job(live)
    done = wait_state(live, add_step(live, job, "t.fail"), StepState.FAILED)
    assert done.attempt == 1 and done.error.kind == kind and done.error.user_hint


def test_billing_error_fails_and_pauses_the_whole_queue(live):
    register_handler("t.bill", lambda ctx, p, i: (_ for _ in ()).throw(FakeProviderError("billing", billed="no", provider="openai")), provider="openai")
    register_handler("t.after", ok_handler)
    job = new_job(live)
    done = wait_state(live, add_step(live, job, "t.bill"), StepState.FAILED)
    assert done.error.kind == "billing" and live.scheduler.queue_paused
    later = add_step(live, job, "t.after")
    time.sleep(0.4)
    assert state_of(live, later) == StepState.READY            # nothing is claimed while paused
    live.scheduler.resume_queue()
    wait_state(live, later, StepState.SUCCEEDED)


def test_auth_error_pauses_only_that_provider(live):
    register_handler("t.auth", lambda ctx, p, i: (_ for _ in ()).throw(FakeProviderError("auth", billed="no", provider="recraft")), provider="recraft")
    register_handler("t.recraft2", ok_handler, provider="recraft")
    register_handler("t.other", ok_handler, provider="openai")
    job = new_job(live)
    wait_state(live, add_step(live, job, "t.auth"), StepState.FAILED)
    assert "recraft" in live.scheduler.paused_providers
    blocked = add_step(live, job, "t.recraft2")
    free = add_step(live, job, "t.other")
    wait_state(live, free, StepState.SUCCEEDED)
    time.sleep(0.3)
    assert state_of(live, blocked) == StepState.READY
    live.scheduler.resume_provider("recraft")
    wait_state(live, blocked, StepState.SUCCEEDED)


def test_truncated_retries_once_with_doubled_token_hint_then_fails(live, fast_backoff):
    seen = []

    def handler(ctx, p, i):
        seen.append(dict(ctx.hints))
        raise FakeProviderError("truncated", billed="yes")

    register_handler("t.trunc", handler)
    job = new_job(live)
    done = wait_state(live, add_step(live, job, "t.trunc"), StepState.FAILED)
    assert seen == [{}, {"max_tokens_scale": 2}] and done.attempt == 2


def test_capability_error_retries_once_without_the_parameter(live, fast_backoff):
    seen = []

    def handler(ctx, p, i):
        seen.append(dict(ctx.hints))
        if len(seen) == 1:
            raise FakeProviderError("capability", code="mask_multi", billed="no")
        return StepResult()

    register_handler("t.cap", handler)
    job = new_job(live)
    wait_state(live, add_step(live, job, "t.cap"), StepState.SUCCEEDED)
    assert seen[1] == {"drop_param": "mask_multi"}


def test_validation_error_succeeds_with_validation_errors_for_the_plan_loop(live):
    register_handler("t.val", lambda ctx, p, i: (_ for _ in ()).throw(FakeProviderError("validation", "palette ids not unique", billed="yes")))
    job = new_job(live)
    done = wait_state(live, add_step(live, job, "t.val"), StepState.SUCCEEDED)
    assert done.result == {"validation_errors": ["palette ids not unique"]}
    # a validation result is never cached (it would repeat the invalid answer)
    again = wait_state(live, add_step(live, job, "t.val"), StepState.SUCCEEDED)
    assert again.cached is False


def test_submission_uncertain_waits_remote_and_polls_to_reconcile(live):
    polled = []

    def run(ctx, p, i):
        raise FakeProviderError("submission_uncertain", "body sent, answer lost", billed="unknown")

    def poll(ctx, p, remote_ref):
        polled.append((remote_ref, ctx.step.remote_state))
        return StepResult(result={"reconciled": True})

    register_handler("t.unc", run, poll=poll, pool="api", paid=False)
    job = new_job(live)
    s = add_step(live, job, "t.unc")
    wait_state(live, s, StepState.SUCCEEDED, timeout=10)
    assert polled and polled[0][1] == "submission_uncertain" and polled[0][0] == ""


def test_unknown_step_kind_fails_with_a_clear_message(rt):
    from duoskin.models.common import new_id, utcnow
    from duoskin.models.job import Step

    job = new_job(rt)
    s = Step(id=new_id("stp"), job_id=job.id, kind="no.such.kind", pool="cpu", created_at=utcnow())
    rt.scheduler.spawn(job.id, [s])
    rt.scheduler.start()
    done = wait_state(rt, s, StepState.FAILED)
    assert done.error.kind == "bad_request" and "no.such.kind" in done.error.message


def test_bad_params_fail_validation(live):
    from pydantic import BaseModel

    class P(BaseModel):
        n: int

    register_handler("t.params", ok_handler, Params=P)
    job = new_job(live)
    s = add_step(live, job, "t.params", params={"n": "not a number"})
    assert wait_state(live, s, StepState.FAILED).error.kind == "bad_request"
    good = add_step(live, job, "t.params", params={"n": 3})
    wait_state(live, good, StepState.SUCCEEDED)


def test_unexpected_exception_fails_without_retry_and_is_logged(live, caplog):
    register_handler("t.bug", lambda ctx, p, i: 1 / 0)
    job = new_job(live)
    done = wait_state(live, add_step(live, job, "t.bug"), StepState.FAILED)
    assert done.error.kind == "other" and done.attempt == 1 and "division" in done.error.message


def test_step_failure_is_honoured(live, fast_backoff):
    n = []

    def handler(ctx, p, i):
        n.append(1)
        raise StepFailure("try again", kind="network", retryable=True, billed="no") if len(n) == 1 else StepFailure("no", kind="bad_request")

    register_handler("t.sf", handler)
    job = new_job(live)
    done = wait_state(live, add_step(live, job, "t.sf"), StepState.FAILED)
    assert len(n) == 2 and done.error.kind == "bad_request"


# ------------------------------------------------------------------------------------------------ cancel, failures, retry
def test_failed_dependency_cancels_dependents_and_retry_revives_them(live):
    state = {"fail": True}

    def maybe(ctx, p, i):
        if state["fail"]:
            raise StepFailure("nope", kind="bad_request")
        return StepResult(result={"fine": True})

    register_handler("t.maybe", maybe)
    register_handler("t.ok", ok_handler)
    job = new_job(live)
    a = add_step(live, job, "t.maybe")
    b = live.ops.new_step("t.ok", job_id=job.id, deps=[a.id])
    c = live.ops.new_step("t.ok", job_id=job.id, deps=[b.id])
    live.scheduler.spawn(job.id, [b, c])
    wait_state(live, a, StepState.FAILED)
    cb = wait_state(live, b, StepState.CANCELLED)
    wait_state(live, c, StepState.CANCELLED)
    assert "upstream" in cb.message
    assert live.repo.get_job(job.id).state == JobState.FAILED
    state["fail"] = False
    live.scheduler.retry_step(a.id)
    wait_state(live, a, StepState.SUCCEEDED)
    wait_state(live, b, StepState.SUCCEEDED)
    wait_state(live, c, StepState.SUCCEEDED)
    assert live.repo.get_step(a.id).attempt == 1          # attempts were reset by the retry
    assert live.repo.get_job(job.id).state == JobState.SUCCEEDED


def test_retry_refuses_a_step_that_did_not_fail(rt):
    register_handler("t.ok", ok_handler)
    s = add_step(rt, new_job(rt), "t.ok")
    with pytest.raises(ValueError):
        rt.scheduler.retry_step(s.id)


def test_cancel_a_running_step_sets_flag_and_discards_result(live):
    started, release = threading.Event(), threading.Event()
    seen_cancel = []

    def slow(ctx, p, i):
        started.set()
        release.wait(5)
        seen_cancel.append(ctx.cancelled)
        ctx.check_cancel()
        return StepResult(result={"late": True})

    register_handler("t.slow", slow)
    job = new_job(live)
    s = add_step(live, job, "t.slow")
    assert started.wait(3)
    live.scheduler.cancel_job(job.id)
    release.set()
    done = wait_state(live, s, StepState.CANCELLED)
    time.sleep(0.2)
    assert seen_cancel == [True] and live.repo.get_step(s.id).state == StepState.CANCELLED
    assert live.repo.get_job(job.id).state == JobState.CANCELLED
    assert done.result == {}                                  # the late result never landed


def test_cancel_pending_and_ready_steps(rt):
    register_handler("t.ok", ok_handler)
    job = new_job(rt)
    a = add_step(rt, job, "t.ok")
    b = rt.ops.new_step("t.ok", job_id=job.id, deps=[a.id])
    rt.scheduler.spawn(job.id, [b])
    rt.scheduler.cancel_job(job.id)
    assert state_of(rt, a) == StepState.CANCELLED and state_of(rt, b) == StepState.CANCELLED


def test_supersede_keeps_history(live):
    register_handler("t.ok", ok_handler)
    job = new_job(live)
    s = add_step(live, job, "t.ok")
    wait_state(live, s, StepState.SUCCEEDED)
    assert live.ops.supersede([s.id]) == 1
    assert state_of(live, s) == StepState.SUPERSEDED
    assert live.repo.get_job(job.id).state == JobState.SUCCEEDED   # all succeeded or superseded


# ------------------------------------------------------------------------------------------------ job state derivation
@pytest.mark.parametrize("states,expected", [
    ([StepState.RUNNING, StepState.WAITING_USER], JobState.RUNNING),
    ([StepState.READY], JobState.RUNNING),
    ([StepState.WAITING_REMOTE, StepState.FAILED], JobState.RUNNING),
    ([StepState.WAITING_USER, StepState.SUCCEEDED], JobState.WAITING_USER),
    ([StepState.WAITING_USER, StepState.PENDING], JobState.WAITING_USER),
    ([StepState.SUCCEEDED, StepState.SUPERSEDED], JobState.SUCCEEDED),
    ([StepState.SUCCEEDED, StepState.FAILED, StepState.CANCELLED], JobState.FAILED),
    ([StepState.CANCELLED, StepState.CANCELLED], JobState.CANCELLED),
    ([], JobState.RUNNING),
])
def test_derive_job_state(states, expected):
    assert derive_job_state(states) == expected


def test_paused_project_shows_paused_and_claims_nothing(rt):
    register_handler("t.ok", ok_handler)
    project = make_project(rt)
    job = new_job(rt, project.id)
    s = add_step(rt, job, "t.ok")
    rt.scheduler.pause(project.id)
    assert rt.repo.get_job(job.id).state == JobState.PAUSED
    rt.scheduler.start()
    time.sleep(0.5)
    assert state_of(rt, s) == StepState.READY
    rt.scheduler.resume(project.id)
    wait_state(rt, s, StepState.SUCCEEDED)
    assert rt.repo.get_job(job.id).state == JobState.SUCCEEDED


def test_focus_raises_priority_of_that_part_only(rt):
    register_handler("t.ok", ok_handler)
    project = make_project(rt)
    job = new_job(rt, project.id)
    a = add_step(rt, job, "t.ok", part_id="a.face")
    b = add_step(rt, job, "t.ok", part_id="b.hair")
    rt.scheduler.focus(project.id, "b.hair")
    assert rt.repo.get_step(b.id).priority == 10 and rt.repo.get_step(a.id).priority == 100
    rt.scheduler.focus(project.id, "a.face")
    assert rt.repo.get_step(b.id).priority == 100 and rt.repo.get_step(a.id).priority == 10


def test_claim_order_is_priority_then_age(rt):
    register_handler("t.ok", ok_handler)
    job = new_job(rt)
    low = add_step(rt, job, "t.ok", priority=1000)
    mid1 = add_step(rt, job, "t.ok")
    mid2 = add_step(rt, job, "t.ok")
    high = add_step(rt, job, "t.ok", priority=10)
    order = [rt.ops.claim_one("cpu", "me").id for _ in range(4)]
    assert order == [high.id, mid1.id, mid2.id, low.id]
    assert rt.ops.claim_one("cpu", "me") is None


def test_events_describe_the_whole_life_of_a_step(live):
    register_handler("t.ok", ok_handler)
    job = new_job(live)
    s = add_step(live, job, "t.ok")
    wait_state(live, s, StepState.SUCCEEDED)
    time.sleep(0.1)
    events = [e for e in live.bus.events_after(0) if e.payload.get("step_id") == s.id]
    assert [e.payload["state"] for e in events] == ["ready", "running", "succeeded"]
    job_events = [e.payload["state"] for e in live.bus.events_after(0) if e.type == "job.state"]
    assert job_events[0] == "running" and job_events[-1] == "succeeded"
