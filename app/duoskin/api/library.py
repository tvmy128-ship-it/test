"""Library routes (APP_SPEC §13, §10.1): the kit summary, adding a kit, the head-base build and the manifest rebuild."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from duoskin.api import RT
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


class KitIn(BaseModel):
    folder_path: str
    kind: str
    origin: str
    license: str = "n/a"


class HeadIn(BaseModel):
    source_path: str
    variant: str


@router.get("/library")
def get_library(rt: Runtime = RT) -> dict[str, Any]:
    from duoskin.pipeline import library

    return library.summary(rt)


@router.post("/library/kits")
def add_kit(body: KitIn, rt: Runtime = RT) -> dict[str, Any]:
    """Validate and copy a kit folder (the user's own, with its ``origin`` and ``license``) and rebuild the manifest."""
    from duoskin.pipeline import library

    try:
        return library.add_kit(rt, body.folder_path, body.kind, body.origin, body.license)
    except library.KitError as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_kit", "message": str(exc)}) from exc


@router.post("/library/head-base/build")
def build_head_base(body: HeadIn, rt: Runtime = RT):
    """A LIBRARY job with the step ``kit.build_head`` (needs Blender, APP_SPEC §10.9.1)."""
    from duoskin.models.job import JobKind
    from duoskin.pipeline import kits
    from duoskin.pipeline.library import HeadParams

    if not kits.blender_present(rt):
        raise HTTPException(status_code=409, detail={"error": "blender_missing", "message": "Building a head base needs Blender. Install it, or add a prebuilt head-base folder with Add kit."})
    step = rt.ops.new_step("kit.build_head", job_id="", params=HeadParams(source_path=body.source_path, variant=body.variant).model_dump(mode="json"))
    return rt.scheduler.submit_job(JobKind.LIBRARY, None, {"variant": body.variant}, steps=[step])


@router.post("/library/rebuild-manifest")
def rebuild(rt: Runtime = RT) -> dict[str, Any]:
    from duoskin.pipeline import library

    return library.rebuild_manifest(rt)
