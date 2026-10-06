"""The BUILD job (APP_SPEC §2 S10, §10.5-10.10, §11): everything that turns the approved board into the files of the upload kit.

``start_build(rt, project_id)`` runs when every tile of Gate 2 is approved (``parts._after_approval``). A BUILD job has one ``build.plan`` step that
decides the 3D route (``mesh_mode`` api / manual / ask) and spawns one chain per part:

==========  ============================================================================================================
shirt/pants ``template.finalize`` (re-open and validate the final PNG)
colours     ``body.compose`` (``body_colors.json``; the modesty layer with a body base)
face        ``face.finalize`` (the layer pack, no head base) or ``head.texture`` + ``head.check`` (head base)
print       nothing to build: its board final is what the garment already placed
hair        kit: ``hair.kit_match`` -> ``hair.fit`` -> ``mesh.validate`` -> ``mesh.judge``;  custom: ``tripo.model`` (or the Tripo pack) ->
            ``mesh.import`` -> ``hair.register`` -> ``mesh.validate`` -> ``mesh.judge``
accessory   tripo: ``tripo.model`` (or the pack) -> ``mesh.import`` -> ``mesh.repair`` -> ``mesh.validate`` -> ``mesh.judge``;
            sticker slab: ``slab.build`` -> ``mesh.validate``;  primitive: ``primitive.build`` -> ``mesh.validate``
==========  ============================================================================================================

Every chain ends in ``build.finish`` (the build assets are in ``Part.build_assets``; ``deps.stamp_build`` writes the second stamp, the
``build_hash``, and the part becomes BUILT). When every part is BUILT the DUO job starts.

``tripo.model`` is a remote step: ``set_remote_ref`` is called by the adapter the moment Tripo answers (before ``Pending``), ``poll`` resumes after a
restart and never submits again. Seeds run one at a time (11, 29, 47) and a failure only counts after the mesh gate and the judge (3-vote majority)
said so; then the P1 route runs once; then the user gets the Tripo pack (``manual_mesh``).
"""
from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from duoskin.db.errors import NotFound
from duoskin.engine import deps, registry
from duoskin.engine import errors as eng_errors
from duoskin.engine import scheduler as sched
from duoskin.engine.errors import StepFailure
from duoskin.engine.gates import ApplyContext, ApplyResult
from duoskin.engine.registry import Pending, StepResult
from duoskin.models.common import Strict
from duoskin.models.gate import GateAction, GateKind, TileState
from duoskin.models.job import Job, JobKind, Step, StepState
from duoskin.models.part import Part, PartKind, PartState
from duoskin.models.project import Stage
from duoskin.pipeline import common, itemspec, kits, meshrun, meshsteps, multiview, parts
from duoskin.pipeline.meshsteps import MeshStepParams

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.build")

SEEDS = (11, 29, 47)
MOCK_POLL_S = 0.05


class PlanParams(Strict):
    project_id: str


class FinishParams(Strict):
    part_id: str
    from_step: str = ""


class ModelParams(Strict):
    part_id: str
    seed_index: int = 0
    route: str = "p2"                       # p2 | p1
    nonce: str = ""
    mv_task_id: str = ""
    views: dict[str, str] = Field(default_factory=dict)
    views_from_gpt: bool = False
    mv_step: str = ""


# ---------------------------------------------------------------------------------------------------- build assets
def set_build_assets(rt: Runtime, project_id: str, part_id: str, assets: dict[str, str], *, replace: bool = False,
                     extra: dict[str, Any] | None = None) -> Part:
    """Record the part's build files (role -> sha). The approval is untouched; ``build_hash`` is written by ``deps.stamp_build`` at BUILT."""
    part = rt.repo.mutate_part(project_id, part_id, lambda p: setattr(p, "build_assets", dict(assets) if replace else {**p.build_assets, **assets}))
    if extra:
        parts.set_tile_facts(rt, project_id, part_id, **extra)
    return part


# ---------------------------------------------------------------------------------------------------- starting
def start_build(rt: Runtime, project_id: str) -> Job:
    """Start the BUILD job (idempotent per approval round: a second call while one runs returns the running job)."""
    for j in rt.repo.list_jobs(project_id=project_id, limit=20):
        if j.kind == JobKind.BUILD and j.state.value in ("running", "waiting_user", "queued"):
            return j
    rt.repo.set_project_stage(project_id, Stage.BUILDING, bus=rt.bus)
    return rt.scheduler.submit_job(JobKind.BUILD, project_id, {"reason": "every tile approved"}, spec_id=rt.repo.get_project(project_id).approved_spec_id)


def _build_steps(rt: Runtime, job: Job, project: Any) -> list[Step]:
    if not common.handlers_present("build.plan"):
        return []
    pid = job.project_id or ""
    return [rt.ops.new_step("build.plan", job_id=job.id, project_id=pid, params=PlanParams(project_id=pid).model_dump(mode="json"))]


def tripo_ok(rt: Runtime) -> bool:
    return common.provider_available(rt, "tripo")


def needs_mesh_from_tripo(spec: dict[str, Any], part: Part, route: str) -> bool:
    if part.kind == PartKind.ACCESSORY:
        return spec[part.character]["accessories"][common.split_part_id(part.id).index or 0]["build"] == "tripo"
    return part.kind == PartKind.HAIR and route == "custom"


def estimate_api_usd(rt: Runtime, project_id: str, spec: dict[str, Any]) -> tuple[float, int]:
    """About what the API route would cost for the meshes of this duo, and how many meshes there are (the "ask" gate shows it)."""
    from duoskin.pipeline import hair as H
    from duoskin.providers import pricing

    n = 0
    for p in rt.repo.list_parts(project_id):
        if p.kind in (PartKind.HAIR, PartKind.ACCESSORY):
            route = H.hair_route(rt, project_id, spec, p.character)[0] if p.kind == PartKind.HAIR else ""
            if needs_mesh_from_tripo(spec, p, route):
                n += 1
    return round(n * float(pricing.estimate_tripo("multiview_to_model", route="p2").usd), 4), n


def run_plan(ctx: StepContext, p: PlanParams, inputs: list[Any]) -> StepResult:
    from duoskin.models.common import utcnow
    from duoskin.models.gate import Gate, GateTile

    rt = ctx.rt
    project_id = p.project_id
    project = rt.repo.get_project(project_id)
    _, spec = common.load_spec(rt, project_id)
    mode = project.settings.mesh_mode
    if mode == "ask":
        est, n = estimate_api_usd(rt, project_id, spec)
        if n == 0:
            mode = "api"
        elif not tripo_ok(rt):
            mode = "api"                       # no key: nothing to ask, the chain of every mesh part goes to the Tripo pack ("no Tripo key")
        else:
            tile = GateTile(tile_id=ctx.step.id, part_id=None, label=f"3D models: {n} to make (about ${est:.2f} with the Tripo API)", state=TileState.READY,
                            facts={"mesh_mode_ask": True, "estimate_usd": est, "meshes": n, "step_id": ctx.step.id, "step_kind": "build.plan",
                                   "choices": {"continue": "Use the Tripo API", "stop": "I will make the 3D models myself on Tripo's website"}},
                            badges=["3D route"], allowed_actions=[GateAction.CONTINUE, GateAction.STOP])   # type: ignore[arg-type]
            gate = Gate(id="", project_id=project_id, job_id=ctx.step.job_id, kind=GateKind.BUDGET, tiles=[tile], opened_at=utcnow())   # type: ignore[arg-type]
            ctx.open_gate(gate)
            return StepResult(message="waiting for the 3D route")
    no_key = mode == "api" and not tripo_ok(rt)
    created: list[Step] = []
    todo = [x for x in rt.repo.list_parts(project_id) if x.kind != PartKind.DUO and x.state in (PartState.APPROVED, PartState.BUILDING)]
    for part in todo:
        parts.set_part_state(rt, project_id, part.id, PartState.BUILDING)
    for part in todo:
        created += part_chain(rt, ctx.step.job_id, project_id, part, spec, mode, priority=ctx.step.priority)
    all_parts = todo
    if created:
        ctx.spawn(created)
    return StepResult(result={"mode": mode, "steps": len(created), "no_tripo_key": no_key},
                      message=f"building {len(all_parts)} parts ({'no Tripo key: the Tripo packs' if no_key else mode + ' 3D'})")


# ---------------------------------------------------------------------------------------------------- chains
def _step(rt: Runtime, kind: str, job_id: str, project_id: str, part_id: str | None, params: dict[str, Any], *, deps_: list[str] | None = None,
          priority: int = 100, nonce: str = "") -> Step:
    return rt.ops.new_step(kind, job_id=job_id, project_id=project_id, part_id=part_id, params=params, deps=deps_ or [], priority=priority, nonce=nonce)


def _finish(rt: Runtime, job_id: str, project_id: str, part: Part, after: Step | None, priority: int) -> Step:
    return _step(rt, "build.finish", job_id, project_id, part.id, FinishParams(part_id=part.id, from_step=after.id if after else "").model_dump(mode="json"),
                 deps_=[after.id] if after else [], priority=priority)


def part_chain(rt: Runtime, job_id: str, project_id: str, part: Part, spec: dict[str, Any], mode: str, *, priority: int = 100) -> list[Step]:
    """The BUILD steps of one part (ending in ``build.finish``)."""
    from duoskin.pipeline import hair as H
    from duoskin.pipeline.clothing import ComposeParams
    from duoskin.pipeline.colours import ColoursParams
    from duoskin.pipeline.face import FaceParams

    k = part.kind
    if k in (PartKind.SHIRT, PartKind.PANTS):
        s = _step(rt, "template.finalize", job_id, project_id, part.id, ComposeParams(part_id=part.id).model_dump(mode="json"), priority=priority)
        return [s, _finish(rt, job_id, project_id, part, s, priority)]
    if k == PartKind.COLOURS:
        s = _step(rt, "body.compose", job_id, project_id, part.id, ColoursParams(part_id=part.id).model_dump(mode="json"), priority=priority)
        return [s, _finish(rt, job_id, project_id, part, s, priority)]
    if k == PartKind.FACE:
        s = _step(rt, "face.finalize", job_id, project_id, part.id, FaceParams(part_id=part.id).model_dump(mode="json"), priority=priority)
        return [s, _finish(rt, job_id, project_id, part, s, priority)]
    if k == PartKind.PRINT:
        return [_finish(rt, job_id, project_id, part, None, priority)]
    if k == PartKind.HAIR:
        route, _flags = H.hair_route(rt, project_id, spec, part.character)
        if route == "kit":
            return kit_hair_chain(rt, job_id, project_id, part, priority=priority)
        return tripo_or_manual_chain(rt, job_id, project_id, part, spec, mode, priority=priority)
    if k == PartKind.ACCESSORY:
        item = itemspec.accessory_item(spec, part.id)
        if item.build == "tripo":
            return tripo_or_manual_chain(rt, job_id, project_id, part, spec, mode, priority=priority)
        kind = "slab.build" if item.build == "sticker_slab" else "primitive.build"
        b = _step(rt, kind, job_id, project_id, part.id, MeshStepParams(part_id=part.id, source={"kind": "slab" if kind == "slab.build" else "primitive",
                                                                                              "judge": False}).model_dump(mode="json"), priority=priority)
        return mesh_tail(rt, job_id, project_id, part, b, {"kind": "slab" if kind == "slab.build" else "primitive", "judge": False}, priority) + []
    return []


def mesh_tail(rt: Runtime, job_id: str, project_id: str, part: Part, first: Step, source: dict[str, Any], priority: int, *, seed_index: int = 0,
              mv_step: str = "") -> list[Step]:
    """``first`` (a step that produces the exported files) -> ``mesh.validate`` -> ``mesh.judge`` (which starts ``build.finish`` on a pass)."""
    v = _step(rt, "mesh.validate", job_id, project_id, part.id,
              MeshStepParams(part_id=part.id, from_step=first.id, source=source, seed_index=seed_index, mv_step=mv_step).model_dump(mode="json"),
              deps_=[first.id], priority=priority)
    j = _step(rt, "mesh.judge", job_id, project_id, part.id,
              MeshStepParams(part_id=part.id, from_step=v.id, source=source, seed_index=seed_index, mv_step=mv_step).model_dump(mode="json"),
              deps_=[v.id], priority=priority)
    return [first, v, j]


def mesh_chain(rt: Runtime, job_id: str, project_id: str, part: Part, *, source: dict[str, Any], model_sha: str, work_name: str = "model.glb",
               seed_index: int = 0, nonce: str = "", mv_step: str = "", expect_pack_id: str = "", priority: int = 100, deps_: list[str] | None = None) -> list[Step]:
    """``mesh.import`` -> ``mesh.repair`` / ``hair.register`` -> ``mesh.validate`` -> ``mesh.judge`` for one model file."""
    spec = common.load_spec(rt, project_id)[1]
    is_hair = part.kind == PartKind.HAIR
    imp = _step(rt, "mesh.import", job_id, project_id, part.id,
                MeshStepParams(part_id=part.id, source=source, seed_index=seed_index, nonce=nonce, mv_step=mv_step, model_sha=model_sha, work_name=work_name,
                               expect_pack_id=expect_pack_id).model_dump(mode="json"), deps_=deps_, priority=priority)
    rep = _step(rt, "hair.register" if is_hair else "mesh.repair", job_id, project_id, part.id,
                MeshStepParams(part_id=part.id, from_step=imp.id, source=source, seed_index=seed_index, mv_step=mv_step).model_dump(mode="json"),
                deps_=[imp.id], priority=priority)
    _ = spec
    return [imp, *mesh_tail(rt, job_id, project_id, part, rep, source, priority, seed_index=seed_index, mv_step=mv_step)]


def tripo_or_manual_chain(rt: Runtime, job_id: str, project_id: str, part: Part, spec: dict[str, Any], mode: str, *, priority: int = 100) -> list[Step]:
    """API mode: ``tripo.model`` (its poll spawns the mesh chain). Manual mode (or no Tripo key): the pack and the MANUAL_IMPORT gate."""
    if mode == "manual" or not tripo_ok(rt):
        from duoskin.pipeline import manual_mesh

        return [manual_mesh.pack_step(rt, job_id, project_id, part, priority=priority, reason="manual 3D" if mode == "manual" else "no Tripo key")]
    return [model_step(rt, job_id, project_id, part, seed_index=0, route="p2", priority=priority)]


def model_step(rt: Runtime, job_id: str, project_id: str, part: Part, *, seed_index: int, route: str, priority: int = 100, nonce: str = "",
               mv_step: str = "") -> Step:
    """One ``tripo.model`` step. The views and the multiview task come from the part's Gate 2 views (the T1 task id is reused while they are
    Tripo's own; GPT views are uploaded)."""
    views = {v: part.board_assets[f"view.{v}"] for v in multiview.VIEWS if f"view.{v}" in part.board_assets}
    mv_task = ""
    from_gpt = "views_from_gpt" in part.flags
    st = rt.repo.kv_get(f"mvtask:{project_id}:{part.id}") or {}
    if not from_gpt:
        mv_task = str(st.get("task_id") or "")
    return _step(rt, "tripo.model", job_id, project_id, part.id,
                 ModelParams(part_id=part.id, seed_index=seed_index, route=route, nonce=nonce, mv_task_id=mv_task, views=views, views_from_gpt=from_gpt or not mv_task,
                             mv_step=mv_step).model_dump(mode="json"), priority=priority, nonce=nonce)


def kit_hair_chain(rt: Runtime, job_id: str, project_id: str, part: Part, *, priority: int = 100) -> list[Step]:
    src = {"kind": "kit", "judge": True}
    m = _step(rt, "hair.kit_match", job_id, project_id, part.id, MeshStepParams(part_id=part.id, source=src).model_dump(mode="json"), priority=priority)
    f = _step(rt, "hair.fit", job_id, project_id, part.id, MeshStepParams(part_id=part.id, from_step=m.id, source=src).model_dump(mode="json"), deps_=[m.id],
              priority=priority)
    return [m, *mesh_tail(rt, job_id, project_id, part, f, src, priority)]


# ---------------------------------------------------------------------------------------------------- tripo.model (T3, remote)
def estimate_model(p: Any) -> float:
    from duoskin.providers import pricing

    return float(pricing.estimate_tripo("multiview_to_model", route=p.route).usd)


def _params_for(item: itemspec.Item, p: ModelParams) -> Any:
    from duoskin.providers import tripo as T

    seed = SEEDS[min(p.seed_index, len(SEEDS) - 1)]
    if p.route == "p1":
        return T.P1Params.for_seed(item.face_limit, seed)
    return T.P2Params.for_seed(item.face_limit, seed)


def run_tripo_model(ctx: StepContext, p: ModelParams, inputs: list[Any]) -> StepResult | Pending:
    if ctx.step.remote_ref:                                  # never submit twice
        return poll_tripo_model(ctx, p, ctx.step.remote_ref)
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    adapter = ctx.provider("tripo")
    item = meshsteps.item_of(rt, project_id, p.part_id)
    params = _params_for(item, p)
    views: Any
    if p.mv_task_id and not p.views_from_gpt:
        views = p.mv_task_id                               # the views are Tripo's own: the T1 task is the input (no re-upload, no re-crop)
    else:
        views = {}
        for v, sha in p.views.items():
            png = multiview.repad(ctx.read_asset(sha)) if v == "front" else ctx.read_asset(sha)
            views[v] = adapter.upload(png, name=multiview.upload_name(p.part_id, v), ctx=ctx.call_ctx())   # just before the request (free)
    from duoskin.providers import pricing

    adapter.ensure_credits(float(pricing.tripo_credits("multiview_to_model", route=p.route)))
    ctx.progress(0.1, f"asking Tripo for the model (seed {params.model_seed})")
    task = multiview._submit(ctx, adapter, "multiview_to_model", views, params, tag="T3")
    return Pending(delay_s=multiview._delay(rt), message=f"Tripo is building the model ({task[:8]}, seed {params.model_seed})")


def poll_tripo_model(ctx: StepContext, p: ModelParams, ref: str) -> StepResult | Pending:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    adapter = ctx.provider("tripo")
    st = adapter.task(ref, ctx=ctx.call_ctx())
    if not st.done:
        return Pending(delay_s=multiview._delay(rt), message=f"Tripo {st.status}" + (" (slow)" if st.slow else ""), progress=(st.progress or 0) / 100.0)
    err = st.failure_error()
    if err is not None:
        raise err
    files = adapter.download_files(ref, ["model_url"], ctx=ctx.call_ctx(), status=st)
    f = files["model_url"]
    seed = SEEDS[min(p.seed_index, len(SEEDS) - 1)]
    mock = common.is_mock(rt, "tripo")
    pv = common.prov("mock" if mock else "tripo", provider="tripo", step_kind="tripo.model", request_id=ref, nonce=p.nonce,
                     model="P1-20260311" if p.route == "p1" else "P2-20260801", license="tripo_api_private_commercial",   # type: ignore[arg-type]
                     params={"seed": seed, "route": p.route, "task_id": ref, "face_limit": meshsteps.item_of(rt, project_id, p.part_id).face_limit,
                             "sha256": f.sha256, "bytes": f.size, "kind": f.kind})
    sha = common.put_bytes(ctx, f.data, "glb" if f.kind == "glb" else f.kind, role="tripo_model", part_id=p.part_id, provenance=pv, status="candidate").sha256
    multiview._record_task_cost(ctx, adapter, st, "tripo.model")
    source = {"kind": "tripo", "seed": seed, "route": p.route, "task_id": ref, "licence": "tripo_api_private_commercial", "judge": True}
    part = rt.repo.get_part(project_id, p.part_id)
    steps = mesh_chain(rt, ctx.step.job_id, project_id, part, source=source, model_sha=sha, work_name=f"model.{f.kind}", seed_index=p.seed_index, nonce=p.nonce,
                       mv_step=p.mv_step, priority=ctx.step.priority)
    ctx.spawn(steps)
    return StepResult(outputs=[sha], result={"task_id": ref, "model_sha": sha, "seed": seed, "route": p.route}, message=f"model downloaded (seed {seed})")


# ---------------------------------------------------------------------------------------------------- code-built meshes
def _store_code_mesh(ctx: StepContext, res: Any, part: Part, kind: str, in_shas: list[str], *, mock: bool = False) -> StepResult:
    rt = ctx.rt
    mock = mock or meshsteps_mock(rt, in_shas)
    common.store_checks(ctx, list(res.checks))
    files = meshsteps.store_files(ctx, res, part.id, source="code", provenance_extra={"licence": "n/a", "kind": kind, "mock_lineage": mock},
                                  input_shas=in_shas)
    blocking = common.hard_failures(list(res.checks))
    return StepResult(outputs=list(files.values()), result={"ok": res.ok and not blocking, "files": files, "facts": meshsteps.slim_facts(res.facts), "mirrored": False,
                                                            "licence": "n/a", "mock": mock,
                                                            "failed": [r.check_id for r in blocking],
                                                            "reason": "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]) or "; ".join(res.messages[:2]),
                                                            "checks": meshsteps.checks_summary(list(res.checks))},
                      message=f"{kind} built" if res.ok and not blocking else f"the {kind} needs a look")


def meshsteps_mock(rt: Runtime, shas: list[str]) -> bool:
    from duoskin.pipeline import clothing

    return clothing.mock_lineage(rt, [s for s in shas if s])


def run_slab_build(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    from duoskin.pipeline import accessory

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    work = meshsteps.work_dir(ctx)
    badge_sha = part.board_assets["badge"]
    art = meshsteps.write_asset(ctx, badge_sha, work / "badge.png")
    job = accessory.build_job(rt, project_id, part, spec, str(work / "out"), op="slab", art_path=str(art))
    job = job.model_copy(update={"forward_axis": meshsteps.forward_axis(rt), "blender_path": meshsteps.blender_path(rt)})
    res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt), timeout_s=300)
    return _store_code_mesh(ctx, res, part, "sticker slab", [badge_sha])


def run_primitive_build(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    from duoskin.pipeline import accessory

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    work = meshsteps.work_dir(ctx)
    job = accessory.build_job(rt, project_id, part, spec, str(work / "out"), op="primitive")
    job = job.model_copy(update={"forward_axis": meshsteps.forward_axis(rt), "blender_path": meshsteps.blender_path(rt)})
    res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt), timeout_s=300)
    return _store_code_mesh(ctx, res, part, "primitive", [])


# ---------------------------------------------------------------------------------------------------- the end of a mesh chain
def fit_json(item: itemspec.Item, facts: dict[str, Any]) -> dict[str, Any]:
    """``fit.json``: what the Studio Accessory Fitting Tool needs (APP_SPEC §10.13): type, attachment, the numeric offset and the size."""
    return {"schema": "duoskin.fit/1", "asset_id": item.part_id, "asset_type": item.asset_type, "attachment": item.attachment,
            "target_studs": list(item.target_studs), "bbox_studs": facts.get("bbox_studs"), "attachment_offset": facts.get("attachment_offset", [0, 0, 0]),
            "scale_type": "Classic", "units": "studs", "up": "+Y", "front": (facts.get("frame") or {}).get("front", "+Z"), "tris": facts.get("tris"),
            "texture_px": facts.get("texture_px"), "fbx": "produced" if facts.get("fbx_produced") else "not produced"}


def mesh_passed(ctx: StepContext, part: Part, p: MeshStepParams, prev: dict[str, Any]) -> None:
    """The model passed the gate and the judge: its files become the part's build assets and ``build.finish`` runs."""
    import json

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    _, spec = common.load_spec(rt, project_id)
    item = itemspec.item_of(spec, part.id)
    files: dict[str, str] = dict(prev.get("files") or {})
    assets: dict[str, str] = {}
    for role, sha in files.items():
        key = role if role in meshsteps.GATE_FILES else "render." + role.split("/", 1)[-1] if role.startswith("renders/") else role
        assets[key] = sha
        asset = ctx.get_asset(sha)
        common.link_asset(rt, project_id, part.id, sha, f"build.{key}", "final", asset.first_provenance, step_id=ctx.step.id)
    facts = dict(prev.get("facts") or {})
    fit = json.dumps(fit_json(item, facts), indent=1, sort_keys=True).encode("utf-8")
    assets["fit_json"] = common.put_bytes(ctx, fit, "json", role="fit_json", part_id=part.id, provenance=common.prov("code", step_kind="build.fit"), status="final").sha256
    licence = str(prev.get("licence") or "n/a")
    set_build_assets(rt, project_id, part.id, assets, replace=True, extra={"mesh_facts": facts, "licence": licence, "mesh_source": p.source.get("kind"),
                                                                          "seed": p.source.get("seed"), "judge": p.source.get("judge", True)})
    flags_add = (["mock"] if prev.get("mock") else []) + (["licence_free_plan"] if licence == "tripo_free_public_ccby_noncommercial" else []) \
        + (["flip_applied"] if prev.get("flipped") else [])
    parts.set_part_state(rt, project_id, part.id, part.state, flags_add=flags_add, flags_remove=["mirrored", "waiting_manual"])
    rt.repo.mutate_part(project_id, part.id, lambda x: setattr(x, "license", licence if licence in _LICENCES else "n/a"))
    fin = _step(rt, "build.finish", ctx.step.job_id, project_id, part.id, FinishParams(part_id=part.id, from_step=ctx.step.id).model_dump(mode="json"),
                deps_=[ctx.step.id], priority=ctx.step.priority)          # it reads this step's result: it must not start before it is stored
    ctx.spawn([fin])


_LICENCES = ("n/a", "tripo_api_private_commercial", "tripo_paid_private_commercial", "tripo_free_public_ccby_noncommercial", "user_made", "unknown")


def mesh_failed(ctx: StepContext, part: Part, p: MeshStepParams, prev: dict[str, Any], *, reason: str, mirrored_only: bool) -> str:
    """What happens after a model failed the gate or the judge: flip offered, the next seed, the next route, or the user (the Tripo pack)."""
    from duoskin.pipeline import manual_mesh

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    kind = p.source.get("kind", "tripo")
    if prev.get("handled"):                                      # the guard of an earlier step already chose the next route
        return "handled"
    if mirrored_only and prev.get("files"):
        rt.repo.kv_set(f"candidate:{project_id}:{part.id}", {"files": prev["files"], "source": p.source, "licence": prev.get("licence"), "facts": prev.get("facts"),
                                                             "mock": prev.get("mock")})
        manual_mesh.offer_flip(rt, project_id, part.id, job_id=ctx.step.job_id, reason=reason or "mirrored model: check the Left/Right slots")
        return "flip_offered"
    if kind == "tripo":
        idx = p.seed_index
        route = str(p.source.get("route", "p2"))
        if route == "p2" and idx + 1 < len(SEEDS):
            ctx.spawn([model_step(rt, ctx.step.job_id, project_id, part, seed_index=idx + 1, route="p2", priority=ctx.step.priority, nonce=common.new_nonce(),
                                  mv_step=p.mv_step)])
            return f"seed {SEEDS[idx + 1]}"
        if route == "p2":
            ctx.spawn([model_step(rt, ctx.step.job_id, project_id, part, seed_index=0, route="p1", priority=ctx.step.priority, nonce=common.new_nonce(),
                                  mv_step=p.mv_step)])
            return "route p1"
    if kind == "kit":
        parts.set_tile_facts(rt, project_id, part.id, report=f"the kit hair failed the mesh gate: {reason}")
    if prev.get("handled"):
        return "handled"
    if not all(f"view.{v}" in part.board_assets for v in ("front", "left", "back", "right")):
        # made by code (a sticker slab, a primitive): there are no four approved pictures to put in a Tripo pack, so a pack could never be made and
        # the part would wait for ever behind a "Try again" that fails again. The person is asked to look at the part instead (as for any failed build).
        parts.set_tile_facts(rt, project_id, part.id, report=f"The build of this part did not pass the checks: {reason or 'the model could not be made'}. "
                                                              "Draw it again or change it, then approve it again.")
        parts.set_part_state(rt, project_id, part.id, PartState.NEEDS_HUMAN, flags_add=["build_failed"])
        parts.refresh_tile(rt, project_id, part.id)
        return "needs_help"
    manual_mesh.start_manual(rt, project_id, part.id, job_id=ctx.step.job_id, reason=reason or "the model could not be made automatically",
                             step_ctx=ctx)
    return "manual"


# ---------------------------------------------------------------------------------------------------- a step that fails for good
def guarded(fn: Any, provider: str | None = None) -> Any:
    """Wrap a BUILD handler so that a failure the engine would not retry (or the last attempt) never leaves the tile hanging: the next Tripo seed or
    route, else the Tripo pack for the user. The engine still retries what it would retry; a cancelled step stays cancelled."""
    @functools.wraps(fn)
    def run(ctx: StepContext, p: Any, *rest: Any) -> Any:
        try:
            return fn(ctx, p, *rest)
        except Exception as exc:
            if eng_errors.is_cancelled(exc):
                raise
            decision = eng_errors.decide(ctx.step, exc, provider)
            if decision.action != "fail" or not getattr(p, "part_id", ""):
                raise
            return fall_back(ctx, p, decision.error.user_hint or decision.error.message)
    return run


def fall_back(ctx: StepContext, p: Any, reason: str) -> StepResult:
    """The step cannot give a result: the next seed / route for a Tripo model, else the tile goes to the user (the Tripo pack, with the reason)."""
    from duoskin.pipeline import manual_mesh

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    reason = (reason or "the step failed")[:300]
    log.warning("build step %s of %s failed for good: %s", ctx.step.kind, part.id, reason)
    if ctx.step.kind == "tripo.model":
        idx, route = int(getattr(p, "seed_index", 0)), str(getattr(p, "route", "p2"))
        nxt = None
        if route == "p2" and idx + 1 < len(SEEDS):
            nxt = {"seed_index": idx + 1, "route": "p2"}
        elif route == "p2":
            nxt = {"seed_index": 0, "route": "p1"}
        if nxt is not None and "budget" not in reason.lower():
            ctx.spawn([model_step(rt, ctx.step.job_id, project_id, part, priority=ctx.step.priority, nonce=common.new_nonce(), mv_step=getattr(p, "mv_step", ""), **nxt)])
            return StepResult(result={"ok": False, "handled": True, "reason": reason, "next": f"seed {nxt['seed_index']} {nxt['route']}"},
                              message=f"Tripo could not make the model ({reason}); trying again")
    manual_mesh.start_manual(rt, project_id, part.id, job_id=ctx.step.job_id, reason=f"{ctx.step.kind} could not finish: {reason}", step_ctx=ctx)
    return StepResult(result={"ok": False, "handled": True, "skipped": True, "reason": reason, "next": "manual"}, message="could not be made automatically: the Tripo pack is ready")


# ---------------------------------------------------------------------------------------------------- flip
def flip_mirrored(rt: Runtime, project_id: str, part_id: str, decision_id: str = "") -> None:
    """The decision "Flip left/right (I checked)": mirror the candidate, re-validate it and continue the chain. Logged on the part (``flip_applied``)."""
    cand = rt.repo.kv_get(f"candidate:{project_id}:{part_id}")
    if not cand:
        raise StepFailure("there is no mirrored model to flip", kind="bad_request", billed="no")
    part = rt.repo.get_part(project_id, part_id)
    job_id = _job_for_new_steps(rt, project_id, part_id)
    src = {**(cand.get("source") or {}), "flip_decision": decision_id}
    carry = _step(rt, "mesh.carry", job_id, project_id, part_id, MeshStepParams(part_id=part_id, source=src).model_dump(mode="json"))
    rt.repo.kv_set(f"carry:{carry.id}", {"files": cand["files"], "licence": cand.get("licence"), "facts": cand.get("facts"), "mock": cand.get("mock"), "ok": True})
    flip = _step(rt, "mesh.flip", job_id, project_id, part_id, MeshStepParams(part_id=part_id, from_step=carry.id, source=src).model_dump(mode="json"), deps_=[carry.id])
    steps = [carry, flip, *mesh_tail(rt, job_id, project_id, part, flip, src, 100)[1:]]
    rt.scheduler.spawn(job_id, steps)
    parts.set_part_state(rt, project_id, part_id, PartState.BUILDING, flags_add=["flip_applied"], flags_remove=["mirrored"])


def run_carry(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    """Hands the stored mirrored candidate to ``mesh.flip`` as the previous step's result."""
    data = ctx.rt.repo.kv_get(f"carry:{ctx.step.id}") or {}
    return StepResult(result=dict(data), message="candidate carried over")


def _job_for_new_steps(rt: Runtime, project_id: str, part_id: str) -> str:
    for j in reversed(rt.repo.list_jobs(project_id=project_id, limit=50)):
        if j.kind in (JobKind.BUILD, JobKind.MANUAL_MESH) and j.state.value != "cancelled":
            return j.id
    return rt.scheduler.submit_job(JobKind.BUILD, project_id, {"only": [part_id]}, steps=[]).id


# ---------------------------------------------------------------------------------------------------- build.finish
def run_finish(ctx: StepContext, p: FinishParams, inputs: list[Any]) -> StepResult:
    """Stamp the build (``build_hash``), mark the part BUILT and start the DUO job when every part is built."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    prev = dict(rt.repo.get_step(p.from_step).result) if p.from_step else {}
    if prev.get("ok") is False:
        reason = str(prev.get("reason") or "the build step did not finish")
        parts.set_tile_facts(rt, project_id, part.id, report=reason)
        parts.set_part_state(rt, project_id, part.id, PartState.NEEDS_HUMAN, flags_add=["build_failed"])
        return StepResult(result={"ok": False, "reason": reason}, message="the build needs a look")
    if not part.build_assets:
        default = _default_build_assets(part)
        if default:
            set_build_assets(rt, project_id, part.id, default)
    try:
        stamped = deps.stamp_build(rt.repo, project_id, part.id)
    except ValueError as exc:
        # a model made by hand before the tile was approved (or after its approval was lost): the part waits for the person's OK, and the board
        # says so (the tile is refreshed, the "waiting for your file" flag of the pack that is now imported is dropped)
        parts.set_part_state(rt, project_id, part.id, PartState.STALE, flags_remove=["waiting_manual"])
        parts.set_tile_facts(rt, project_id, part.id, report="The 3D model is ready and passed the checks. Approve this part again to build with it.")
        parts.refresh_tile(rt, project_id, part.id)
        return StepResult(result={"ok": False, "reason": str(exc)}, message="the approval is gone: approve the tile again")
    rt.bus.emit("part.state", {"project_id": project_id, "part_id": part.id, "state": "built"}, project_id)
    started = maybe_start_duo(rt, project_id)
    return StepResult(result={"ok": True, "build_hash": stamped.build_stamp.build_hash if stamped.build_stamp else None, "duo_started": started},
                      message=f"{part.id} built")


def _default_build_assets(part: Part) -> dict[str, str]:
    if part.kind in (PartKind.SHIRT, PartKind.PANTS) and "template" in part.board_assets:
        return {"template": part.board_assets["template"]}
    if part.kind == PartKind.PRINT and "final" in part.board_assets:
        return {"final": part.board_assets["final"]}
    return {}


def maybe_start_duo(rt: Runtime, project_id: str) -> bool:
    """Every character part BUILT: start the DUO job once."""
    from duoskin.pipeline import duo

    with rt.db.tx():
        ps = [x for x in rt.repo.list_parts(project_id) if x.kind != PartKind.DUO]
        if not ps or any(x.state != PartState.BUILT for x in ps):
            return False
        key = f"duo_start:{project_id}:" + ",".join(sorted(x.build_stamp.build_hash[:8] for x in ps if x.build_stamp))
        if rt.repo.kv_get(key):
            return False
        rt.repo.kv_set(key, True)
    duo.start_duo(rt, project_id)
    return True


# ---------------------------------------------------------------------------------------------------- the "ask" gate
BUILD_PAID_KINDS = ("tripo.model", "hair.kit_match")


def _stopped_at_budget(rt: Runtime, ac: ApplyContext) -> None:
    """"Stop" on the budget gate of a model step: the step fails with "budget" (the engine's rule) and the tile is not left hanging: the Tripo pack
    opens, so the user can make the model on Tripo's website instead (rows and steps only: the pack folder is written by the step)."""
    from duoskin.pipeline import manual_mesh

    manual_mesh.start_manual(rt, ac.project_id, str(ac.tile.part_id), job_id=ac.gate.job_id, reason="you stopped at the budget gate")


def wrap_budget_applier(rt: Runtime) -> None:
    """The 3D-route question reuses the BUDGET gate (Continue = the Tripo API, Stop = I make the models myself); other budget gates are unchanged."""
    prev = rt.gates._appliers.get(GateKind.BUDGET.value)
    if prev is None or getattr(prev, "_mesh_mode_wrapped", False):
        return

    def apply(ac: ApplyContext) -> ApplyResult | None:
        if not ac.tile.facts.get("mesh_mode_ask"):
            res = prev(ac)
            if ac.decision.action == GateAction.STOP and ac.tile.facts.get("step_kind") in BUILD_PAID_KINDS and ac.tile.part_id:
                _stopped_at_budget(rt, ac)
            return res
        mode = "api" if ac.decision.action in (GateAction.CONTINUE, GateAction.RAISE_CAP) else "manual"
        rt.repo.mutate_project(ac.project_id, lambda x: setattr(x.settings, "mesh_mode", mode))
        rt.ops.transition(ac.tile.tile_id, StepState.READY, expect=StepState.WAITING_USER,
                          update=lambda s: (setattr(s, "budget_ok", True), setattr(s, "gate_id", None), setattr(s, "message", f"3D route: {mode}")))
        ac.tile.state = TileState.APPROVED
        return ApplyResult(close_gate=True, step="keep")

    apply._mesh_mode_wrapped = True      # type: ignore[attr-defined]
    rt.gates.register_applier(GateKind.BUDGET, apply)


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("build.plan", run_plan, version=1, pool="cpu", paid=False, Params=PlanParams, cacheable=False)
    registry.register_handler("build.finish", run_finish, version=1, pool="cpu", paid=False, Params=FinishParams, cacheable=False)
    registry.register_handler("tripo.model", guarded(run_tripo_model, "tripo"), version=1, pool="api", paid=True, provider="tripo", Params=ModelParams,
                              estimate=estimate_model, poll=guarded(poll_tripo_model, "tripo"), cacheable=False)
    registry.register_handler("slab.build", guarded(run_slab_build), version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("primitive.build", guarded(run_primitive_build), version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("mesh.carry", run_carry, version=1, pool="cpu", paid=False, Params=MeshStepParams, cacheable=False)
    meshsteps.register(rt)
    sched.register_job_factory(JobKind.BUILD, _build_steps)
    if rt is not None:
        wrap_budget_applier(rt)


_ = (Path, NotFound, kits)
