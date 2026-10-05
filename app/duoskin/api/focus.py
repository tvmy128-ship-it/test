"""``POST /api/focus``: the UI is looking at a tile, so its steps run first (priority 10, APP_SPEC §8.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from duoskin.api import get_rt
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


class FocusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    part_id: str | None = None


@router.post("/focus", status_code=204)
def focus(body: FocusIn, rt: Runtime = Depends(get_rt)) -> Response:
    rt.scheduler.focus(body.project_id, body.part_id)
    return Response(status_code=204)
