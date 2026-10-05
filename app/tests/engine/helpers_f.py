"""Shared helpers of the engine tests (imported by basename; this folder has no __init__.py)."""
from __future__ import annotations

from duoskin.engine.registry import StepResult
from duoskin.engine.testkit import png_bytes, wait_for
from duoskin.models.job import JobKind, StepState

TERMINAL = {StepState.SUCCEEDED, StepState.FAILED, StepState.CANCELLED, StepState.SUPERSEDED}


def new_job(rt, project_id=None, kind=JobKind.PARTS):
    return rt.scheduler.submit_job(kind, project_id, {})


def add_step(rt, job, kind, **kw):
    step = rt.ops.new_step(kind, job_id=job.id, project_id=job.project_id, **kw)
    rt.scheduler.spawn(job.id, [step])
    return step


def state_of(rt, step):
    return rt.repo.get_step(step.id).state


def wait_state(rt, step, *states, timeout=5.0):
    want = set(states)
    def reached():
        return rt.repo.get_step(step.id) if rt.repo.get_step(step.id).state in want else None

    try:
        return wait_for(reached, timeout, message=f"step {step.kind} to reach {[s.value for s in want]}")
    except AssertionError as exc:
        now = rt.repo.get_step(step.id)
        raise AssertionError(f"{exc}; it is {now.state.value}, attempt {now.attempt}, error={now.error}, message={now.message!r}") from None


def make_project(rt, name="Test Duo", **settings):
    from duoskin.models.common import new_id, utcnow
    from duoskin.models.project import Project, ProjectSettings, Stage

    now = utcnow()
    p = Project(id=new_id("prj"), name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo="bg",
                brief="", stage=Stage.BRIEF, settings=ProjectSettings(**settings))
    return rt.repo.create_project(p)


def ok_handler(ctx, p, inputs):
    return StepResult(result={"ok": True})


def png_handler(ctx, p, inputs):
    from duoskin.engine.cas import make_prov

    a = ctx.put_asset(png_bytes(8, 8, (1, 2, 3, 255)), "png", role="draft", prov=make_prov("code"))
    return StepResult(result={"sha": a.sha256})
