"""Part routes (APP_SPEC §13). ``GET`` is a plain read; the Tripo pack route is a STUB for the 3D track."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from duoskin.api import get_rt, not_implemented
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


@router.get("/projects/{project_id}/parts/{part_id}")
def get_part(project_id: str, part_id: str, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """The part plus its asset links, check results and a provenance summary."""
    part = rt.repo.get_part(project_id, part_id)
    links = rt.repo.list_links(project_id=project_id, part_id=part_id)
    checks = [{"id": cid, **r.model_dump(mode="json")} for cid, r in rt.repo.list_checks(project_id=project_id)
              if r.subject_sha in {l.asset_sha for l in links}]
    summary = [{"asset_sha": l.asset_sha, "role": l.role, "status": l.status, "source": l.provenance.source,
                "model": l.provenance.model, "prompt_id": l.provenance.prompt_id, "cost_usd": l.provenance.cost_usd}
               for l in links]
    return {"part": part.model_dump(mode="json"), "links": [l.model_dump(mode="json") for l in links], "checks": checks,
            "provenance": summary}


@router.post("/projects/{project_id}/parts/{part_id}/tripo-pack")   # STUB
def tripo_pack(project_id: str, part_id: str) -> JSONResponse:
    return JSONResponse(not_implemented("The Tripo pack export", "3D"), status_code=501)
