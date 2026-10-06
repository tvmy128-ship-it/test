"""Manual 3D imports (APP_SPEC §11.4, §13): drop a model, see the inbox, answer the wizard ("which Tripo plan made this file?")."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from duoskin.api import RT
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")
MAX_IMPORT_BYTES = 50 * 1024 * 1024


class AssignIn(BaseModel):
    project_id: str
    part_id: str
    tripo_plan: Literal["free", "paid", "not_tripo"]
    task_link: str = Field(default="", max_length=500)      # stored in the part's provenance and in the export kit: short, and scrubbed of keys there


@router.post("/imports")
def post_import(file: Annotated[UploadFile, File()], project_id: Annotated[str, Form()] = "", part_id: Annotated[str, Form()] = "",
                rt: Runtime = RT) -> dict[str, Any]:
    """A dropped mesh file (<= 50 MB, the type is read from the content later, meshes are only parsed in the worker subprocess) goes to the inbox."""
    from duoskin.pipeline import manual_mesh

    data = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail={"error": "too_large", "message": "the file is larger than 50 MB: export it with fewer triangles and a 1024 px texture"})
    if not data:
        raise HTTPException(status_code=422, detail={"error": "empty", "message": "the file is empty"})
    try:
        entry = manual_mesh.ingest_upload(rt, file.filename or "upload.glb", data, project_id=project_id or None, part_id=part_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_file", "message": str(exc)}) from exc
    return entry


@router.post("/imports/{inbox_id}/assign")
def assign(inbox_id: str, body: AssignIn, rt: Runtime = RT) -> dict[str, Any]:
    """The import wizard: the Tripo plan (free / paid / not Tripo = made by me), an optional task link; starts the mesh gate for the tile."""
    from duoskin.pipeline import manual_mesh

    rt.repo.get_part(body.project_id, body.part_id)
    try:
        return manual_mesh.assign_import(rt, inbox_id, body.project_id, body.part_id, tripo_plan=body.tripo_plan, task_link=body.task_link)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "no such inbox entry"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_request", "message": str(exc)}) from exc


@router.get("/inbox")
def inbox(rt: Runtime = RT) -> dict[str, Any]:
    """Unassigned and pending imports. A poll of the watched folders runs first, so the list is current even without the background thread."""
    from duoskin.pipeline import manual_mesh

    try:
        manual_mesh.watcher(rt).poll_once()
    except OSError:
        pass
    entries = manual_mesh.inbox_entries(rt)
    return {"entries": entries, "inbox_dir": str(manual_mesh.inbox_dir(rt)), "unassigned": [e for e in entries if not e["assigned_part"] and e["state"] in ("ready", "arriving")]}
