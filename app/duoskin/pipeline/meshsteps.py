"""The mesh gate as steps (APP_SPEC §10.9, §11.4): ``mesh.import`` -> ``mesh.repair`` (or ``hair.register``) -> ``mesh.validate`` -> ``mesh.judge``.

The same chain runs for a Tripo model (API mode), a file the user made (manual mode, polish returns, any import) and, with fewer steps, for the
code-built meshes (slabs, primitives, fitted kit hair). Every step takes its input from the CAS and the previous step's result, never from a
path that only exists in memory, so a restart between two steps loses nothing.

* ``mesh.import``: the safety rules of §11.4 (size cap, type from the magic bytes, safe unzip, ``extensionsRequired``), the licence flag, then the
  worker's ``import`` op (CHK-M01). The model file is kept as the asset ``mesh_source`` with its licence and origin in the provenance.
* ``mesh.repair``: the worker's ``repair`` op (merge, weld copy, decimate, fixes, texture, orientation, scale, export, validation, renders). For hair
  from Tripo or by hand the step is ``hair.register`` (the worker's ``register_hair``): find the grey cube head, fit the hair to the mannequin head,
  cut the head cavity, check CHK-M21. The exported ``.gltf`` + ``.bin`` + ``.png`` set, the ``.glb`` archive and the renders become assets.
* ``mesh.validate``: the exported files are opened again from the CAS and validated (the gate verdict of CHK-M01..M21).
* ``mesh.judge``: L11 on the render sheet against the approved views (3-vote majority before a failing verdict counts, CHK-P12); a pass stores the
  build assets and starts ``build.finish``, a failure goes to the next seed, the next route, or the user (``manual_mesh``).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duoskin.engine import registry
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict
from duoskin.pipeline import common, itemspec, kits, meshrun

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime
    from duoskin.mesh.types import MeshResult

log = logging.getLogger("duoskin.meshsteps")

VIEW_ROLES = ("front", "left", "back", "right")
GATE_FILES = ("gltf", "bin", "png", "glb_archive", "fbx")


class MeshStepParams(Strict):
    part_id: str
    from_step: str = ""
    source: dict[str, Any] = {}          # kind tripo|manual|kit|slab|primitive, seed, route, task_id, licence, pack_id, plan, task_link
    seed_index: int = 0
    nonce: str = ""
    mv_step: str = ""                    # the multiview step whose task id and views a next seed reuses
    model_sha: str = ""                  # mesh.import: the model file asset (a Tripo download or a user file)
    work_name: str = ""                  # mesh.import: the file name of an import (for the pack id)
    expect_pack_id: str = ""


# ---------------------------------------------------------------------------------------------------- helpers
def work_dir(ctx: StepContext, name: str = "") -> Path:
    d = ctx.rt.paths.tmp_dir / "mesh" / ctx.step.id / name if name else ctx.rt.paths.tmp_dir / "mesh" / ctx.step.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_asset(ctx: StepContext, sha: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ctx.read_asset(sha))
    return path


def forward_axis(rt: Runtime) -> str:
    ax = rt.effective_settings().three_d.studio_forward_axis
    return ax if ax in ("+Z", "-Z") else "+Z"


def blender_path(rt: Runtime) -> str:
    return rt.effective_settings().three_d.blender_path or ""


def approved_views(ctx: StepContext, part: Any, work: Path) -> dict[str, str]:
    """The Gate 2 views of the part as files (the orientation search and the judge compare against them)."""
    out: dict[str, str] = {}
    for v in VIEW_ROLES:
        sha = part.board_assets.get(f"view.{v}")
        if sha:
            out[v] = str(write_asset(ctx, sha, work / "views" / f"{v}.png"))
    return out


def item_of(rt: Runtime, project_id: str, part_id: str) -> itemspec.Item:
    _, spec = common.load_spec(rt, project_id)
    return itemspec.item_of(spec, part_id)


def store_files(ctx: StepContext, res: MeshResult, part_id: str, *, source: str, provenance_extra: dict[str, Any], input_shas: list[str]) -> dict[str, str]:
    """The worker's output files as assets (role -> sha). The ``.gltf``, ``.bin`` and ``.png`` keep the names the glTF refers to."""
    mock = bool(provenance_extra.get("mock_lineage"))
    notes = ["mock_lineage"] if mock else []
    out: dict[str, str] = {}
    for role, path in res.files.items():
        p = Path(path)
        if not p.is_file():
            continue
        ext = p.suffix.lstrip(".").lower() or "bin"
        pv = common.prov(source, step_kind=ctx.step.kind, input_shas=input_shas, notes=notes,
                         params={"role": role, "file_name": p.name, **{k: v for k, v in provenance_extra.items() if k != "mock_lineage"}})
        out[role] = common.put_bytes(ctx, p.read_bytes(), ext, role=f"mesh.{role}", part_id=part_id, provenance=pv, status="candidate").sha256
    return out


def slim_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The facts worth keeping in a step result (small, JSON-safe)."""
    keep = ("tris", "shells", "bbox_studs", "box_margins", "surface_area", "coplanar_frac", "centre_offset", "normals_out", "watertight", "texture_px",
            "orientation", "mirrored", "stem", "asset_id", "licence", "frame", "attachment_offset", "fbx_produced", "hair_register", "decimator")
    return {k: facts[k] for k in keep if k in facts}


def checks_summary(results: list[Any]) -> list[dict[str, Any]]:
    return [{"id": r.check_id, "kind": r.kind, "passed": r.passed, "status": r.status, "ev": (r.evidence or "")[:160]} for r in results]


def source_kind(rt: Runtime, source: dict[str, Any]) -> str:
    """The provenance source of a model file: ``mock`` for a mock Tripo, ``tripo`` for the API, ``user`` for a hand-made or Tripo-website file."""
    kind = source.get("kind", "tripo")
    if kind == "tripo":
        return "mock" if common.is_mock(rt, "tripo") else "tripo"
    if kind == "manual":
        return "tripo_manual" if source.get("licence", "").startswith("tripo") else "user"
    return "code"


# ---------------------------------------------------------------------------------------------------- mesh.import
def run_import(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    from duoskin.mesh.types import MeshJob
    from duoskin.pipeline import mesh_import

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    item = item_of(rt, project_id, p.part_id)
    work = work_dir(ctx)
    src = write_asset(ctx, p.model_sha, work / "in" / (p.work_name or "model.glb"))
    licence = str(p.source.get("licence") or ("tripo_api_private_commercial" if p.source.get("kind") == "tripo" else "unknown"))
    imp = mesh_import.import_file(src, work / "unpacked", licence=licence, expected_pack_id=p.expect_pack_id or None,
                                  blender_available=bool(kits.blender_present(rt)), task_link=str(p.source.get("task_link", "")),
                                  tripo_plan=str(p.source.get("plan", "")))
    results = []
    if p.source.get("kind") == "manual":
        results.append(mesh_import.check_assignment(imp, p.expect_pack_id or None))
    if not imp.ok or not imp.work_path:
        results.append(common.mk_result("CHK-M01", False, metric="load_contract", evidence=imp.message or "the file cannot be imported", fix_hint="human"))
        common.store_checks(ctx, results)
        return StepResult(result={"ok": False, "reason": imp.message or "the file cannot be imported", "issues": [i.code for i in imp.issues],
                                  "licence": licence}, message="import refused")
    job = MeshJob(op="import", input_path=imp.work_path, out_dir=str(work / "import"), asset_type=item.asset_type, attachment=item.attachment,   # type: ignore[arg-type]
                  target_studs=item.target_studs, tris_target=item.tris_target, texture_px=item.texture_px, asset_id=part.id,
                  licence=licence, blender_path=blender_path(rt))   # type: ignore[arg-type]
    res = meshrun.run_mesh_job(ctx, job, timeout_s=120)
    results += list(res.checks)
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    mock = common.is_mock(rt, "tripo") and p.source.get("kind") == "tripo"
    pv = common.prov(source_kind(rt, p.source), step_kind="mesh.import", input_shas=[p.model_sha],
                     notes=["mock_lineage"] if mock else [], license=licence if licence != "n/a" else "n/a",   # type: ignore[arg-type]
                     params={"original_name": imp.original_name, "format": imp.kind, "sha256": imp.sha256, "bytes": imp.size, "seed": p.source.get("seed"),
                             "task_id": p.source.get("task_id"), "route": p.source.get("route"), "plan": p.source.get("plan", ""),
                             "task_link": p.source.get("task_link", ""), "pack_id": imp.assigned_pack_id or p.expect_pack_id or ""})
    data = Path(imp.work_path).read_bytes()
    ext = Path(imp.work_path).suffix.lstrip(".").lower() or "glb"
    sha = common.put_bytes(ctx, data, ext, role="mesh_source", part_id=part.id, provenance=pv, status="candidate").sha256
    return StepResult(outputs=[sha], result={"ok": not blocking, "source_sha": sha, "ext": ext, "licence": licence, "facts": slim_facts(res.facts),
                                             "mock": mock, "reason": "; ".join(r.evidence for r in blocking[:2]), "banner": imp.banner or "",
                                             "checks": checks_summary(results)},
                      message="model imported" if not blocking else "the model cannot be imported")


# ---------------------------------------------------------------------------------------------------- mesh.repair / hair.register
def _prev(ctx: StepContext, step_id: str) -> dict[str, Any]:
    return dict(ctx.rt.repo.get_step(step_id).result) if step_id else {}


def run_repair(ctx: StepContext, p: MeshStepParams, inputs: list[Any], *, hair: bool = False) -> StepResult:
    from duoskin.mesh.types import MeshJob

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    prev = _prev(ctx, p.from_step)
    if not prev.get("ok"):
        return StepResult(result={"ok": False, "reason": prev.get("reason", "the import failed"), "skipped": True}, message="skipped: the import failed")
    part = rt.repo.get_part(project_id, p.part_id)
    item = item_of(rt, project_id, p.part_id)
    work = work_dir(ctx)
    src = write_asset(ctx, prev["source_sha"], work / "in" / f"model.{prev.get('ext', 'glb')}")
    views = approved_views(ctx, part, work)
    params: dict[str, Any] = {"stem": "hair" if item.is_hair else "acc", "want_fbx": bool(kits.blender_present(rt)), "render": True, "view_size": 384,
                              "scale_mode": "fit"}
    if item.is_hair:
        st = rt.repo.kv_get(f"hair:{project_id}:{part.id}") or {}
        if st.get("hair_only"):
            params["hair_mask_path"] = str(write_asset(ctx, st["hair_only"], work / "hair_only.png"))
        grey = _guide_rgb(rt, st.get("guide"))
        params["guide_rgb"] = [list(g) for g in grey]
    op = "register_hair" if item.is_hair else "repair"
    job = MeshJob(op=op, input_path=str(src), out_dir=str(work / "out"), asset_type=item.asset_type, attachment=item.attachment,   # type: ignore[arg-type]
                  target_studs=item.target_studs, tris_target=item.tris_target, texture_px=item.texture_px, approved_views=views,
                  mannequin="", forward_axis=forward_axis(rt), params=params, asset_id=part.id, licence=prev.get("licence", "unknown"),   # type: ignore[arg-type]
                  blender_path=blender_path(rt))
    ctx.progress(0.2, "repairing the model")
    res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt), timeout_s=300)
    common.store_checks(ctx, list(res.checks))
    files = store_files(ctx, res, part.id, source=source_kind(rt, p.source),
                        provenance_extra={"licence": prev.get("licence"), "mock_lineage": bool(prev.get("mock")), "seed": p.source.get("seed"),
                                          "task_id": p.source.get("task_id"), "attachment": item.attachment, "asset_type": item.asset_type,
                                          "target_studs": list(item.target_studs), "tris": res.facts.get("tris"), "texture_px": item.texture_px},
                        input_shas=[prev["source_sha"]])
    blocking = common.hard_failures(list(res.checks))
    mirrored = bool(res.facts.get("mirrored"))
    return StepResult(outputs=list(files.values()), result={"ok": res.ok and not blocking, "files": files, "facts": slim_facts(res.facts), "mirrored": mirrored,
                                                            "licence": prev.get("licence"), "mock": bool(prev.get("mock")),
                                                            "reason": "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]) or "; ".join(res.messages[:2]),
                                                            "messages": res.messages[:6], "degraded": res.degraded,
                                                            "failed": [r.check_id for r in blocking], "checks": checks_summary(list(res.checks))},
                      message="model repaired" if res.ok and not blocking else "the model needs another try")


def run_hair_register(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    return run_repair(ctx, p, inputs, hair=True)


def _guide_rgb(rt: Runtime, guide_sha: str | None) -> list[tuple[int, int, int]]:
    from duoskin.mesh.hair import GUIDE_GREY

    out = [GUIDE_GREY]
    if guide_sha:
        try:
            grey = rt.cas.get_asset(guide_sha).first_provenance.params.get("grey")
            if grey:
                rgb = common.hex_to_rgb(str(grey))
                if rgb not in out:
                    out.append(rgb)
        except Exception:  # noqa: BLE001, S110 - the default guide colour still applies
            pass
    return out


# ---------------------------------------------------------------------------------------------------- mesh.validate
def materialise_set(ctx: StepContext, files: dict[str, str], work: Path) -> Path:
    """The exported glTF set back on disk under the file names the glTF refers to (``stem.gltf``, ``stem.bin``, ``stem.png``)."""
    gltf_path = None
    for role in ("gltf", "bin", "png"):
        sha = files.get(role)
        if not sha:
            continue
        name = ctx.get_asset(sha).first_provenance.params.get("file_name") or f"model.{role}"
        write_asset(ctx, sha, work / name)
        if role == "gltf":
            gltf_path = work / name
    if gltf_path is None:
        raise StepFailure("the repaired model has no glTF file", kind="bad_request", billed="no")
    return gltf_path


KIT_NOT_APPLICABLE = {
    "CHK-M08": "kit hair is made by code in the attachment frame: it has no orientation to find",
    "CHK-M13": "kit hair is chosen by silhouette match (the kit-match score and its warning measure it); mesh.judge compares the renders with the views",
    "CHK-M21": "kit hair is registered by construction",
}


def kit_not_applicable(checks: list[Any]) -> list[Any]:
    """The checks that cannot hold for a kit hair (APP_SPEC 10.7: registered by construction, matched by the L9 kit score) become not applicable.
    CHK-M21's 80% visible front face also cannot hold together with CHK-M14's hair zone (the hair covers the upper face), so it is never asked of the kit."""
    from duoskin.checks.model import not_applicable

    return [not_applicable(c.check_id, "hard", KIT_NOT_APPLICABLE[c.check_id], fm_ids=list(c.fm_ids)) if c.check_id in KIT_NOT_APPLICABLE else c for c in checks]


def run_validate(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    """Open the exported files again from the CAS and validate them (the file gate the upload kit relies on)."""
    from duoskin.mesh.types import MeshJob

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    prev = _prev(ctx, p.from_step)
    if not prev.get("ok") or not prev.get("files"):
        return StepResult(result={**prev, "ok": False, "skipped": True}, message="skipped: nothing to validate")
    part = rt.repo.get_part(project_id, p.part_id)
    item = item_of(rt, project_id, p.part_id)
    work = work_dir(ctx)
    gltf = materialise_set(ctx, prev["files"], work / "set")
    views = approved_views(ctx, part, work)
    params = {"expect_slab": item.build == "sticker_slab", "expect_hair_register": item.is_hair and p.source.get("kind") != "kit", "roundtrip": False}
    job = MeshJob(op="validate", input_path=str(gltf), out_dir=str(work / "validate"), asset_type=item.asset_type, attachment=item.attachment,   # type: ignore[arg-type]
                  target_studs=item.target_studs, tris_target=item.tris_target, texture_px=item.texture_px, approved_views=views, params=params,
                  asset_id=part.id, forward_axis=forward_axis(rt), licence=prev.get("licence", "unknown"), blender_path=blender_path(rt))   # type: ignore[arg-type]
    res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt), timeout_s=180)
    if p.source.get("kind") == "kit":
        res.checks = kit_not_applicable(list(res.checks))
    common.store_checks(ctx, list(res.checks))
    blocking = common.hard_failures(list(res.checks))
    mirrored = bool(res.facts.get("mirrored") or prev.get("mirrored"))
    only_mirror = bool(blocking) and all(r.check_id == "CHK-M08" for r in blocking) and mirrored
    return StepResult(result={**{k: v for k, v in prev.items() if k not in ("checks",)}, "ok": res.ok and not blocking, "mirrored": mirrored,
                              "only_mirrored": only_mirror, "failed": [r.check_id for r in blocking],
                              "reason": "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]), "validate_facts": slim_facts(res.facts),
                              "checks": checks_summary(list(res.checks))},
                      message="the model passes the file gate" if not blocking else "the model fails the file gate")


# ---------------------------------------------------------------------------------------------------- mesh.judge
JUDGE_RULES = ("m3_front_matches", "m3_sides_match", "m3_no_fragments", "m3_texture_clean")


def run_judge(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    """L11 on the render sheet against the approved views; a verdict that fails must be confirmed by a 3-vote majority (CHK-P12)."""
    from duoskin.pipeline import build, llmcall

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    prev = _prev(ctx, p.from_step)
    part = rt.repo.get_part(project_id, p.part_id)
    ok = bool(prev.get("ok"))
    judge_results: list[Any] = []
    if ok and p.source.get("judge", True) and common.provider_available(rt, "anthropic"):
        sheet_sha = prev.get("files", {}).get("renders/judge_sheet")
        approved = [common.open_image(ctx.read_asset(part.board_assets[f"view.{v}"])) for v in VIEW_ROLES if f"view.{v}" in part.board_assets]
        if sheet_sha and approved:
            from duoskin.imaging import guides

            images = [common.open_image(ctx.read_asset(sheet_sha)),
                      guides.side_by_side([guides.vlm_image(a.convert("RGBA"), with_checkerboard=False).resize((256, 256)) for a in approved], gutter=12)]
            facts = f"mesh {prev.get('facts', {}).get('tris', '?')} triangles; renders first, approved views second"
            counter = llmcall.CallCounter()
            reqs = llmcall.rule_requests(list(JUDGE_RULES), {})
            first = llmcall.run_rules(ctx, reqs, images, measured_facts=facts, subject_sha=sheet_sha, votes=1, counter=counter)
            judge_results = list(first.results)
            if common.hard_failures(judge_results):                               # a failing verdict must survive a 3-vote majority
                again = llmcall.run_rules(ctx, reqs, images, measured_facts=facts, subject_sha=sheet_sha, votes=3, counter=counter)
                judge_results = list(again.results)
    common.store_checks(ctx, judge_results)
    judge_blocking = common.hard_failures(judge_results)
    ok = ok and not judge_blocking
    reason = prev.get("reason", "") if not prev.get("ok") else "; ".join(f"{r.check_id}: {r.evidence}" for r in judge_blocking[:3])
    result = {**{k: v for k, v in prev.items() if k not in ("checks",)}, "ok": ok, "judge_failed": [r.check_id for r in judge_blocking], "reason": reason}
    if ok:
        build.mesh_passed(ctx, part, p, prev)
        return StepResult(result=result, message="the model passed the judge")
    mirrored_only = bool(prev.get("only_mirrored")) and not judge_blocking
    nxt = build.mesh_failed(ctx, part, p, prev, reason=reason, mirrored_only=mirrored_only)
    return StepResult(result={**result, "next": nxt}, message=f"the model failed ({nxt})")


# ---------------------------------------------------------------------------------------------------- mesh.flip
def run_flip(ctx: StepContext, p: MeshStepParams, inputs: list[Any]) -> StepResult:
    """"Flip left/right (I checked)": mirror the exported mesh (negate X, reverse the winding), re-export and re-validate (APP_SPEC §10.9 step 8)."""
    from duoskin.mesh.types import MeshJob

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    prev = _prev(ctx, p.from_step)
    part = rt.repo.get_part(project_id, p.part_id)
    item = item_of(rt, project_id, p.part_id)
    work = work_dir(ctx)
    gltf = materialise_set(ctx, prev["files"], work / "set")
    views = approved_views(ctx, part, work)
    job = MeshJob(op="flip_lr", input_path=str(gltf), out_dir=str(work / "out"), asset_type=item.asset_type, attachment=item.attachment,   # type: ignore[arg-type]
                  target_studs=item.target_studs, tris_target=item.tris_target, texture_px=item.texture_px, approved_views=views,
                  params={"stem": "hair" if item.is_hair else "acc", "render": True, "want_fbx": bool(kits.blender_present(rt))}, asset_id=part.id,
                  forward_axis=forward_axis(rt), licence=prev.get("licence", "unknown"), blender_path=blender_path(rt))   # type: ignore[arg-type]
    res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt), timeout_s=300)
    common.store_checks(ctx, list(res.checks))
    files = store_files(ctx, res, part.id, source=source_kind(rt, p.source),
                        provenance_extra={"licence": prev.get("licence"), "mock_lineage": bool(prev.get("mock")), "flipped_lr": True, "attachment": item.attachment},
                        input_shas=list(prev["files"].values())[:1])
    blocking = common.hard_failures(list(res.checks))
    return StepResult(outputs=list(files.values()), result={"ok": res.ok and not blocking, "files": files, "facts": slim_facts(res.facts), "mirrored": False,
                                                            "licence": prev.get("licence"), "mock": bool(prev.get("mock")), "flipped": True,
                                                            "failed": [r.check_id for r in blocking],
                                                            "reason": "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]),
                                                            "checks": checks_summary(list(res.checks))},
                      message="flipped left/right")


def estimate_judge(p: Any) -> float:
    return llmcall_estimate()


def llmcall_estimate() -> float:
    from duoskin.pipeline import llmcall

    return llmcall.estimate_llm_usd("L11.asset_checker", calls=len(JUDGE_RULES), image_count=2) * 0.2


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("mesh.import", run_import, version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("mesh.repair", run_repair, version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("hair.register", run_hair_register, version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("mesh.validate", run_validate, version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("mesh.flip", run_flip, version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)
    registry.register_handler("mesh.judge", run_judge, version=1, pool="api", paid=True, provider="anthropic", Params=MeshStepParams,
                              estimate=estimate_judge, cacheable=False)


_ = shutil
