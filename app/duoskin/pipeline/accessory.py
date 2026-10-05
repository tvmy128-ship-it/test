"""The accessories lane (APP_SPEC §10.8): front view, views, the on-body scale render; slabs and primitives; their BUILD steps.

``build`` of the spec's accessory decides the lane:

* ``tripo``: **I5** draws the front view (1024 px, transparent; I5.accessory_frame while no concept crop exists, I5g as the guided rung), then
  ``multiview`` makes the four views (T1, T2 once, I10 flagged) and ``mv.check`` checks them; ``acc.board`` makes the tile with the
  **scale render** (``guide_scale_<attachment>``: the mannequin outline at a fixed stud scale, the accessory at its planned size and
  attachment, the Classic box; for hat, face and hair items the approved hair front is composited into the outline).
* ``sticker_slab`` / ``hair_clip_slab``: **I6** draws flat badge art; code adds the border (``slab_art.build_badge``) and ``acc.board`` shows the
  badge, the extruded slab preview (``mesh.slab.build_slab``) and the scale render. BUILD: ``slab.build``.
* ``code_primitive``: no AI. ``acc.board`` builds the primitive (``mesh.primitives``: ring, loop, strap, bead, charm) and renders it; BUILD:
  ``primitive.build``.

Board assets: ``front`` (art), ``view.front|left|back|right``, ``views_sheet``, ``scale_front``, ``scale_side``; slabs add ``badge`` and
``slab_preview``; primitives have ``preview`` instead of the views.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PIL import Image

from duoskin.engine import registry
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict
from duoskin.models.part import Part, PartKind
from duoskin.pipeline import assetloop as AL
from duoskin.pipeline import common, itemspec, kits, multiview, parts, prints

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.accessory")

HEAD_CATEGORIES = ("hat", "hair", "face")
PRIMITIVE_WORDS = (("ring", "ring"), ("strap", "strap"), ("belt", "strap"), ("band", "strap"), ("loop", "loop"), ("chain", "loop"), ("bead", "bead"),
                   ("charm", "charm"))
BORDER_PX = 20


class AccParams(Strict):
    part_id: str
    front_sha: str = ""
    nonce: str = ""
    mv_step: str = ""


# ---------------------------------------------------------------------------------------------------- helpers
def primitive_kind(item: itemspec.Item) -> str:
    text = f"{item.kind} {item.description}".lower()
    for word, kind in PRIMITIVE_WORDS:
        if word in text:
            return kind
    return "charm" if item.kind == "keychain_charm" else "bead"


def palette_rgb(spec: dict[str, Any], refs: tuple[str, ...]) -> dict[str, tuple[int, int, int]]:
    pal = common.palette_map(spec)
    hexes = [pal[r] for r in refs if r in pal] or ["#c8a050"]
    out = {"base": common.hex_to_rgb(hexes[0])}
    if len(hexes) > 1:
        out["accent"] = common.hex_to_rgb(hexes[1])
    if len(hexes) > 2:
        out["shade"] = common.hex_to_rgb(hexes[2])
    return out


def loop_palette(spec: dict[str, Any], item: itemspec.Item) -> list[str]:
    pal = common.palette_map(spec)
    own = [pal[r] for r in item.colour_refs if r in pal]
    extra = [c["hex"].lower() for c in spec.get("palette", []) if c.get("role") in ("line", "neutral_dark", "neutral_light")]
    return list(dict.fromkeys(own + extra))


def hair_front_for(rt: Runtime, project_id: str, character: str) -> tuple[Image.Image | None, str]:
    """``(the approved hair-only front, "ok")`` or ``(None, "hair_not_approved")``: a scale tile is judged against the hair of its character."""
    hair = rt.repo.find_part(project_id, f"{character}.hair")
    if hair is None or "hair_only" not in hair.board_assets or hair.state.value in ("planned", "generating", "stale"):
        return None, "hair_not_approved"
    img = common.open_image(rt.cas.get(hair.board_assets["hair_only"]))
    return img, "ok" if hair.approval is not None else "hair_not_approved"


# ---------------------------------------------------------------------------------------------------- the lane
def acc_loop(rt: Runtime, project_id: str, spec: dict[str, Any], part: Part, item: itemspec.Item, *, nonce: str) -> AL.AssetLoopSpec:
    from duoskin.imaging import guides

    badge = item.build == "sticker_slab"
    idx = common.split_part_id(part.id).index or 0
    refs = common.concept_refs(rt, project_id, spec, part.id)
    placeholder = refs.placeholder_crop
    template = ("I6.badge_frame" if placeholder else "I6.badge_art") if badge else ("I5.accessory_frame" if placeholder else "I5.accessory_front")
    ladder = ("badge_frame" if placeholder else "badge") if badge else ("accessory_frame" if placeholder else "accessory")
    frame = guides.guide_frame("square", margin=guides.FRAME_MARGIN_I5)
    fsha = rt.cas.put(frame.image_png(), "png", prov=common.prov("code", params={"guide": frame.guide_id})).sha256
    fmask = rt.cas.put(frame.mask_png(), "png", prov=common.prov("code", params={"guide": frame.guide_id, "mask": True})).sha256
    box = None
    overrides: dict[str, AL.TechOverride] = {}
    if not badge:
        from duoskin.roblox import limits

        box_studs = limits.box_for(item.asset_type, item.attachment).size
        box = guides.guide_acc_box((item.target_studs[0], item.target_studs[1]), (box_studs[0], box_studs[1]))
        bsha = rt.cas.put(box.image_png(), "png", prov=common.prov("code", params={"guide": box.guide_id})).sha256
        bmask = rt.cas.put(box.mask_png(), "png", prov=common.prov("code", params={"guide": box.guide_id, "mask": True})).sha256
        overrides["I5g"] = AL.TechOverride(images=[i for i in (bsha, None if placeholder else refs.crop_sha, refs.style_sha) if i], mask=bmask,
                                           slots={"accessory": idx, "no_crop": placeholder})
    if placeholder:
        images, mask = [fsha, refs.style_sha], fmask
    else:
        images, mask = [refs.crop_sha, refs.style_sha], None
    gate_a, hard, soft = prints.loop_checks(template)
    return AL.AssetLoopSpec(
        part_id=part.id, character=part.character, role="badge" if badge else "acc_front", template_id=template, slots={"accessory": idx},   # type: ignore[arg-type]
        images=[i for i in images if i], mask=mask, overrides=overrides, n_drafts=4, finalize=True, gate_a=gate_a, gate_b_hard=hard, gate_b_soft=soft,
        technique_ladder=ladder, then="badge" if badge else "acc_front", palette_hex=loop_palette(spec, item),
        expected={"components": [1, 3], "margin": 0.08, "placed_scale": 1.0, "allow_checker": False, "symmetric": False},
        reference_shas=[refs.crop_sha] if refs.crop_sha and not placeholder else [], nonce=nonce,
        kit_subset_sha=kits.load_context(rt).subset_sha(accessory=item.kind, build=item.build),
        available=sorted(common.available_providers(rt)))


class AccessoryLane(parts.Lane):
    kind = PartKind.ACCESSORY

    def waits_for(self, part: Part, all_parts: dict[str, Part]) -> list[str]:
        return list(part.part_deps)

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:
        rt = ctx.rt
        project_id = ctx.step.project_id or ""
        item = itemspec.accessory_item(spec, part.id)
        flag = {"sticker_slab": "slab", "code_primitive": "code_primitive"}.get(item.build)
        parts.set_part_state(rt, project_id, part.id, part.state, flags_add=[flag] if flag else [])
        if item.build == "code_primitive":
            st = rt.ops.new_step("acc.board", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id,
                                 params=AccParams(part_id=part.id, nonce=nonce).model_dump(mode="json"), nonce=nonce, priority=ctx.step.priority)
            ctx.spawn([st])
            return
        loop = acc_loop(rt, project_id, spec, part, item, nonce=nonce)
        AL.start_loop(rt, ctx.step.job_id, project_id, spec, loop, nonce=nonce, priority=ctx.step.priority)

    def recheck(self, ctx: StepContext, part: Part, spec: dict[str, Any]) -> None:
        """RECHECK (hair approved or changed, size, attachment): the scale tile is made again from the existing art; no new generation."""
        project_id = ctx.step.project_id or ""
        item = itemspec.accessory_item(spec, part.id)
        front = part.board_assets.get("front") or part.board_assets.get("badge") or ""
        mv_step = ""
        st = ctx.rt.ops.new_step("acc.board", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id,
                                 params=AccParams(part_id=part.id, front_sha=front, mv_step=mv_step).model_dump(mode="json"), priority=ctx.step.priority)
        ctx.spawn([st])
        _ = item


def acc_front_done(ctx: StepContext, loop: AL.AssetLoopSpec, res: AL.LoopResult) -> None:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, loop.part_id)
    if res.needs_human or not res.final_sha:
        parts.mark_needs_human(ctx, project_id, part.id, report=res.report, best=res.best_sha,
                               assets={"front": res.best_sha} if res.best_sha else None, results=res.results)
        return
    with rt.db.tx():
        rt.repo.kv_set(f"acc:{project_id}:{part.id}", {"front": res.final_sha, "alternatives": res.alternatives, "technique": res.technique,
                                                       "flags": res.flags, "results": common.summarize_checks(res.results)})
    _, spec = common.load_spec(rt, project_id)
    idx = common.split_part_id(part.id).index or 0
    multiview.start_views(ctx, part, spec, target=f"acc:{idx}", front_sha=res.final_sha, board="acc.board", nonce=ctx.step.nonce)


def badge_done(ctx: StepContext, loop: AL.AssetLoopSpec, res: AL.LoopResult) -> None:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, loop.part_id)
    if res.needs_human or not res.final_sha:
        parts.mark_needs_human(ctx, project_id, part.id, report=res.report, best=res.best_sha,
                               assets={"badge_art": res.best_sha} if res.best_sha else None, results=res.results)
        return
    with rt.db.tx():
        rt.repo.kv_set(f"acc:{project_id}:{part.id}", {"front": res.final_sha, "alternatives": res.alternatives, "technique": res.technique,
                                                       "flags": res.flags, "results": common.summarize_checks(res.results)})
    st = rt.ops.new_step("acc.board", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id,
                         params=AccParams(part_id=part.id, front_sha=res.final_sha, nonce=ctx.step.nonce).model_dump(mode="json"),
                         nonce=ctx.step.nonce, priority=ctx.step.priority)
    ctx.spawn([st])


# ---------------------------------------------------------------------------------------------------- the board
PREVIEW_VIEWS = ("front", "left", "back", "three_quarter")      # a slab or a primitive tile shows four views (the judge sheet default is six: it needs right and top)


def scale_renders(rt: Runtime, project_id: str, item: itemspec.Item, *, art: Image.Image | None, mesh: Any = None) -> tuple[dict[str, Image.Image], str]:
    """The scale render (front and side) of the item on the mannequin; head-area items sit against the approved hair."""
    from duoskin.render import avatar, sheets

    mq = avatar.default_mannequin()
    hair, hair_state = (None, "ok")
    if item.category in HEAD_CATEGORIES:
        hair, hair_state = hair_front_for(rt, project_id, item.character)
    kw: dict[str, Any] = {"target_studs": item.target_studs}
    if mesh is not None:
        kw["mesh"] = mesh
    elif art is not None:
        kw["accessory_rgba"] = art
    out = {"scale_front": sheets.scale_render(mq, item.asset_type, item.attachment, hair_front_rgba=hair, **kw),
           "scale_side": sheets.scale_render(mq, item.asset_type, item.attachment, view="left", **kw)}
    return out, hair_state


def run_board(ctx: StepContext, p: AccParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    item = itemspec.accessory_item(spec, part.id)
    st = dict(rt.repo.kv_get(f"acc:{project_id}:{part.id}") or {})
    mv = rt.repo.get_step(p.mv_step).result if p.mv_step else {}
    views = dict(mv.get("views") or {})
    multiview.remember_task(rt, project_id, part.id, mv)
    front_sha = p.front_sha or st.get("front", "")
    assets: dict[str, str] = {}
    pv_in = [ctx.get_asset(front_sha).pixel_sha or front_sha] if front_sha else []
    pv = common.prov("code", step_kind="acc.board", input_shas=pv_in, params={"build": item.build, "attachment": item.attachment,
                                                                             "target_studs": list(item.target_studs)})
    flags: list[str] = list(mv.get("flags") or [])
    facts: dict[str, Any] = {"build": item.build, "asset_type": item.asset_type, "attachment": item.attachment, "target_studs": list(item.target_studs),
                             "technique": st.get("technique")}
    art = common.open_image(ctx.read_asset(front_sha)) if front_sha else None
    mesh = None
    results: list[Any] = []
    if item.build == "tripo":
        assets["front"] = front_sha
        for v, sha in views.items():
            assets[f"view.{v}"] = sha
        ims = {v: common.open_image(ctx.read_asset(s)) for v, s in views.items() if s}
        if len(ims) == 4:
            from duoskin.imaging import guides

            sheet = guides.side_by_side([guides.vlm_image(ims[v].convert("RGBA"), with_checkerboard=False).resize((320, 320)) for v in multiview.VIEWS],
                                        gutter=12)
            assets["views_sheet"] = common.put_png(ctx, sheet, role="views_sheet", part_id=part.id, provenance=pv, status="final").sha256
    elif item.build == "sticker_slab":
        from duoskin.imaging import slab_art
        from duoskin.mesh import slab as SL
        from duoskin.render import sheets

        assert art is not None
        border_hex = _border_hex(spec, item)
        badge = slab_art.build_badge(art, border_px=BORDER_PX, border_hex=border_hex)
        assets["badge_art"] = front_sha
        assets["badge"] = common.put_png(ctx, badge.image, role="badge", part_id=part.id, provenance=pv, status="final").sha256
        sres = SL.build_slab(badge.image, size_studs=max(item.target_studs[:2]), thickness=0.1, tris_budget=2800, kind="sticker_slab",
                             texture_px=item.texture_px)
        mesh = sres.mesh
        prev = sheets.mesh_judge_sheet(sheets.render_mesh_views(mesh, PREVIEW_VIEWS, size=256), order=PREVIEW_VIEWS, cols=4)
        assets["slab_preview"] = common.put_png(ctx, prev, role="slab_preview", part_id=part.id, provenance=pv, status="final").sha256
        facts["slab"] = {k: v for k, v in sres.facts.items() if isinstance(v, (int, float, str, bool))}
        results.append(common.mk_result("A_BADGE_SOLID", badge.solidity >= 0.5, kind="hard", metric="solidity", value=float(badge.solidity),
                                        evidence=f"solidity {badge.solidity:.2f}", fix_hint="human"))
        art = badge.image
    else:
        from duoskin.mesh import primitives
        from duoskin.render import sheets

        kind = primitive_kind(item)
        mesh = primitives.build(kind, _primitive_params(kind, item), palette_rgb(spec, item.colour_refs), texture_px=item.texture_px)
        prev = sheets.mesh_judge_sheet(sheets.render_mesh_views(mesh, PREVIEW_VIEWS, size=256), order=PREVIEW_VIEWS, cols=4)
        assets["preview"] = common.put_png(ctx, prev, role="primitive_preview", part_id=part.id, provenance=pv, status="final").sha256
        facts["primitive"] = kind
    renders, hair_state = scale_renders(rt, project_id, item, art=art if mesh is None else None, mesh=mesh)
    for role, im in renders.items():
        assets[role] = common.put_png(ctx, im, role=role, part_id=part.id, provenance=pv, status="final").sha256
    if item.category in HEAD_CATEGORIES and hair_state != "ok":
        flags.append("hair_not_approved")
    flags = [f for f in dict.fromkeys(flags + (["mock"] if clothing_mock(rt, [s for s in (front_sha,) if s]) else []))]
    for r, sha in assets.items():
        common.link_asset(rt, project_id, part.id, sha, f"board.{r}", "final", common.prov("code", params={"role": r}), step_id=ctx.step.id)
    parts.set_tile_facts(rt, project_id, part.id, **facts)
    drop = [f for f in part.flags if f in ("hair_not_approved", "views_from_gpt") and f not in flags]
    if drop:
        parts.set_part_state(rt, project_id, part.id, part.state, flags_remove=drop)
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    if mv.get("ok") is False or blocking:
        parts.mark_needs_human(ctx, project_id, part.id, report=str(mv.get("report") or "; ".join(r.evidence for r in blocking[:2]) or "needs a look"),
                               assets=assets, results=results)
    else:
        alts = [{"front": a} for a in st.get("alternatives", [])] if item.build == "tripo" else []
        rt.repo.mutate_part(project_id, part.id, lambda x: setattr(x, "alternatives", alts))
        parts.mark_ready(ctx, project_id, part.id, assets=assets, flags_add=flags, results=results)
    return StepResult(outputs=[], result={"assets": assets}, message="accessory tile ready")


def clothing_mock(rt: Runtime, shas: list[str]) -> bool:
    from duoskin.pipeline import clothing

    return clothing.mock_lineage(rt, shas)


def _border_hex(spec: dict[str, Any], item: itemspec.Item) -> str:
    pal = common.palette_map(spec)
    for c in spec.get("palette", []):
        if c.get("role") == "line":
            return str(c["hex"]).lower()
    return pal.get(item.colour_refs[0], "#2b2b33") if item.colour_refs else "#2b2b33"


def _primitive_params(kind: str, item: itemspec.Item) -> dict[str, Any]:
    s = float(item.target_studs[0])
    return {"ring": {"major": s * 0.35, "minor": s * 0.08}, "loop": {"width": s * 0.7, "height": s * 0.5}, "strap": {"length": s * 0.9, "width": s * 0.2},
            "bead": {"radius": s * 0.4}, "charm": {}}.get(kind, {})


# ---------------------------------------------------------------------------------------------------- BUILD
def build_job(rt: Runtime, project_id: str, part: Part, spec: dict[str, Any], out_dir: str, *, op: str, art_path: str = "") -> Any:
    from duoskin.mesh.types import MeshJob

    item = itemspec.accessory_item(spec, part.id)
    params: dict[str, Any] = {"stem": "acc", "render": True, "want_fbx": True}
    if op == "slab":
        params.update({"art_path": art_path, "size_studs": max(item.target_studs[:2]), "thickness": 0.1, "back": "plain", "kind": item.build})
    else:
        kind = primitive_kind(item)
        params.update({"kind": kind, "params": _primitive_params(kind, item), "palette": palette_rgb(spec, item.colour_refs)["base"], "fit": False})
    return MeshJob(op=op, input_path=art_path or "", out_dir=out_dir, asset_type=item.asset_type, attachment=item.attachment,   # type: ignore[arg-type]
                   target_studs=item.target_studs, tris_target=item.tris_target, texture_px=item.texture_px, params=params, asset_id=part.id,
                   licence="n/a")   # type: ignore[arg-type]


def register(rt: Runtime | None = None) -> None:
    parts.register_lane(AccessoryLane())
    AL.register_callback("acc_front", acc_front_done)
    AL.register_callback("badge", badge_done)
    registry.register_handler("acc.board", run_board, version=1, pool="cpu", paid=False, Params=AccParams, cacheable=False)
