"""Projects, references, plan start, pause/resume/archive and spec listings (APP_SPEC §13)."""
from __future__ import annotations

import io
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from duoskin.api import RT, not_implemented
from duoskin.api.state import project_summaries
from duoskin.db.errors import ConflictError
from duoskin.engine import deps
from duoskin.engine.cas import CasError, make_prov
from duoskin.engine.runtime import Runtime
from duoskin.engine.scheduler import has_job_factory
from duoskin.models.common import new_id, utcnow
from duoskin.models.job import Job, JobKind
from duoskin.models.project import (
    Project,
    ProjectCreate,
    ProjectPatch,
    ProjectSettings,
    ProjectSummary,
    ReferenceImage,
    Stage,
)

router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PIXELS = 100_000_000
_IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}


def _defaults(rt: Runtime) -> dict[str, Any]:
    s = rt.settings
    return {"budget_usd": s.budgets.per_duo_usd, "ask_above_usd": s.budgets.ask_above_usd,
            "reference_similarity_check": s.checks.reference_similarity_default, "mesh_mode": s.three_d.default_mesh_mode,
            "hair_route": s.three_d.hair_default_route, "gemini_second_opinion": s.checks.gemini_second_opinion,
            "build_start_per_tile": s.three_d.build_start_per_tile}


@router.get("/projects")
def list_projects(include_archived: bool = False, rt: Runtime = RT) -> list[ProjectSummary]:
    return project_summaries(rt, include_archived)


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, rt: Runtime = RT) -> Project:
    try:
        settings = ProjectSettings.model_validate({**_defaults(rt), **body.settings})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_settings", "message": str(exc)}) from exc
    if not body.structure_request.replace("_", "").isalnum() or len(body.structure_request) > 40:
        raise HTTPException(status_code=422, detail={"error": "bad_structure", "message": "pair structure must be 'auto' or a structure name"})
    now = utcnow()
    project = Project(id=new_id("prj"), name=body.name.strip(), slug=rt.repo.unique_slug(body.name), created_at=now,
                      updated_at=now, combo=body.combo, brief=body.brief, structure_request=body.structure_request,
                      must_include=body.must_include, stage=Stage.BRIEF, settings=settings)
    rt.repo.create_project(project)
    rt.bus.emit("project.stage", {"project_id": project.id, "stage": project.stage.value, "created": True}, project.id)
    return project


@router.get("/projects/{project_id}")
def get_project(project_id: str, rt: Runtime = RT) -> dict[str, Any]:
    project = rt.repo.get_project(project_id)
    spec = dna = None
    spec_id = project.current_spec_id or project.approved_spec_id
    if spec_id:
        rec = rt.repo.get_spec(spec_id)
        spec = rec.model_dump(mode="json")
        dna = rt.repo.get_dna_card(spec_id)
    gates = [rt.gates.view(g).model_dump(mode="json") for g in rt.repo.list_gates(project_id, "open")]
    return {"project": project.model_dump(mode="json"),
            "parts": [p.model_dump(mode="json") for p in rt.repo.list_parts(project_id)],
            "spec": spec, "dna_card": dna, "open_gates": gates}


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch, rt: Runtime = RT) -> Project:
    project = rt.repo.get_project(project_id)
    if project.version != body.expected_version:
        raise ConflictError("the project changed since you loaded it", project)
    if body.settings is not None:
        if project.stage != Stage.BRIEF:
            raise ConflictError("settings can only change before the plan starts", project, code="settings_locked")
        try:
            project.settings = ProjectSettings.model_validate({**project.settings.model_dump(), **body.settings})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail={"error": "bad_settings", "message": str(exc)}) from exc
    if body.name is not None:
        project.name = body.name.strip()
    return rt.repo.save_project(project, body.expected_version)


@router.post("/projects/{project_id}/references")
def add_reference(project_id: str, file: Annotated[UploadFile, File()], role: Annotated[str, Form()] = "reference",
                  note: Annotated[str, Form()] = "",
                  rt: Runtime = RT) -> ReferenceImage:
    """Upload a reference image (<= 50 MB; the type is sniffed from the content, never from the file name)."""
    if role not in ("reference", "favourite"):
        raise HTTPException(status_code=422, detail={"error": "bad_role", "message": "role must be reference or favourite"})
    project = rt.repo.get_project(project_id)
    if len(project.references) >= 4:
        raise HTTPException(status_code=409, detail={"error": "too_many_references", "message": "a project holds at most 4 references"})
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail={"error": "too_large", "message": "the image is larger than 50 MB"})
        chunks.append(chunk)
    data = b"".join(chunks)
    kind = _sniff_image(data)
    try:
        asset = rt.cas.put(data, kind, prov=make_prov("user", stream="pipeline", notes=["reference"]))
    except CasError as exc:
        raise HTTPException(status_code=415, detail={"error": "bad_image", "message": str(exc)}) from exc
    ref = ReferenceImage(asset_sha=asset.sha256, role=role, note=note[:200])   # type: ignore[arg-type]
    rt.repo.mutate_project(project_id, lambda p: p.references.append(ref))
    return ref


def _sniff_image(data: bytes) -> str:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt, (w, h) = im.format or "", im.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail={"error": "bad_image", "message": "this is not a PNG, JPEG or WebP image"}) from exc
    if fmt not in _IMAGE_FORMATS:
        raise HTTPException(status_code=415, detail={"error": "bad_image", "message": f"{fmt or 'this'} images are not accepted"})
    if w * h > MAX_PIXELS:
        raise HTTPException(status_code=413, detail={"error": "too_large", "message": "the image has too many pixels"})
    return _IMAGE_FORMATS[fmt]


@router.post("/projects/{project_id}/plan")
def start_plan(project_id: str, rt: Runtime = RT):
    """Start the PLAN job. The steps come from the pipeline's registered job factory (``register_job_factory``)."""
    project = rt.repo.get_project(project_id)
    if not has_job_factory(JobKind.PLAN):
        from fastapi.responses import JSONResponse

        return JSONResponse(not_implemented("The plan loop", "pipeline"), status_code=501)
    if project.stage not in (Stage.BRIEF, Stage.PLANNING):
        raise ConflictError(f"the project is at stage '{project.stage.value}'", project, code="wrong_stage")
    if project.plan_job_id:
        existing = rt.repo.find_job(project.plan_job_id)
        if existing is not None and existing.state.value in ("running", "waiting_user", "paused"):
            raise ConflictError("a plan job is already running", existing, code="plan_running")
    job: Job = rt.scheduler.submit_job(JobKind.PLAN, project_id, {})
    rt.repo.mutate_project(project_id, lambda p: (setattr(p, "plan_job_id", job.id), setattr(p, "stage", Stage.PLANNING)))
    rt.bus.emit("project.stage", {"project_id": project_id, "stage": Stage.PLANNING.value}, project_id)
    return job


@router.post("/projects/{project_id}/pause")
def pause_project(project_id: str, rt: Runtime = RT) -> Project:
    rt.repo.get_project(project_id)
    rt.scheduler.pause(project_id)
    return rt.repo.get_project(project_id)


@router.post("/projects/{project_id}/resume")
def resume_project(project_id: str, rt: Runtime = RT) -> Project:
    rt.repo.get_project(project_id)
    rt.scheduler.resume(project_id)
    return rt.repo.get_project(project_id)


@router.post("/projects/{project_id}/archive")
def archive_project(project_id: str, rt: Runtime = RT) -> Project:
    """Hide the project. Never deletes approved or exported assets."""
    rt.repo.get_project(project_id)
    return rt.repo.mutate_project(project_id, lambda p: setattr(p, "archived", True))


@router.get("/projects/{project_id}/specs")
def list_specs(project_id: str, rt: Runtime = RT) -> list[dict[str, Any]]:
    rt.repo.get_project(project_id)
    out = []
    for rec in rt.repo.list_specs(project_id):
        out.append({"spec": rec.model_dump(mode="json"), "dna_card": rt.repo.get_dna_card(rec.id), "lint": [r.model_dump(mode="json") for r in rec.lint]})
    return out


@router.get("/specs/{spec_id}/diff/{other_id}")
def diff_specs(spec_id: str, other_id: str, rt: Runtime = RT) -> dict[str, Any]:
    """Changed JSON pointers between two specs (``old`` = ``spec_id``, ``new`` = ``other_id``) with both values, plus the DNA-card diff."""
    old, new = rt.repo.get_spec(spec_id), rt.repo.get_spec(other_id)
    changes = [{"path": p, "old": deps.resolve(old.spec, p), "new": deps.resolve(new.spec, p)}
               for p in deps.changed_pointers(old.spec, new.spec)]
    card_a, card_b = rt.repo.get_dna_card(spec_id), rt.repo.get_dna_card(other_id)
    dna = ([{"path": p, "old": deps.resolve(card_a, p), "new": deps.resolve(card_b, p)}
            for p in deps.changed_pointers(card_a, card_b)] if card_a is not None and card_b is not None else [])
    return {"spec_changes": changes, "dna_changes": dna}
