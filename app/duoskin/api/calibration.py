"""Calibration routes (APP_SPEC §3.8, §12, §13): the blind drill screen and the judge calibration set.

``GET /api/calibration/session?kind=drill|judge`` serves the next blind items (a picture and a question, nothing else; below 5 approved duos it
answers ``{"locked": true, ...}``). ``POST /api/calibration/labels`` takes ``{item_id, label, like?}`` and answers 204; a drill answer is a label
with ``source="drill"`` (at most a quarter of the calibration set), a judge answer one with ``source="calibration"``.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from duoskin.api import RT
from duoskin.engine import calibration as cal
from duoskin.engine.runtime import Runtime
from duoskin.pipeline import drills

router = APIRouter(prefix="/api")


class LabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=20)
    like: bool | None = None


def _drill_error(exc: drills.DrillError) -> JSONResponse:
    body = {"error": exc.code, "message": str(exc)}
    for k in ("approved_duos", "needed"):
        if k in exc.extra:
            body[k] = exc.extra[k]
    return JSONResponse(body, status_code=exc.status)


@router.get("/calibration/session", response_model=None)
def get_session(kind: Literal["drill", "judge"] = "drill", rt: Runtime = RT) -> dict[str, Any] | JSONResponse:
    try:
        return drills.session_for(rt, kind)
    except drills.DrillError as exc:
        return _drill_error(exc)


@router.post("/calibration/labels", status_code=204, response_class=Response, response_model=None)
def post_label(body: LabelIn, rt: Runtime = RT) -> Response | JSONResponse:
    try:
        drills.answer_any(rt, body.item_id, body.label, body.like)
    except drills.DrillError as exc:
        return _drill_error(exc)
    return Response(status_code=204)


@router.post("/calibration/session/end", status_code=204, response_class=Response)
def end_session(rt: Runtime = RT) -> Response:
    """"Stop here": the round closes; the next request starts a new one."""
    drills.end_session(rt)
    return Response(status_code=204)


@router.get("/calibration/status")
def get_status(rt: Runtime = RT) -> dict[str, Any]:
    """The unlock state and the label counts by source (drill share at most 25%)."""
    return drills.status(rt)


@router.get("/calibration/labels")
def get_counts(rt: Runtime = RT) -> dict[str, Any]:
    return cal.label_counts(rt).as_dict()
