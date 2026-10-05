"""Export routes (APP_SPEC §13, §10.13): STUBS for the export track. Each answers ``501 not_implemented``."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from duoskin.api import not_implemented

router = APIRouter(prefix="/api")


@router.post("/projects/{project_id}/export")   # STUB
def start_export(project_id: str) -> JSONResponse:
    return JSONResponse(not_implemented("The export kit", "export"), status_code=501)


@router.get("/exports/{project_id}")   # STUB
def get_export(project_id: str) -> JSONResponse:
    return JSONResponse(not_implemented("The export kit", "export"), status_code=501)


@router.patch("/exports/{project_id}/checklist")   # STUB
def patch_checklist(project_id: str) -> JSONResponse:
    return JSONResponse(not_implemented("The export checklist", "export"), status_code=501)
