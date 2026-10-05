"""The plan of a project (APP_SPEC §13): what the Plan page shows while the planner works and after.

``GET /api/projects/{id}/plan`` returns the PLAN job's steps, the plans of the newest plan set (with their pair structure, wildcard flag,
lint results and status), the notice Gate 1 shows when fewer than three plans could be built, and the id of the open Gate 1.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from duoskin.api import RT
from duoskin.engine.runtime import Runtime
from duoskin.models.gate import GateKind
from duoskin.pipeline import brief as BR
from duoskin.pipeline import plan as PL

router = APIRouter(prefix="/api")


def plan_set_of(rt: Runtime, project_id: str) -> str | None:
    """The newest plan set of a project (the one the last PLAN job made)."""
    recs = rt.repo.list_specs(project_id)
    return recs[-1].plan_set_id if recs else None


@router.get("/projects/{project_id}/plan")
def get_plan(project_id: str, rt: Runtime = RT) -> dict[str, Any]:
    project = rt.repo.get_project(project_id)
    job = rt.repo.find_job(project.plan_job_id) if project.plan_job_id else None
    steps = [{"id": s.id, "kind": s.kind, "state": s.state.value, "message": s.message, "progress": s.progress, "paid": s.paid}
             for s in (rt.repo.list_steps(job_id=job.id) if job else [])]
    psid = plan_set_of(rt, project_id)
    specs: list[dict[str, Any]] = []
    env: dict[str, Any] = {}
    if psid:
        env = PL.get_envelope(rt, psid)
        for rec in rt.repo.list_specs(project_id):
            if rec.plan_set_id != psid:
                continue
            specs.append({"id": rec.id, "plan_index": rec.plan_index, "version": rec.version, "status": rec.status, "rank": rec.rank,
                          "created_by": rec.created_by, "is_wildcard": bool(rec.spec.get("is_wildcard")),
                          "pair_structure": rec.spec.get("world", {}).get("pair_structure"), "theme": rec.spec.get("world", {}).get("theme"),
                          "critic_levels": rec.critic_levels, "pairwise_wins": rec.pairwise_wins,
                          "lint": [{"id": r.check_id, "kind": r.kind, "passed": r.passed, "metric": r.metric, "evidence": r.evidence}
                                   for r in rec.lint if not r.passed]})
    gate = next((g for g in rt.repo.list_gates(project_id, "open") if g.kind == GateKind.CONCEPT), None)
    return {"project_id": project_id, "stage": project.stage.value, "job": job.model_dump(mode="json") if job else None, "steps": steps,
            "plan_set_id": psid, "specs": specs, "notice": env.get("notice", ""), "dropped": env.get("dropped", []),
            "brief_read_as": BR.brief_read_as([r.spec for r in rt.repo.list_specs(project_id) if r.plan_set_id == psid and r.status in ("shown", "approved")]),
            "gate_id": gate.id if gate else None}
