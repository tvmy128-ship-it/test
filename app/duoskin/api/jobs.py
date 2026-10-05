"""Jobs, steps and the queue (APP_SPEC §13): list jobs, retry a FAILED step, cancel a job, pause/resume the queue."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from duoskin.api import RT
from duoskin.db.errors import NotFound
from duoskin.engine.runtime import Runtime
from duoskin.models.job import JobView, Step, StepSummary

router = APIRouter(prefix="/api")


def summarize(s: Step) -> StepSummary:
    return StepSummary(id=s.id, job_id=s.job_id, project_id=s.project_id, part_id=s.part_id, kind=s.kind, state=s.state,
                       pool=s.pool, progress=s.progress, message=s.message, attempt=s.attempt, paid=s.paid, cached=s.cached,
                       error=s.error, remote_state=s.remote_state, priority=s.priority, created_at=s.created_at,
                       finished_at=s.finished_at)


def _job_view(rt: Runtime, job_id: str, with_steps: bool) -> JobView:
    job = rt.repo.get_job(job_id)
    steps = [summarize(s) for s in rt.repo.list_steps(job_id=job_id)] if with_steps else []
    return JobView(job=job, steps_by_state=rt.repo.step_counts(job_id=job_id), steps=steps)


@router.get("/jobs")
def list_jobs(project_id: str | None = None, state: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100,
              steps: bool = False, rt: Runtime = RT) -> list[JobView]:
    return [_job_view(rt, j.id, steps) for j in rt.repo.list_jobs(project_id, state, limit)]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, rt: Runtime = RT) -> JobView:
    return _job_view(rt, job_id, True)


@router.post("/steps/{step_id}/retry")
def retry_step(step_id: str, rt: Runtime = RT) -> Step:
    step = rt.repo.find_step(step_id)
    if step is None:
        raise NotFound("step", step_id)
    try:
        return rt.scheduler.retry_step(step_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": "not_failed", "message": str(exc)}) from exc


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, rt: Runtime = RT):
    rt.repo.get_job(job_id)
    return rt.scheduler.cancel_job(job_id)


@router.get("/queue")
def queue_status(rt: Runtime = RT) -> dict[str, Any]:
    return {"paused": rt.scheduler.queue_paused, "paused_providers": dict(rt.scheduler.paused_providers),
            "scheduler_running": rt.scheduler.running, "paid_blocked": rt.paid_blocked_reason(), "steps": rt.repo.step_counts()}


@router.post("/queue/resume")
def resume_queue(rt: Runtime = RT) -> dict[str, Any]:
    """Lift a billing pause (after the user added credits) and every provider pause."""
    rt.scheduler.resume_queue()
    for provider in list(rt.scheduler.paused_providers):
        rt.scheduler.resume_provider(provider)
    return queue_status(rt)
