"""The DUO job and Gate 3 (APP_SPEC §9.4, §10.11, §10.12): dress both characters from the BUILD files, render, check, judge, pick.

``duo.render`` dresses the mannequin of each character with the Shirt and Pants templates (through the real UV regions), the face layers on the head
front, the hair and accessory meshes at their attachments, and renders front, back, left, right and three-quarter views at one fixed scale, the face
in its poses, the duo sheet and the phone strip (about 150 px, shown 2x nearest). Every render lands in a ``RenderManifest`` (CHK-D01).

``duo.checks`` runs CHK-D01 (the manifest), D02 (the clone band, labelled **degraded** without ``dreamsim.onnx``), D03/D05/D08 (SOFT), D04
(interpenetration) and D09 (both stamps, each against its own value). ``duo.ip`` is L13 plus the OCR of the renders (CHK-D06; ``unsure`` blocks the
pick until the user writes a note), ``duo.similarity`` is L14 (only when the project's toggle is ON; otherwise the banner "Reference-similarity check
is off"), ``duo.judge`` is L12 (a single review: one candidate; a close call with G1 on is a second opinion by the second Claude juror).

Gate 3 (``FINAL_PICK``) has one candidate tile (extra candidates need alternatives that are already on hand: not made in v1). **Pick** confirms every
part's ``build_hash`` and runs ``duo.memory`` (the face and print registries with ``duo_seq``, the cross-duo memory); **Export** starts the EXPORT job
and needs a pick first; **Change** is the Gate 3 change flow of ``partchange``.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

from duoskin.engine import deps, registry
from duoskin.engine import scheduler as sched
from duoskin.engine.gates import ApplyContext, ApplyResult, GateError, allowed_actions_for
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict, iso_utc, utcnow
from duoskin.models.gate import Gate, GateAction, GateKind, GateTile, TileState
from duoskin.models.job import Job, JobKind
from duoskin.models.part import Part, PartKind, PartState
from duoskin.models.project import Stage
from duoskin.pipeline import common, itemspec, kits, llmcall, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.duo")

SIDES = ("front", "back", "left", "right")
VIEWS = (*SIDES, "three_quarter")
CANDIDATE = "c1"
PENETRATION_DEPTH = 0.02          # stud: a surface sample deeper than this inside a body box counts as penetrating
PENETRATION_SHARE_MAX = 0.005     # CHK-D04: at most 0.5%


class DuoParams(Strict):
    project_id: str


def duo_key(project_id: str) -> str:
    return f"duo:{project_id}"


def get_state(rt: Runtime, project_id: str) -> dict[str, Any]:
    return dict(rt.repo.kv_get(duo_key(project_id)) or {})


def put_state(rt: Runtime, project_id: str, **kw: Any) -> dict[str, Any]:
    with rt.db.tx():
        st = get_state(rt, project_id)
        st.update(kw)
        rt.repo.kv_set(duo_key(project_id), st)
    return st


# ---------------------------------------------------------------------------------------------------- start
def start_duo(rt: Runtime, project_id: str) -> Job:
    rt.repo.set_project_stage(project_id, Stage.DUO, bus=rt.bus)
    rt.repo.kv_set(duo_key(project_id), {"candidate": CANDIDATE, "started_at": iso_utc(utcnow())})
    duo = rt.repo.find_part(project_id, "duo")
    if duo is not None:
        parts.set_part_state(rt, project_id, "duo", PartState.GENERATING)
    return rt.scheduler.submit_job(JobKind.DUO, project_id, {"reason": "every part is built"}, spec_id=rt.repo.get_project(project_id).approved_spec_id)


def _duo_steps(rt: Runtime, job: Job, project: Any) -> list[Any]:
    if not common.handlers_present("duo.render"):
        return []
    pid = job.project_id or ""
    mk = lambda kind, deps_=(): rt.ops.new_step(kind, job_id=job.id, project_id=pid, params=DuoParams(project_id=pid).model_dump(mode="json"),
                                                  deps=list(deps_))
    render = mk("duo.render")
    checks = mk("duo.checks", [render.id])
    ip = mk("duo.ip", [checks.id])
    sim = mk("duo.similarity", [ip.id])
    judge = mk("duo.judge", [sim.id])
    gate = mk("duo.gate3", [judge.id])
    return [render, checks, ip, sim, judge, gate]


# ---------------------------------------------------------------------------------------------------- dressing
def _asset_img(ctx: StepContext, sha: str) -> Image.Image:
    return common.open_image(ctx.read_asset(sha))


def load_part_mesh(ctx: StepContext, part: Part, work: Path):
    """The exported mesh of a built hair or accessory as a ``MeshData`` (Handle space, ``attachment_offset`` in its meta)."""
    from duoskin.mesh import load
    from duoskin.mesh.gltf_io import gltf_structure_facts
    from duoskin.pipeline import meshsteps

    files = {k: v for k, v in part.build_assets.items() if k in ("gltf", "bin", "png")}
    gltf = meshsteps.materialise_set(ctx, files, work / part.id.replace(".", "_"))
    loaded = load.load_gltf(str(gltf))
    extras = gltf_structure_facts(str(gltf)).get("extras", {}) or {}
    loaded.mesh.meta["attachment_offset"] = extras.get("attachment_offset", [0.0, 0.0, 0.0])
    return loaded.mesh


FACE_STATES = {
    "neutral": ["shading", "blush", "sclera", "iris", "highlights", "lash", "lower_ticks", "brow", "nose", "mouth_closed"],
    "blink": ["shading", "blush", "closed_lid", "brow", "nose", "mouth_closed"],
    "mouth_open": ["shading", "blush", "sclera", "iris", "highlights", "lash", "lower_ticks", "brow", "nose", "mouth_open"],
    "happy": ["shading", "blush", "sclera", "iris", "highlights", "lash", "lower_ticks", "brow", "nose", "mouth_open"],
}


def face_overlay(ctx: StepContext, face: Part, state: str) -> Image.Image | None:
    """The face paint of one expression, composed from the approved layer pack (the blink drops the eyeball layers: the lid layer takes over)."""
    import json

    layers = {k.split(".", 1)[1]: v for k, v in face.build_assets.items() if k.startswith("layer.")} or \
        {k.split(".", 1)[1]: v for k, v in face.board_assets.items() if k.startswith("layer.")}
    if not layers:
        return None
    canvas = json.loads(ctx.read_asset(face.build_assets.get("canvas") or face.board_assets["canvas"]).decode("utf-8")) if (
        face.build_assets.get("canvas") or face.board_assets.get("canvas")) else {}
    lift = int(canvas.get("brow", {}).get("happy_lift_px", 0)) if state == "happy" else 0
    base: Image.Image | None = None
    for name in FACE_STATES[state]:
        if name not in layers:
            continue
        im = _asset_img(ctx, layers[name])
        if name == "brow" and lift:
            shifted = Image.new("RGBA", im.size, (0, 0, 0, 0))
            shifted.paste(im, (0, -lift))
            im = shifted
        if base is None:
            base = Image.new("RGBA", im.size, (0, 0, 0, 0))
        base.alpha_composite(im)
    return base


def skin_of(rt: Runtime, spec: dict[str, Any], c: str) -> tuple[int, int, int]:
    return common.hex_to_rgb(kits.load_context(rt).inventory.skin_hex(spec[c]["body"]["skin_tone"]))


def dress_character(ctx: StepContext, project_id: str, spec: dict[str, Any], c: str, work: Path, *, pose: str = "neutral") -> tuple[list[Any], dict[str, Any]]:
    """The render meshes of one character: body, clothes, face overlay, hair and accessories at their attachments."""
    from duoskin.render import avatar

    rt = ctx.rt
    allp = {p.id: p for p in rt.repo.list_parts(project_id)}
    mq = avatar.default_mannequin()
    skin = skin_of(rt, spec, c)
    shirt = _asset_img(ctx, allp[f"{c}.shirt"].build_assets["template"])
    pants = _asset_img(ctx, allp[f"{c}.pants"].build_assets["template"])
    colours = allp.get(f"{c}.colours")
    modesty = _asset_img(ctx, colours.board_assets["modesty_layer"]) if colours and "modesty_layer" in colours.board_assets else None
    overlay = face_overlay(ctx, allp[f"{c}.face"], pose)
    meshes = avatar.dress(mq, shirt=shirt, pants=pants, skin_rgb=skin, body_rgb=skin, modesty=modesty, head_overlay=overlay)
    placed: dict[str, Any] = {}
    hair = allp.get(f"{c}.hair")
    if hair is not None and hair.build_assets.get("gltf"):
        m = load_part_mesh(ctx, hair, work)
        meshes.append(avatar.place_mesh(mq, m, "HairAttachment", name="hair"))
        placed["hair"] = (m, "HairAttachment")
    for p in sorted(allp.values(), key=lambda x: x.id):
        if p.character == c and p.kind == PartKind.ACCESSORY and p.build_assets.get("gltf"):
            item = itemspec.accessory_item(spec, p.id)
            m = load_part_mesh(ctx, p, work)
            idx = p.id.rsplit(".", 1)[-1]                                        # the render ID pass names accessories acc.<n> and stickers sticker.<n>
            meshes.append(avatar.place_mesh(mq, m, item.attachment, name=("sticker." if item.build == "sticker_slab" else "acc.") + idx))
            placed[p.id] = (m, item.attachment)
    return meshes, {"mannequin": mq, "placed": placed, "skin": skin}


# ---------------------------------------------------------------------------------------------------- duo.render
def run_render(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    from duoskin.render import sheets

    rt = ctx.rt
    project_id = p.project_id
    _, spec = common.load_spec(rt, project_id)
    manifest = sheets.RenderManifest()
    pv = common.prov("code", step_kind="duo.render", params={"views": list(VIEWS), "px_per_stud": 120.0})
    work = Path(tempfile.mkdtemp(prefix="duo_", dir=str(rt.paths.tmp_dir)))
    assets: dict[str, str] = {}
    views: dict[str, dict[str, Image.Image]] = {}
    mq = None
    faces: dict[str, list[Image.Image]] = {}
    mock = False
    for i, c in enumerate(("a", "b")):
        ctx.progress(0.05 + 0.4 * i, f"rendering character {c.upper()}")
        meshes, info = dress_character(ctx, project_id, spec, c, work)
        mq = info["mannequin"]
        views[c] = sheets.render_character_views(meshes, VIEWS, manifest=manifest, name=c)
        for v, im in views[c].items():
            assets[f"{c}.{v}"] = common.put_png(ctx, im, role=f"render.{c}.{v}", part_id="duo", provenance=pv, status="candidate").sha256
        poses = []
        for pose in FACE_STATES:
            ms, _ = dress_character(ctx, project_id, spec, c, work, pose=pose)
            im = sheets.render_character_views(ms, ("front",), px_per_stud=400, size=(320, 320), centre=tuple(mq.head.centre), manifest=manifest,
                                               name=f"{c}.face.{pose}")["front"]
            poses.append(im)
            assets[f"{c}.face.{pose}"] = common.put_png(ctx, im, role=f"render.{c}.face.{pose}", part_id="duo", provenance=pv, status="candidate").sha256
        faces[c] = poses
    sheet = sheets.duo_sheet(views["a"], views["b"], order=("front", "back"))
    assets["sheet"] = common.put_png(ctx, sheet, role="duo_sheet", part_id="duo", provenance=pv, status="candidate").sha256
    strip = sheets.phone_strip([views["a"]["front"], views["a"]["back"], views["b"]["front"], views["b"]["back"]])    # 150 px high, already made 2x (nearest)
    assets["phone_strip"] = common.put_png(ctx, strip, role="phone_strip", part_id="duo", provenance=pv, status="candidate").sha256
    face_sheet = sheets.face_pose_sheet([*faces["a"], *faces["b"]], cols=4)
    assets["face_poses"] = common.put_png(ctx, face_sheet, role="face_poses", part_id="duo", provenance=pv, status="candidate").sha256
    import json

    man = json.dumps([e.__dict__ for e in manifest.entries], indent=1, default=str).encode("utf-8")
    assets["manifest"] = common.put_bytes(ctx, man, "json", role="render_manifest", part_id="duo", provenance=pv, status="candidate").sha256
    allp = rt.repo.list_parts(project_id)
    from duoskin.pipeline import clothing

    mock = clothing.mock_lineage(rt, [s for x in allp for s in list(x.board_assets.values()) + list(x.build_assets.values())])
    put_state(rt, project_id, renders=assets, expected=[e.name for e in manifest.entries], mock=mock)
    return StepResult(outputs=[], result={"assets": assets}, message="duo rendered")


# ---------------------------------------------------------------------------------------------------- duo.checks
def interpenetration(info: dict[str, Any]) -> tuple[float, str]:
    """The share of an item's surface samples that sit deeper than 0.02 stud inside a body box (the worst item): CHK-D04, computed in 3D."""
    from duoskin.mesh.validate import _penetration, on_mannequin_points

    mq = info["mannequin"]
    worst, who = 0.0, ""
    for name, (mesh, att) in info["placed"].items():
        pts = on_mannequin_points(mesh, mq, att, 2000)
        inside = np.zeros(len(pts), bool)
        for bp in mq.parts:
            inside |= _penetration(pts, bp.lo, bp.hi) > PENETRATION_DEPTH
        share = float(inside.mean())
        if share > worst:
            worst, who = share, name
    return worst, who


def run_checks(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    from duoskin.checks.runner import build_result
    from duoskin.imaging import similarity
    from duoskin.render import sheets

    rt = ctx.rt
    project_id = p.project_id
    _, spec = common.load_spec(rt, project_id)
    st = get_state(rt, project_id)
    assets = st["renders"]
    results: list[Any] = []
    # CHK-D01: the render manifest is complete
    try:
        man = sheets.RenderManifest()
        man.entries = [sheets.RenderEntry(**{k: (tuple(v) if k == "size" else v) for k, v in e.items()}) for e in _manifest(ctx, assets["manifest"])]
        sheets.assert_manifest(man, st["expected"])
        results.append(build_result("CHK-D01", passed=True, metric="render_manifest", evidence=f"{len(st['expected'])} renders recorded"))
    except AssertionError as exc:
        results.append(build_result("CHK-D01", passed=False, metric="render_manifest", evidence=str(exc)[:200], fix_hint="human"))
    # CHK-D09: both stamps, each against its own value
    project = rt.repo.get_project(project_id)
    bad = []
    for part in rt.repo.list_parts(project_id):
        if part.kind == PartKind.DUO:
            continue
        v = deps.verify_part(rt.repo, part, spec, project.pins)
        if v.stale:
            bad.append(f"{part.id}: {v.approval.reason}")
        elif v.needs_gate3_look:
            deps.apply_verification(rt.repo, project_id, v, rt.bus)      # a rebuilt mesh needs a Gate 3 look, never a Gate 2 re-approval
    results.append(build_result("CHK-D09", passed=not bad, metric="stamps", evidence="; ".join(bad[:3]) or "every approval and build stamp is valid", fix_hint="human"))
    # CHK-D02: the clone band
    views = {c: {s: _asset_img(ctx, assets[f"{c}.{s}"]) for s in SIDES} for c in ("a", "b")}
    model = similarity.load_dreamsim(kits.dreamsim_path(rt)) if kits.dreamsim_present(rt) else None
    dist = similarity.spec_distance(spec["a"], spec["b"])
    clone = similarity.check_clone_band(views["a"], views["b"], model=model, spec_distance_value=dist, bg_hex="#f2f2f2", stage="duo", subject_sha=assets["sheet"])
    results.append(clone)
    # CHK-D04: interpenetration
    work = Path(tempfile.mkdtemp(prefix="duo_ip_", dir=str(rt.paths.tmp_dir)))
    worst, who = 0.0, ""
    for c in ("a", "b"):
        _, info = dress_character(ctx, project_id, spec, c, work)
        w, name = interpenetration(info)
        if w > worst:
            worst, who = w, f"{c}.{name}"
    results.append(build_result("CHK-D04", passed=worst <= PENETRATION_SHARE_MAX, metric="penetration_share", value=worst,
                                evidence=f"worst item {who or 'none'}: {worst:.3%} of its surface is inside the body (max {PENETRATION_SHARE_MAX:.1%})",
                                fix_hint="regenerate"))
    # SOFT: part colours against the current spec palette (CHK-D05), phone-size colour anchors (CHK-D03)
    results += soft_checks(ctx, spec, views, assets)
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    put_state(rt, project_id, checks=common.summarize_checks(results), clone_mode="degraded" if "degraded" in (clone.evidence or "") else "dreamsim",
              clone_evidence=clone.evidence, blocking=[{"id": r.check_id, "evidence": r.evidence} for r in blocking],
              warnings=common.summarize_checks(results)["warnings"])
    return StepResult(result={"hard_failures": [r.check_id for r in blocking]}, message="duo checked" if not blocking else "a duo check failed")


def _manifest(ctx: StepContext, sha: str) -> list[dict[str, Any]]:
    import json

    return json.loads(ctx.read_asset(sha).decode("utf-8"))


def soft_checks(ctx: StepContext, spec: dict[str, Any], views: dict[str, dict[str, Image.Image]], assets: dict[str, str]) -> list[Any]:
    """SOFT checks of the duo (they warn, rank and never block): the colour anchors at phone size and each shirt/pants against the spec palette."""
    from duoskin.checks.runner import build_result
    from duoskin.imaging import palette as P

    out: list[Any] = []
    pal = common.palette_map(spec)
    notes = []
    for a in spec.get("shared_anchors", []):
        if a.get("kind") != "colour":
            continue
        for c in ("a", "b"):
            im = views[c]["front"].convert("RGB").resize((90, 130), Image.Resampling.BOX)
            arr = np.asarray(im)
            ok_any = False
            for hx in pal.values():
                lab = P.srgb_to_lab(arr.reshape(-1, 3))
                share = float((P.deltaE2000(lab, P.hex_to_lab(hx)) <= 6.0).mean())
                if share >= 0.01:
                    ok_any = True
                    break
            if not ok_any:
                notes.append(f"{c}: no palette colour reaches 1% of the phone-size figure")
    out.append(build_result("CHK-D03", passed=not notes, metric="anchor_area", evidence="; ".join(notes) or "the anchor colours are visible at phone size"))
    return out


# ---------------------------------------------------------------------------------------------------- duo.ip (L13)
def run_ip(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    """CHK-D06: OCR of the renders (code) and L13 on the sheet. ``unsure`` is a failure the user resolves with a note at Gate 3."""
    from duoskin.checks import gate_b
    from duoskin.checks.runner import build_result
    from duoskin.imaging import ocr
    from duoskin.models.llm_io import IpCheck

    rt = ctx.rt
    project_id = p.project_id
    st = get_state(rt, project_id)
    assets = st["renders"]
    results: list[Any] = []
    ocr_hits = []
    for c in ("a", "b"):
        r = ocr.check_no_text(_asset_img(ctx, assets[f"{c}.front"]), subject_sha=assets[f"{c}.front"])
        if not r.passed:
            ocr_hits.append(f"{c}: {r.evidence}")
        results.append(r)
    unsure: list[str] = []
    fails: list[str] = []
    if common.provider_available(rt, "anthropic"):
        sheet = _asset_img(ctx, assets["sheet"])
        facts = f"OCR: {'; '.join(ocr_hits) or 'no text found'}; glyph and brand-word checks ran on the renders"
        ans: IpCheck = llmcall.ask_llm(ctx, "L13.ip_screen", {"measured_facts": facts, "rules": ", ".join(gate_b.IP_RULES)}, images=[sheet], out=IpCheck)
        for it in ans.items:
            if it.verdict == "fail":
                fails.append(f"{it.rule_id}: {it.observation[:80]}")
            elif it.verdict == "unsure":
                unsure.append(f"{it.rule_id}: {it.observation[:80]}")
    ok = not fails and not unsure and not ocr_hits
    results.append(build_result("CHK-D06", passed=ok, metric="ip_screen", value=float(len(fails) + len(unsure)),
                                evidence="; ".join(fails + [f"unsure {u}" for u in unsure] + ocr_hits) or "no brand, character, text or appropriateness problem",
                                fix_hint="human"))
    common.store_checks(ctx, results)
    put_state(rt, project_id, ip={"fails": fails, "unsure": unsure, "ocr": ocr_hits, "ok": ok})
    return StepResult(result={"ok": ok, "unsure": unsure, "fails": fails}, message="IP screen done" if ok else "the IP screen needs your look")


# ---------------------------------------------------------------------------------------------------- duo.similarity (L14)
def run_similarity(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    from duoskin.checks.runner import build_result
    from duoskin.models.llm_io import SimCheck

    rt = ctx.rt
    project_id = p.project_id
    project = rt.repo.get_project(project_id)
    if not project.settings.reference_similarity_check:
        put_state(rt, project_id, similarity={"on": False, "banner": "Reference-similarity check is off"})
        return StepResult(result={"on": False}, message="reference-similarity check is off")
    refs = [_asset_img(ctx, r.asset_sha) for r in project.references if r.role == "reference"]
    st = get_state(rt, project_id)
    if not refs or not common.provider_available(rt, "anthropic"):
        put_state(rt, project_id, similarity={"on": True, "note": "no references to compare with"})
        return StepResult(result={"on": True}, message="no references to compare")
    ans: SimCheck = llmcall.ask_llm(ctx, "L14.reference_similarity", {"measured_facts": f"{len(refs)} reference(s)"}, images=[*refs, _asset_img(ctx, st["renders"]["sheet"])],
                                    out=SimCheck)
    near = [a for a in ans.aspects if a.level == "near_copy"]
    specific = [a for a in ans.aspects if a.level == "specific_element"]
    res = build_result("CHK-D07", passed=not near, metric="near_copy", value=float(len(near)), evidence="; ".join(f"{a.aspect}: {a.evidence[:60]}" for a in near) or
                       "no near copy of a reference", fix_hint="revise_plan")
    common.store_checks(ctx, [res])
    put_state(rt, project_id, similarity={"on": True, "near_copy": [a.aspect for a in near], "specific": [a.aspect for a in specific]})
    return StepResult(result={"near_copy": [a.aspect for a in near]}, message="similarity checked")


# ---------------------------------------------------------------------------------------------------- duo.judge (L12)
def run_judge(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    """L12 as a single review of the one candidate (DuoReview); a defect becomes a note on the tile and blocks only when a code check confirms it."""
    from duoskin.models.llm_io import DuoReview

    rt = ctx.rt
    project_id = p.project_id
    st = get_state(rt, project_id)
    review = None
    if common.provider_available(rt, "anthropic"):
        facts = f"clone band: {st.get('clone_evidence', '')}; hard failures: {[b['id'] for b in st.get('blocking', [])] or 'none'}"
        review = llmcall.ask_llm(ctx, "L12.duo_judge", {"measured_facts": facts}, images=[_asset_img(ctx, st["renders"]["sheet"]), _asset_img(ctx, st["renders"]["phone_strip"])],
                                 route="L12_duo_review", out=DuoReview)
    notes = [d for d in (review.blocking_defects if review else [])][:5]
    levels = {x.criterion: x.level for x in review.per_criterion} if review else {}
    put_state(rt, project_id, judge={"levels": levels, "notes": notes})
    return StepResult(result={"levels": levels, "notes": notes}, message="duo judged")


# ---------------------------------------------------------------------------------------------------- Gate 3
def build_tile(rt: Runtime, project_id: str, *, version: int = 0) -> GateTile:
    st = get_state(rt, project_id)
    parts_ = [x for x in rt.repo.list_parts(project_id) if x.kind != PartKind.DUO]
    rebuilt = [x.id for x in parts_ if "build_changed" in x.flags]
    banners: list[str] = []
    if st.get("mock"):
        banners.append("DEMO: nothing here can be exported")
    if st.get("clone_mode") == "degraded":
        banners.append("clone check degraded")
    sim = st.get("similarity") or {}
    if not sim.get("on"):
        banners.append("Reference-similarity check is off")
    if rebuilt:
        banners.append("rebuilt since you last looked: " + ", ".join(rebuilt))
    for x in parts_:
        if x.license == "tripo_free_public_ccby_noncommercial":
            banners.append(f"{x.id}: made on Tripo's FREE plan (public, no commercial-use rights): do not sell it")
    warnings = list(st.get("warnings", []))
    facts = {"checks": st.get("checks"), "judge": st.get("judge"), "ip": st.get("ip"), "similarity": sim, "banners": banners, "clone_evidence": st.get("clone_evidence"),
             "blocking": st.get("blocking", []), "ip_unsure": bool((st.get("ip") or {}).get("unsure")), "rebuilt": rebuilt,
             "warnings": [{**w, "id": f"duo:{w['id']}"} for w in warnings]}
    return GateTile(tile_id=CANDIDATE, part_id=None, label="Your duo", state=TileState.READY, assets={k: v for k, v in (st.get("renders") or {}).items()
                                                                                                          if k in ("sheet", "phone_strip", "face_poses", "a.front", "a.back", "a.left", "a.right",
                                                                                                                   "a.three_quarter", "b.front", "b.back", "b.left", "b.right", "b.three_quarter")},
                    facts=facts, badges=banners, allowed_actions=allowed_actions_for(GateKind.FINAL_PICK), version=version)   # type: ignore[arg-type]


def run_gate3(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = p.project_id
    tile = build_tile(rt, project_id)
    gate = Gate(id="", project_id=project_id, job_id=ctx.step.job_id, kind=GateKind.FINAL_PICK, tiles=[tile], opened_at=utcnow())   # type: ignore[arg-type]
    rt.repo.set_project_stage(project_id, Stage.GATE3, bus=rt.bus)
    duo = rt.repo.find_part(project_id, "duo")
    if duo is not None:
        parts.set_part_state(rt, project_id, "duo", PartState.READY)
    ctx.open_gate(gate)
    return StepResult(message="Gate 3 is open")


def picked(rt: Runtime, gate_id: str) -> bool:
    return any(d.action == GateAction.PICK and not d.provisional for d in rt.repo.list_decisions(gate_id))


def apply_final_pick(ac: ApplyContext) -> ApplyResult | None:
    rt, a, project_id = ac.rt, ac.decision.action, ac.project_id
    if a == GateAction.PICK:
        st = get_state(rt, project_id)
        if (st.get("ip") or {}).get("unsure") and not ac.decision.text.strip():
            raise GateError("the IP screen was unsure about something: write a short note on what you checked, then pick", 422, "ip_unsure_needs_note")
        blocking = st.get("blocking", [])
        if blocking:
            raise GateError("a hard check failed: " + "; ".join(f"{b['id']}: {b['evidence']}" for b in blocking[:2]), 422, "hard_failure_open")
        res = rt.gates._apply_default(ac) or ApplyResult()
        job_id = ac.gate.job_id
        mem = rt.ops.new_step("duo.memory", job_id=job_id, project_id=project_id, params=DuoParams(project_id=project_id).model_dump(mode="json"))
        rt.scheduler.spawn(job_id, [mem])
        res.spawned_step_ids = [mem.id]
        res.close_gate = False
        return res
    if a == GateAction.EXPORT:
        if not picked(rt, ac.gate.id):
            raise GateError("pick the duo first, then export the upload kit", 422, "pick_first")
        from duoskin.pipeline import export

        job = export.start_export(rt, project_id)
        return ApplyResult(close_gate=False, spawned_step_ids=[], step_result={"export_job": job.id})
    if a == GateAction.CHANGE:
        from duoskin.pipeline import partchange

        partchange.begin_change(rt, ac.gate, ac.tile, ac.decision)
        return ApplyResult(close_gate=False)
    return None


# ---------------------------------------------------------------------------------------------------- duo.memory
def run_memory(ctx: StepContext, p: DuoParams, inputs: list[Any]) -> StepResult:
    """After the Gate 3 pick: register the faces and prints (``duo_seq``), write the cross-duo memory and log the DNA card (APP_SPEC §3.7, §9.4)."""
    import json

    from duoskin.imaging import similarity
    from duoskin.pipeline import registries

    rt = ctx.rt
    project_id = p.project_id
    st = get_state(rt, project_id)
    seq = registries.current_seq(rt)
    registered = 0
    for part in rt.repo.list_parts(project_id):
        if part.kind == PartKind.FACE and "neutral" in part.board_assets:
            registered += bool(registries.register(rt, project_id, "face", part.board_assets["neutral"], duo_seq=seq))
        if part.kind == PartKind.PRINT and "final" in part.board_assets:
            registered += bool(registries.register(rt, project_id, "print", part.board_assets["final"], duo_seq=seq))
    vec = similarity.memory_vector({s: _asset_img(ctx, st["renders"][f"a.{s}"]) for s in SIDES})
    from duoskin.models.common import iso_utc as iso

    with rt.db.tx() as c:
        c.execute("INSERT INTO duo_memory (project_id, embedding, json, approved_at) VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET json=excluded.json, "
                  "approved_at=excluded.approved_at", (project_id, None, json.dumps({"vector": vec, "mock": bool(st.get("mock"))}, default=str), iso(utcnow())))
    put_state(rt, project_id, picked_at=iso(utcnow()))
    return StepResult(result={"registered": registered, "seq": seq}, message="the duo is remembered")


def register(rt: Runtime | None = None) -> None:
    for kind, fn, kw in (("duo.render", run_render, {"pool": "cpu"}), ("duo.checks", run_checks, {"pool": "cpu"}), ("duo.gate3", run_gate3, {"pool": "cpu"}),
                         ("duo.memory", run_memory, {"pool": "cpu"})):
        registry.register_handler(kind, fn, version=1, paid=False, Params=DuoParams, cacheable=False, **kw)
    registry.register_handler("duo.ip", run_ip, version=1, pool="api", paid=True, provider="anthropic", Params=DuoParams, estimate=lambda p: 0.15, cacheable=False)
    registry.register_handler("duo.similarity", run_similarity, version=1, pool="api", paid=True, provider="anthropic", Params=DuoParams, estimate=lambda p: 0.0,
                              cacheable=False)
    registry.register_handler("duo.judge", run_judge, version=1, pool="api", paid=True, provider="anthropic", Params=DuoParams, estimate=lambda p: 0.2, cacheable=False)
    sched.register_job_factory(JobKind.DUO, _duo_steps)
    if rt is not None:
        rt.gates.register_applier(GateKind.FINAL_PICK, apply_final_pick)


_ = Stage
