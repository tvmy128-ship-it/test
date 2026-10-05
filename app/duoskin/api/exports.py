"""Export routes (APP_SPEC §13, §10.13): start the EXPORT job, read the kit manifest, checklist and banners, tick checklist steps."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from duoskin.api import RT
from duoskin.db.errors import ConflictError
from duoskin.engine.runtime import Runtime
from duoskin.models.job import Job
from duoskin.models.project import Stage

router = APIRouter(prefix="/api")


class TickIn(BaseModel):
    item_id: str
    step_id: str
    ticked: bool
    expected_version: int = Field(ge=0)


@router.post("/projects/{project_id}/export")
def start_export(project_id: str, rt: Runtime = RT) -> Job:
    """Start the EXPORT job. Needs the Gate 3 pick: the same rule as the Gate 3 action."""
    from duoskin.pipeline import duo, export

    project = rt.repo.get_project(project_id)
    if project.stage not in (Stage.GATE3, Stage.EXPORTED, Stage.EXPORTING):
        raise ConflictError(f"the project is at stage '{project.stage.value}'", project, code="wrong_stage")
    if not duo.get_state(rt, project_id).get("picked_at") and not any(
            duo.picked(rt, g.id) for g in rt.repo.list_gates(project_id) if g.kind.value == "final_pick"):
        raise HTTPException(status_code=409, detail={"error": "pick_first", "message": "pick the duo at Gate 3 first"})
    return export.start_export(rt, project_id)


def _checklist(rt: Runtime, project_id: str) -> dict[str, Any] | None:
    from duoskin.pipeline import export

    st = export.get_state(rt, project_id)
    return rt.repo.kv_get(f"checklist:{project_id}") or st.get("checklist")


@router.get("/exports/{project_id}")
def get_export(project_id: str, rt: Runtime = RT) -> dict[str, Any]:
    """The kit manifest, the checklist and the banners (``status``: none, running, blocked, failed, done)."""
    from duoskin.pipeline import export

    rt.repo.get_project(project_id)
    st = export.get_state(rt, project_id)
    out: dict[str, Any] = {"status": st.get("status", "none"), "reason": st.get("reason"), "kit_dir": st.get("kit_dir"), "zip": st.get("zip"),
                           "banners": st.get("banners", []), "mock": bool(st.get("mock")), "checks": st.get("checks", [])}
    if st.get("kit_dir"):
        try:
            mf = next(Path(st["kit_dir"]).glob("*manifest.json"))
            out["manifest"] = json.loads(mf.read_text(encoding="utf-8"))
        except (StopIteration, OSError, ValueError):
            out["manifest"] = None
    out["checklist"] = _checklist(rt, project_id)
    out["version"] = int(rt.repo.kv_get(f"checklist_version:{project_id}") or 0)
    return out


@router.patch("/exports/{project_id}/checklist")
def patch_checklist(project_id: str, body: TickIn, rt: Runtime = RT) -> dict[str, Any]:
    """Tick or untick one step. A locked step (an upload line before its Studio test and confirmation) is refused; un-ticking a prerequisite un-ticks
    what depends on it. Optimistic lock: ``expected_version``."""
    from duoskin.roblox import checklist as CL

    data = _checklist(rt, project_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"error": "no_checklist", "message": "there is no export kit yet"})
    version = int(rt.repo.kv_get(f"checklist_version:{project_id}") or 0)
    if body.expected_version != version:
        raise ConflictError("the checklist changed since you loaded it", {"checklist": data, "version": version}, code="version_conflict")
    try:
        cl = CL.tick(CL.from_json(data), body.item_id, body.step_id, body.ticked)
    except CL.ChecklistError as exc:
        raise HTTPException(status_code=422, detail={"error": "locked", "message": str(exc)}) from exc
    new = CL.to_json(cl)
    rt.repo.kv_set(f"checklist:{project_id}", new)
    rt.repo.kv_set(f"checklist_version:{project_id}", version + 1)
    return {"checklist": new, "version": version + 1}
