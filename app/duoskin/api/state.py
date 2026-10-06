"""``GET /api/state``: the snapshot of "snapshot + tail" (APP_SPEC §8.7). ``max_event_id`` is read first, so opening the
stream with ``after=max_event_id`` can only replay a few events twice, never miss one."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from duoskin import __version__
from duoskin.api import RT
from duoskin.engine.runtime import Runtime
from duoskin.models.job import TERMINAL_JOB_STATES, JobView
from duoskin.models.project import ProjectSummary

router = APIRouter(prefix="/api")


def project_summaries(rt: Runtime, include_archived: bool = False) -> list[ProjectSummary]:
    open_gates: dict[str, int] = {}
    for g in rt.repo.list_gates(state="open"):
        open_gates[g.project_id] = open_gates.get(g.project_id, 0) + 1
    return [ProjectSummary(id=p.id, name=p.name, slug=p.slug, combo=p.combo, stage=p.stage, paused=p.paused,
                           archived=p.archived, spent_usd=p.spent_usd, created_at=p.created_at, updated_at=p.updated_at,
                           waiting_on_user=open_gates.get(p.id, 0))
            for p in rt.repo.list_projects(include_archived=include_archived)]


def job_view(rt: Runtime, job_id: str, *, with_steps: bool = False) -> JobView:
    from duoskin.api.jobs import summarize

    job = rt.repo.get_job(job_id)
    return JobView(job=job, steps_by_state=rt.repo.step_counts(job_id=job_id),
                   steps=[summarize(s) for s in rt.repo.list_steps(job_id=job_id)] if with_steps else [])


@router.get("/state")
def state(rt: Runtime = RT) -> dict[str, Any]:
    max_event_id = rt.bus.max_event_id()
    jobs = [job_view(rt, j.id) for j in rt.repo.list_jobs(limit=60)]
    active = [j for j in jobs if j.job.state not in TERMINAL_JOB_STATES]
    recent = [j for j in jobs if j.job.state in TERMINAL_JOB_STATES][:20]
    # "picked": the final duo is chosen and only the export is left (the pages must not ask for the pick again)
    gates = [{"id": g.id, "project_id": g.project_id, "job_id": g.job_id, "kind": g.kind.value, "tiles": len(g.tiles),
              "picked": g.kind.value == "final_pick" and any(t.state.value == "approved" for t in g.tiles),
              "step_id": g.step_id, "opened_at": g.opened_at} for g in rt.repo.list_gates(state="open")]
    doctor = rt.doctor_report
    return {
        "version": __version__, "instance_id": rt.instance_id, "demo": rt.demo, "max_event_id": max_event_id,
        "projects": [p.model_dump(mode="json") for p in project_summaries(rt)],
        "jobs": [j.model_dump(mode="json") for j in [*active, *recent]],
        "steps": rt.repo.step_counts(),
        "open_gates": [{**g, "opened_at": g["opened_at"].isoformat()} for g in gates],
        "queue": {"paused": rt.scheduler.queue_paused, "paused_providers": dict(rt.scheduler.paused_providers),
                  "scheduler_running": rt.scheduler.running, "paid_blocked": rt.paid_blocked_reason()},
        "doctor": ({"ts": doctor.get("ts"), "summary": doctor.get("summary"), "exit_code": doctor.get("exit_code"),
                    "blocks_paid_features": doctor.get("blocks_paid_features")} if doctor else None),
        "today_usd": rt.budget.spent_today(),
    }
