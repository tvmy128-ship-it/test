"""The hair lane (APP_SPEC §10.7, §10.7.1, bible §4.3): the front view on the bald guide, its views, the kit match and the fitted mesh.

PARTS (this module's lane):

1. **I4** paints the hairstyle onto the grey bald-head guide (``guides.guide_bald_head``; the mask is the Hair box minus the lower 55% of the
   head's front). The kit route adds the kit style render as Image 3 (and I4k as the second technique); ``hair_custom`` has no Image 3.
2. Code derives the **hair-only RGBA** (the front view minus the known head guide); that is what goes to Tripo (T1), the version with the
   head is kept for the tile.
3. ``multiview`` makes the four views (T1, T2 once, or I10 flagged ``views_from_gpt``) and ``mv.check`` checks them.
4. ``hair.board`` shows the tile: front on the guide, the hair-only front, the four views and the **kit match score** (silhouette IoU of the
   hair-only front against every kit style's front, when a kit exists).

BUILD (handlers here, chained by ``build.py``): ``hair.kit_match`` (L9: the top candidates by silhouette IoU, a style, modules and the
adjustments the style supports) and ``hair.fit`` (the worker's ``fit_hair``: assemble style + modules in the HairAttachment frame, recolour the
bands, repair, export, validate; trimesh/numpy only, no Blender). Routes: **kit** (default), **custom** (``hair_custom``, an empty hair kit or
``hair_route`` tripo_api/manual: Tripo or the manual pack, then ``hair.register``), **manual** (the Tripo pack, same gate).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np
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

log = logging.getLogger("duoskin.hair")

CUSTOM = "hair_custom"
MASK_TOL = 24
SIL_PX = 128


class HairParams(Strict):
    part_id: str
    front_sha: str = ""
    nonce: str = ""
    mv_step: str = ""


# ---------------------------------------------------------------------------------------------------- routes
def hair_route(rt: Runtime, project_id: str, spec: dict[str, Any], character: str) -> tuple[str, list[str]]:
    """``("kit" | "custom", flags)``: the build route of this character's hair (APP_SPEC §10.7 table)."""
    kit = kits.load_context(rt)
    style = spec[character]["hair"]["kit_style_id"]
    setting = rt.repo.get_project(project_id).settings.hair_route
    flags: list[str] = []
    if setting == "manual":
        return "custom", flags
    if style == CUSTOM or setting == "tripo_api" or kit.hair_style_dir(style) is None:
        if kit.hair_kit_empty:
            flags.append("hair_custom_no_kit")
        return "custom", flags
    return "kit", ["kit_hair"]


def kit_front_png(rt: Runtime, style_id: str) -> bytes | None:
    d = kits.load_context(rt).hair_style_dir(style_id)
    f = d / "views" / "front.png" if d else None
    return f.read_bytes() if f and f.exists() else None


# ---------------------------------------------------------------------------------------------------- masks
def hair_mask(front: Image.Image, guide: Image.Image) -> np.ndarray:
    """The hair of an I4 result: the pixels that differ from the bald-head guide it was painted on (the head cube and the white
    background are the guide's own)."""
    a = np.asarray(front.convert("RGB")).astype(int)
    g = np.asarray(guide.convert("RGB")).astype(int)
    m = np.abs(a - g).max(axis=2) > MASK_TOL
    return _clean(m)


def _clean(m: np.ndarray) -> np.ndarray:
    from scipy import ndimage

    m = ndimage.binary_opening(m, iterations=1)
    lab, n = ndimage.label(m)
    if n > 1:
        sizes = ndimage.sum(m, lab, range(1, n + 1))
        keep = [i + 1 for i, s in enumerate(sizes) if s >= 0.02 * sizes.max()]
        m = np.isin(lab, keep)
    return ndimage.binary_closing(m, iterations=2)


def hair_only_rgba(front: Image.Image, guide: Image.Image) -> Image.Image:
    """The hair alone on a transparent canvas (what T1 receives; the grey head never reaches Tripo)."""
    m = hair_mask(front, guide)
    rgba = np.asarray(front.convert("RGBA")).copy()
    rgba[..., 3] = np.where(m, 255, 0)
    return Image.fromarray(rgba, "RGBA")


def silhouette(im: Image.Image, *, bg: tuple[int, int, int] | None = None) -> np.ndarray:
    """A scale-normalised silhouette: the subject's alpha (or its difference from the corner colour), cropped to its box and resized
    to ``SIL_PX`` squared, so a render and a drawing of different sizes are comparable."""
    rgba = np.asarray(im.convert("RGBA"))
    if rgba[..., 3].min() < 250:
        m = rgba[..., 3] > 127
    else:
        corner = np.array(bg if bg is not None else rgba[0, 0, :3], int)
        m = np.abs(rgba[..., :3].astype(int) - corner).max(axis=2) > MASK_TOL
    ys, xs = np.nonzero(m)
    if len(ys) == 0:
        return np.zeros((SIL_PX, SIL_PX), bool)
    crop = Image.fromarray((m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype(np.uint8))
    return np.asarray(crop.resize((SIL_PX, SIL_PX), Image.Resampling.BILINEAR)) > 127


def iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = float((a & b).sum())
    union = float((a | b).sum())
    return inter / union if union else 0.0


def kit_candidates(rt: Runtime, front_mask_img: Image.Image, *, top: int = 5) -> list[dict[str, Any]]:
    """The kit styles ranked by the silhouette IoU of their front view against the approved hair-only front."""
    kit = kits.load_context(rt)
    target = silhouette(front_mask_img)
    out = []
    for sid in sorted(kit.manifest.get("hair", {})):
        png = kit_front_png(rt, sid)
        if png is None:
            continue
        out.append({"style_id": sid, "iou": round(iou(target, silhouette(common.open_image(png), bg=(242, 242, 242))), 4)})
    out.sort(key=lambda r: -r["iou"])
    return out[:top]


# ---------------------------------------------------------------------------------------------------- the lane
def loop_for(rt: Runtime, project_id: str, spec: dict[str, Any], part: Part, route: str, *, nonce: str) -> AL.AssetLoopSpec:
    from duoskin.imaging import guides

    c = part.character
    h = spec[c]["hair"]
    hexes = [x for x in (common.hex_of(spec, h.get(k)) for k in ("colour_ref", "shadow_ref", "highlight_ref")) if x]
    guide = guides.guide_bald_head(hexes)
    gsha = rt.cas.put(guide.image_png(), "png", prov=common.prov("code", params={"guide": guide.guide_id, "grey": guide.meta["guide_grey"]})).sha256
    msha = rt.cas.put(guide.mask_png(), "png", prov=common.prov("code", params={"guide": guide.guide_id, "mask": True})).sha256
    refs = common.concept_refs(rt, project_id, spec, part.id)
    kit_png = kit_front_png(rt, h["kit_style_id"]) if route == "kit" else None
    kit_sha = rt.cas.put(kit_png, "png", prov=common.prov("code", params={"kit_style": h["kit_style_id"], "view": "front"})).sha256 if kit_png else None
    images = [gsha, refs.crop_sha, *([kit_sha] if kit_sha else [])]
    by_tech = {"I4k": AL.TechOverride(images=[i for i in (kit_sha, refs.crop_sha) if i], slots={})} if kit_sha else {}
    gate_a, hard, soft = prints.loop_checks("I4.hair_front")
    box = guide.meta["head_box"]
    return AL.AssetLoopSpec(
        part_id=part.id, character=c, role="hair_front", template_id="I4.hair_front", slots={"kit_render": bool(kit_sha)},   # type: ignore[arg-type]
        images=[i for i in images if i], mask=msha, overrides=by_tech, n_drafts=4, finalize=True,
        gate_a=[g for g in gate_a if g != "A_SYMMETRY"], gate_b_hard=hard, gate_b_soft=soft, technique_ladder="hair", then="hair_front",
        then_params={"route": route, "guide": gsha}, palette_hex=[],
        expected={"guide_hex": guide.meta["guide_grey"], "head_box": list(box), "symmetric": False},
        reference_shas=[refs.crop_sha] if refs.crop_sha and not refs.placeholder_crop else [], nonce=nonce, background="opaque",
        kit_subset_sha=kits.load_context(rt).subset_sha(hair=h["kit_style_id"], route=route),
        available=sorted(common.available_providers(rt)))


class HairLane(parts.Lane):
    kind = PartKind.HAIR

    def waits_for(self, part: Part, all_parts: dict[str, Part]) -> list[str]:
        return []

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:
        rt = ctx.rt
        project_id = ctx.step.project_id or ""
        route, flags = hair_route(rt, project_id, spec, part.character)
        keep = [f for f in part.flags if f in ("hair_custom_no_kit", "kit_hair", "views_from_gpt")]
        parts.set_part_state(rt, project_id, part.id, part.state, flags_add=flags, flags_remove=[f for f in keep if f not in flags])
        loop = loop_for(rt, project_id, spec, part, route, nonce=nonce)
        AL.start_loop(rt, ctx.step.job_id, project_id, spec, loop, nonce=nonce, priority=ctx.step.priority)


def hair_front_done(ctx: StepContext, loop: AL.AssetLoopSpec, res: AL.LoopResult) -> None:
    """The I4 loop finished: derive the hair-only RGBA and start the multiview chain."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, loop.part_id)
    if res.needs_human or not res.final_sha:
        parts.mark_needs_human(ctx, project_id, part.id, report=res.report, best=res.best_sha,
                               assets={"front": res.best_sha} if res.best_sha else None, results=res.results)
        return
    guide_sha = loop.then_params["guide"]
    front = common.open_image(ctx.read_asset(res.final_sha))
    guide = common.open_image(ctx.read_asset(guide_sha))
    only = hair_only_rgba(front, guide)
    fa = ctx.get_asset(res.final_sha)
    pv = common.prov("mock" if fa.first_provenance.source == "mock" else "code", step_kind="hair.only", input_shas=[fa.pixel_sha or res.final_sha],
                     notes=["mock_lineage"] if clothing_mock(rt, [res.final_sha]) else [], params={"from": "front minus the guide head"})
    only_sha = common.put_png(ctx, only, role="hair_only", part_id=part.id, provenance=pv, status="final").sha256
    with rt.db.tx():
        rt.repo.kv_set(f"hair:{project_id}:{part.id}", {"front": res.final_sha, "hair_only": only_sha, "guide": guide_sha, "route": loop.then_params["route"],
                                                       "flags": res.flags, "alternatives": res.alternatives, "technique": res.technique,
                                                       "results": common.summarize_checks(res.results)})
    _, spec = common.load_spec(rt, project_id)
    multiview.start_views(ctx, part, spec, target="hair", front_sha=only_sha, board="hair.board", nonce=ctx.step.nonce)


def clothing_mock(rt: Runtime, shas: list[str]) -> bool:
    from duoskin.pipeline import clothing

    return clothing.mock_lineage(rt, shas)


def run_board(ctx: StepContext, p: HairParams, inputs: list[Any]) -> StepResult:
    """The tile: the front on the bald guide, the hair-only front, the four views and the kit match score."""
    from duoskin.imaging import guides

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    st = dict(rt.repo.kv_get(f"hair:{project_id}:{part.id}") or {})
    mv = rt.repo.get_step(p.mv_step).result if p.mv_step else {}
    views = dict(mv.get("views") or {})
    multiview.remember_task(rt, project_id, part.id, mv)
    assets: dict[str, str] = {"front": st["front"], "hair_only": st["hair_only"]}
    for v, sha in views.items():
        assets[f"view.{v}"] = sha
    ims = {v: common.open_image(ctx.read_asset(s)) for v, s in views.items() if s}
    pv = common.prov("code", step_kind="hair.board", input_shas=[ctx.get_asset(s).pixel_sha or s for s in views.values()])
    if len(ims) == 4:
        sheet = guides.side_by_side([guides.vlm_image(ims[v].convert("RGBA"), with_checkerboard=False).resize((320, 320)) for v in multiview.VIEWS], gutter=12)
        assets["views_sheet"] = common.put_png(ctx, sheet, role="views_sheet", part_id=part.id, provenance=pv, status="final").sha256
    route = st.get("route", "kit")
    facts: dict[str, Any] = {"route": route, "technique": st.get("technique"), "views_source": mv.get("source", "tripo")}
    only_img = common.open_image(ctx.read_asset(st["hair_only"]))
    cands = kit_candidates(rt, only_img) if kits.load_context(rt).flags.get("hair_kit_empty") is False else []
    if cands:
        facts["kit_match"] = cands[0]
        facts["kit_candidates"] = cands[:3]
    parts.set_tile_facts(rt, project_id, part.id, **facts)
    for r, sha in assets.items():
        common.link_asset(rt, project_id, part.id, sha, f"board.{r}", "final", common.prov("code", params={"role": r}), step_id=ctx.step.id)
    flags = list(mv.get("flags") or []) + (["mock"] if clothing_mock(rt, [st["front"]]) else [])
    results = []
    if mv.get("ok") is False:
        parts.mark_needs_human(ctx, project_id, part.id, report=str(mv.get("report") or "the views need a look"), assets=assets, results=results)
    else:
        alts = [{"front": a} for a in st.get("alternatives", [])]
        rt.repo.mutate_part(project_id, part.id, lambda x: setattr(x, "alternatives", alts))
        warns = []
        if cands and cands[0]["iou"] < float(thr("hair.kit_match_min")):
            warns.append(parts.soft_warning(part.id, "HAIR-08", f"The closest kit hairstyle matches this front view only {cands[0]['iou']:.2f} (below "
                                                                f"{thr('hair.kit_match_min')}): see the other kit styles or make the hair by hand."))
        parts.mark_ready(ctx, project_id, part.id, assets=assets, flags_add=flags, results=results, warnings=warns)
    return StepResult(outputs=[], result={"assets": assets, "kit_match": cands[:1]}, message="hair tile ready")


# ---------------------------------------------------------------------------------------------------- BUILD: hair.kit_match and hair.fit
def candidate_sheet(rt: Runtime, style_id: str) -> Image.Image | None:
    """The four kit renders of one style side by side on grey (a candidate sheet for L9)."""
    from duoskin.imaging import guides

    d = kits.load_context(rt).hair_style_dir(style_id)
    if d is None:
        return None
    tiles = []
    for v in ("front", "left", "back", "right"):
        f = d / "views" / f"{v}.png"
        if f.exists():
            tiles.append(Image.open(f).convert("RGB").resize((256, 256)))
    return guides.side_by_side(tiles, gutter=8) if tiles else None


def _module_id(kit: kits.KitContext, kind: str, wanted: str, style_default: str | None) -> str | None:
    """A fringe or back module id the kit has (``kit_default`` = the style's own default, ``none`` = no module)."""
    have = kit.manifest.get("hair_modules", {}).get(kind, {})
    if wanted == "kit_default":
        return style_default if style_default in have else None
    if wanted in ("none", "", None):
        return None
    return wanted if wanted in have else (style_default if style_default in have else None)


def run_kit_match(ctx: StepContext, p: Any, inputs: list[Any]) -> StepResult:
    """L9: the kit style a hair artist would start from (top candidates by silhouette IoU), the fringe and back modules and the adjustments the style
    supports. Code validates the answer: a style, module or adjustment the kit does not have is dropped with a log line (APP_SPEC §10.7.1)."""
    from duoskin.models.llm_io import HairMatch
    from duoskin.pipeline import llmcall

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    h = spec[part.character]["hair"]
    kit = kits.load_context(rt)
    st = dict(rt.repo.kv_get(f"hair:{project_id}:{part.id}") or {})
    only = common.open_image(ctx.read_asset(st["hair_only"]))
    cands = kit_candidates(rt, only, top=5)
    spec_style = h["kit_style_id"]
    if spec_style in {c["style_id"] for c in cands}:                        # the spec's own style is always a candidate (it is what the planner chose)
        cands.sort(key=lambda r: (r["style_id"] != spec_style, -r["iou"]))
    match = None
    notes: list[str] = []
    if common.provider_available(rt, "anthropic") and cands:
        views = [common.open_image(ctx.read_asset(part.board_assets[f"view.{v}"])) for v in multiview.VIEWS if f"view.{v}" in part.board_assets]
        from duoskin.imaging import guides

        approved = guides.side_by_side([guides.vlm_image(v.convert("RGBA"), with_checkerboard=False).resize((256, 256)) for v in views], gutter=8) if views else only
        sheets = [candidate_sheet(rt, c["style_id"]) for c in cands]
        lines = [f"c{i + 1} {c['style_id']}: silhouette IoU {c['iou']:.2f}; supports {', '.join(kit.manifest['hair'][c['style_id']].get('supported_adjustments', [])) or 'nothing'}"
                 for i, c in enumerate(cands)]
        try:
            match = llmcall.ask_llm(ctx, "L9.hair_kit_matcher", {"measured_facts": "; ".join(lines)}, images=[approved, *[s for s in sheets if s]], out=HairMatch,
                                    inventory=kit.inventory)
        except Exception as exc:  # noqa: BLE001 - the matcher is advice: the best silhouette overlap is a fine answer
            log.warning("L9 failed (%s): using the best silhouette match", type(exc).__name__)
            notes.append(f"L9 unavailable: {type(exc).__name__}")
    pick = cands[0] if cands else {"style_id": spec_style, "iou": 0.0}
    if match is not None and match.choice != "none_fit":
        i = int(match.choice[1:]) - 1
        if 0 <= i < len(cands):
            pick = cands[i]
    elif match is not None:
        notes.append("L9 found no fitting kit style: the closest silhouette is used")
    if match is not None:
        notes += list(match.mismatch_notes)[:3]
    style_id = pick["style_id"]
    style_json = kit.hair_style_json(style_id)
    supported = set(style_json.get("supported_adjustments") or [])
    adjustments = []
    for a in (match.adjustments if match is not None else []):
        if a.param in supported:
            adjustments.append({"param": a.param, "value": a.value})
        else:
            log.info("hair.kit_match: dropped %s=%s (style %s does not support it)", a.param, a.value, style_id)
    default_fringe = style_json.get("default_fringe")
    fringe = _module_id(kit, "fringe", (match.fringe_id if match is not None else h.get("fringe_id")), default_fringe)
    back = _module_id(kit, "back", (match.back_id if match is not None else h.get("back_id")), style_json.get("default_back"))
    result = {"ok": True, "style_id": style_id, "iou": pick["iou"], "fringe": fringe, "back": back, "adjustments": adjustments, "candidates": cands[:3],
              "notes": notes}
    if pick["iou"] < float(thr("hair.kit_match_min")):
        result["soft_warning"] = f"kit match {pick['iou']:.2f} is below {thr('hair.kit_match_min')}"
    parts.set_tile_facts(rt, project_id, part.id, build_kit_match={"style_id": style_id, "iou": pick["iou"], "fringe": fringe, "back": back,
                                                                   "adjustments": adjustments})
    return StepResult(result=result, message=f"kit style {style_id} (match {pick['iou']:.2f})")


def thr(name: str) -> Any:
    from duoskin.checks import thresholds

    return thresholds.get(name)


def _kit_registered(res: Any) -> Any:
    """Kit hair is registered by construction (APP_SPEC 10.7): the worker's fit op asks for CHK-M21 and, with views, M08/M13, which do not apply."""
    from duoskin.pipeline import meshsteps

    res.checks = meshsteps.kit_not_applicable(list(res.checks))
    return res


def run_fit(ctx: StepContext, p: Any, inputs: list[Any]) -> StepResult:
    """``hair.fit``: assemble style + modules in the HairAttachment frame, apply the supported adjustments, recolour the bands, repair, export, validate."""
    from duoskin.mesh.types import MeshJob
    from duoskin.pipeline import build, meshrun, meshsteps

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    prev = dict(rt.repo.get_step(p.from_step).result)
    _, spec = common.load_spec(rt, project_id)
    item = itemspec.hair_item(spec, part.character)
    kit = kits.load_context(rt)
    style_dir = kit.hair_style_dir(prev["style_id"])
    if style_dir is None:
        return StepResult(result={"ok": False, "reason": f"the kit style {prev['style_id']} is not in the kit any more"}, message="the kit style is missing")
    modules = []
    for kind, mid in (("fringe", prev.get("fringe")), ("back", prev.get("back"))):
        d = kit.hair_module_dir(kind, mid) if mid else None
        if d is not None:
            modules.append(str(d / "mesh.glb"))
    h = spec[part.character]["hair"]
    pal = common.palette_map(spec)
    palette = {"base": common.hex_to_rgb(pal[h["colour_ref"]]), "shadow": common.hex_to_rgb(pal[h["shadow_ref"]])}
    if h.get("highlight_ref") not in (None, "", "none") and h["highlight_ref"] in pal:
        palette["highlight"] = common.hex_to_rgb(pal[h["highlight_ref"]])
    work = meshsteps.work_dir(ctx)
    params = {"style_path": str(style_dir / "mesh.glb"), "module_paths": modules, "palette": palette, "adjustments": prev.get("adjustments", []),
              "stem": "hair", "render": True, "want_fbx": bool(kits.blender_present(rt))}
    # Kit hair is made by code in the HairAttachment frame, so the orientation search (M08) and the silhouette match against the approved views
    # (M13) do not apply: the L9 kit-match score and its SOFT warning are the match measure, and mesh.judge still compares the renders with the views.
    job = MeshJob(op="fit_hair", input_path=str(style_dir / "mesh.glb"), out_dir=str(work / "out"), asset_type="Hair", attachment="HairAttachment",   # type: ignore[arg-type]
                  target_studs=item.target_studs, tris_target=item.tris_target, approved_views={}, texture_px=item.texture_px, params=params,
                  asset_id=part.id, forward_axis=meshsteps.forward_axis(rt), licence="n/a", blender_path=meshsteps.blender_path(rt))
    ctx.progress(0.2, "fitting the kit hair")
    res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt), timeout_s=300)
    res = _kit_registered(res)
    demo = style_dir.is_relative_to(kits.DEMO_DIR)
    out = build._store_code_mesh(ctx, res, part, "kit hair", [], mock=demo)      # a demo kit is a stand-in: its files are never exportable (CHK-E01)
    out.result.update({"style_id": prev["style_id"], "kit_iou": prev.get("iou"), "kit_origin": "demo" if demo else "user", "soft_warning": prev.get("soft_warning", "")})
    return out


def register(rt: Runtime | None = None) -> None:
    parts.register_lane(HairLane())
    AL.register_callback("hair_front", hair_front_done)
    registry.register_handler("hair.board", run_board, version=1, pool="cpu", paid=False, Params=HairParams, cacheable=False)
    from duoskin.pipeline.build import guarded
    from duoskin.pipeline.meshsteps import MeshStepParams

    registry.register_handler("hair.kit_match", guarded(run_kit_match, "anthropic"), version=1, pool="api", paid=True, provider="anthropic", Params=MeshStepParams,
                              estimate=lambda p: 0.1, cacheable=False)
    registry.register_handler("hair.fit", guarded(run_fit), version=1, pool="proc", paid=False, Params=MeshStepParams, cacheable=False)


_ = itemspec
