"""Part routes (APP_SPEC §13): one part with its links, checks and provenance summary, and the Tripo pack of a hair or accessory tile."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from duoskin.api import RT
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


@router.get("/projects/{project_id}/parts/{part_id}")
def get_part(project_id: str, part_id: str, rt: Runtime = RT) -> dict[str, Any]:
    """The part plus its asset links, check results and a provenance summary."""
    part = rt.repo.get_part(project_id, part_id)
    links = rt.repo.list_links(project_id=project_id, part_id=part_id)
    shas = {lk.asset_sha for lk in links}
    checks = [{"id": cid, **r.model_dump(mode="json")} for cid, r in rt.repo.list_checks(project_id=project_id) if r.subject_sha in shas]
    summary = [{"asset_sha": lk.asset_sha, "role": lk.role, "status": lk.status, "source": lk.provenance.source, "model": lk.provenance.model,
                "prompt_id": lk.provenance.prompt_id, "cost_usd": lk.provenance.cost_usd} for lk in links]
    return {"part": part.model_dump(mode="json"), "links": [lk.model_dump(mode="json") for lk in links], "checks": checks, "provenance": summary}


@router.post("/projects/{project_id}/parts/{part_id}/tripo-pack")
def tripo_pack(project_id: str, part_id: str, body: Annotated[dict[str, Any] | None, Body()] = None, rt: Runtime = RT) -> dict[str, Any]:
    """Write the Tripo pack of a hair or accessory tile (8 files and an empty ``return`` folder) and open the MANUAL_IMPORT gate. The first pack of a
    project needs ``{"acknowledged_free_plan": true}``: the free-plan warning is shown as a confirm dialog first (ACC-10)."""
    from duoskin.models.part import PartKind
    from duoskin.pipeline import manual_mesh, mesh_import

    part = rt.repo.get_part(project_id, part_id)
    if part.kind not in (PartKind.HAIR, PartKind.ACCESSORY):
        raise HTTPException(status_code=422, detail={"error": "not_a_mesh_part", "message": "only hair and accessory tiles have a Tripo pack"})
    ack_key = f"free_plan_ack:{project_id}"
    acknowledged = bool((body or {}).get("acknowledged_free_plan")) or bool(rt.repo.kv_get(ack_key))
    if not acknowledged:
        raise HTTPException(status_code=409, detail={"error": "free_plan_warning_required", "message": "Confirm the free-plan warning before the first pack.",
                                                     "warning": mesh_import.FREE_PLAN_BANNER})
    rt.repo.kv_set(ack_key, True)
    if not any(part.board_assets.get(f"view.{v}") for v in ("front", "left", "back", "right")):
        raise HTTPException(status_code=409, detail={"error": "no_views", "message": "the tile has no views yet: wait until it is ready"})
    from duoskin.engine.errors import StepFailure

    try:
        state = manual_mesh.build_pack_for(rt, project_id, part)
    except StepFailure as exc:       # fewer than the four approved views: a plain answer, not a server error
        raise HTTPException(status_code=409, detail={"error": "no_views", "message": exc.user_hint or str(exc)}) from exc
    manual_mesh.start_manual(rt, project_id, part_id, reason="made by hand")
    return {"pack_id": state["pack_id"], "folder": state["folder"], "return_dir": state["return_dir"], "inbox": str(manual_mesh.inbox_dir(rt)),
            "files": sorted(f.name for f in Path(state["folder"]).iterdir() if f.is_file())}
