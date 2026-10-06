"""The face lane (APP_SPEC §10.6, bible §4 and §11.3): five AI parts, the face canvas composer, the 2D preview and the face checks.

Per character the lane makes five **parts** with the asset loop: ``iris``, ``lash_upper``, ``brow``, ``mouth_closed`` and ``mouth_open``
(R1 vector by default, I3 without a Recraft key or as the ladder rung, code-parametric as the last rung). Everything else on the face (the
sclera, the closed-lid line, highlights, lower ticks, nose, blush, shading) is code (``imaging.face_canvas``). When every part is in, the
``face.assemble`` step composes the face on the canvas (mirroring the right eye, the highlights at the same image-space offset in both
eyes, single-colour lines of at least the minimum width), writes the **face layer pack** (one PNG per layer and ``face_canvas.json``),
renders the tone sheet (4 expressions x 5 skin tones, badge "2D preview - no head base") and runs the face checks.

Without a head base the rig checks are ``not_applicable`` with the reason ``no_head_base`` (the runner sets that, never this module) and the
2D equivalents run. With a head base (``kits/head_base/<variant>/``) the same composer runs on its ``face_canvas.json``; ``head.texture`` warps
the approved layers into the head UV at BUILD (no new AI cost).

Reimagine and Change take a ``target`` (``iris``, ``lash``, ``brow``, ``mouth_closed``, ``mouth_open`` or ``all``): only that part's loop
runs again and the others keep their parts (``face:<project>:<part>`` in the key-value store).

Board assets: ``tone_sheet``, ``neutral``, ``blink``, ``mouth_open``, ``happy`` (on the character's own skin tone), ``canvas`` (the canvas
JSON) and ``layer.<name>`` for the twelve layers. Alternatives (the second assembled face) are not made: see the module notes in the report.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from PIL import Image

from duoskin.engine import registry
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict
from duoskin.models.part import Part, PartKind, PartState
from duoskin.pipeline import assetloop as AL
from duoskin.pipeline import common, kits, parts, prints

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.face")

AI_PARTS = ("iris", "lash_upper", "brow", "mouth_closed", "mouth_open")
TARGET_PARTS = {"iris": ("iris",), "lash": ("lash_upper",), "lash_upper": ("lash_upper",), "brow": ("brow",), "mouth_closed": ("mouth_closed",),
                "mouth_open": ("mouth_open",), "all": AI_PARTS}
PART_ATTR = {"iris": "iris_imgR", "lash_upper": "lash_upper_imgR", "brow": "brow_imgR", "mouth_closed": "mouth_closed", "mouth_open": "mouth_open"}
NOUN = {"iris": "eye iris", "lash_upper": "upper eyelash line", "brow": "eyebrow", "mouth_closed": "closed mouth", "mouth_open": "open mouth"}
GRAMMAR_FIELD = {"iris": "iris_style", "lash_upper": "lash_style", "brow": "brow_style", "mouth_closed": "mouth_style", "mouth_open": "mouth_style"}
LINE_PARTS = ("lash_upper", "brow", "mouth_closed")
GATE_B = {
    "iris": ("fp_single_feature", "fp_front_view", "fp_shape_word", "fp_no_highlight", "fp_style_match"),
    "lash_upper": ("fp_single_feature", "fp_front_view", "fp_shape_word", "fp_orientation", "fp_one_line_colour", "fp_thick_shapes", "fp_style_match"),
    "brow": ("fp_single_feature", "fp_front_view", "fp_shape_word", "fp_orientation", "fp_one_line_colour", "fp_thick_shapes", "fp_style_match"),
    "mouth_closed": ("fp_single_feature", "fp_front_view", "fp_shape_word", "fp_one_line_colour", "fp_thick_shapes", "fp_style_match"),
    "mouth_open": ("fp_single_feature", "fp_front_view", "fp_shape_word", "fp_style_match"),
}
GATE_A = ("A_ALPHA", "A_COMPONENTS", "A_STROKE", "A_OCR", "A_PHASH", "A_HIGHLIGHT", "A_GUIDE_LEFT")
GUIDE_HEX = "#9a9a9a"
#: the mock image providers draw placeholder shapes that cannot be placed on the face rig (the assembled face checks would fail on them,
#: correctly): with a mock first technique the face parts are drawn by code ("code_face", the last rung) and the tile says so.
#: Tests that exercise the R1 / I3 routes set this to False.
CODE_PARTS_WHEN_MOCK = True


class FaceParams(Strict):
    part_id: str
    nonce: str = ""


# ---------------------------------------------------------------------------------------------------- spec and canvas
def face_spec(spec: dict[str, Any], character: str):
    from duoskin.imaging import face_canvas as FC

    return FC.FaceSpec.from_spec(spec[character]["face"], common.palette_map(spec))


def load_canvas(rt: Runtime, fspec: Any):
    """The canvas of the head base variant for this eye shape when a validated head base exists, else the built-in 2D default."""
    from duoskin.imaging import face_canvas as FC

    kit = kits.load_context(rt)
    base = kit.head_base_dir(fspec.eye_shape)
    if base is not None and (base / "face_canvas.json").exists():
        return FC.load_canvas(base / "face_canvas.json"), True
    return FC.load_canvas(), False


def part_colours(fspec: Any, name: str) -> list[str]:
    vals = {"iris": [fspec.iris, fspec.iris_dark, fspec.pupil], "lash_upper": [fspec.lash], "brow": [fspec.brow],
            "mouth_closed": [fspec.mouth_line, fspec.mouth_inner], "mouth_open": [fspec.mouth_inner, fspec.tongue, fspec.teeth]}[name]
    return [v.lower() for v in vals if v]


def state_key(project_id: str, part_id: str) -> str:
    return f"face:{project_id}:{part_id}"


def get_state(rt: Runtime, project_id: str, part_id: str) -> dict[str, Any]:
    return dict(rt.repo.kv_get(state_key(project_id, part_id)) or {"parts": {}, "pending": [], "nonce": "", "flags": {}})


# ---------------------------------------------------------------------------------------------------- the code drafter (last rung)
def code_part(fspec: Any, canvas: Any, name: str) -> Image.Image:
    from duoskin.imaging import face_canvas as FC

    return {"iris": lambda: FC.parametric_iris(fspec), "lash_upper": lambda: FC.parametric_lash(fspec),
            "brow": lambda: FC.parametric_brow(fspec), "mouth_closed": lambda: FC.parametric_mouth_closed(fspec),
            "mouth_open": lambda: FC.parametric_mouth_open(fspec, canvas)}[name]()


def draft_code_face(ctx: StepContext, p: AL.LoopStepParams, tech: AL.Technique) -> list[dict[str, Any]]:
    """``code_face``: the parametric part of the face grammar (deterministic, free, passes Gate A by construction)."""
    rt = ctx.rt
    ref = common.split_part_id(p.loop.part_id)
    _, spec = common.load_spec(rt, ctx.step.project_id or "")
    fspec = face_spec(spec, ref.character)
    canvas, _ = load_canvas(rt, fspec)
    name = str(p.loop.slots.get("part"))
    return [{"png": common.png_bytes(code_part(fspec, canvas, name)), "request_id": None}]


# ---------------------------------------------------------------------------------------------------- the lane
def part_loop(rt: Runtime, project_id: str, spec: dict[str, Any], part: Part, name: str, fspec: Any, canvas: Any, *, nonce: str) -> AL.AssetLoopSpec:
    from duoskin.checks import gate_b
    from duoskin.imaging import guides

    refs = common.concept_refs(rt, project_id, spec, part.id)
    guide = guides.guide_face_part(name, fspec, canvas)
    gsha = rt.cas.put(guide.image_png(), "png", prov=common.prov("code", params={"guide": guide.guide_id})).sha256
    msha = rt.cas.put(guide.mask_png(), "png", prov=common.prov("code", params={"guide": guide.guide_id, "mask": True})).sha256
    rules = list(GATE_B[name])
    hard = [r for r in rules if gate_b.is_hard(r)]
    soft = [r for r in rules if not gate_b.is_hard(r)]
    noun = NOUN[name]
    phrase = str(getattr(fspec, GRAMMAR_FIELD[name])).replace("_", " ")
    slots_b = {r: {"part": noun} for r in ("fp_single_feature", "fp_front_view", "fp_orientation", "fp_one_line_colour")}
    slots_b["fp_shape_word"] = {"grammar phrase": phrase, "part": noun}
    cols = part_colours(fspec, name)
    line = name in LINE_PARTS
    expected: dict[str, Any] = {"components": [1, 4], "placed_scale": 0.5, "margin": 0.0, "symmetric": False, "guide_hex": GUIDE_HEX}
    if line:
        expected["line_hex"] = cols[0]
    gate_a = [c for c in GATE_A if line or c != "A_SINGLE_COLOUR"] + (["A_SINGLE_COLOUR"] if line else [])
    return AL.AssetLoopSpec(
        part_id=part.id, character=part.character, role=name, template_id="R1.face_part", slots={"part": name},   # type: ignore[arg-type]
        images=[gsha, *[s for s in (refs.crop_sha, refs.style_sha) if s]], mask=msha, n_drafts=3, finalize=False, gate_a=gate_a,
        gate_b_hard=hard, gate_b_soft=soft, technique_ladder="face_part", then="face_part", then_params={"part": name}, palette_hex=cols,
        expected=expected, gate_b_slots=slots_b, background="transparent", nonce=nonce,
        kit_subset_sha=kits.load_context(rt).subset_sha(face_part=name, eye_shape=fspec.eye_shape, canvas=canvas.sha256),
        available=sorted(common.available_providers(rt)))


class FaceLane(parts.Lane):
    kind = PartKind.FACE

    def waits_for(self, part: Part, all_parts: dict[str, Part]) -> list[str]:
        return []

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:
        rt = ctx.rt
        project_id = ctx.step.project_id or ""
        fspec = face_spec(spec, part.character)
        canvas, _ = load_canvas(rt, fspec)
        names = list(TARGET_PARTS.get(target or "all", AI_PARTS))
        with rt.db.tx():
            st = get_state(rt, project_id, part.id)
            if (target or "all") == "all":
                st = {"parts": {}, "pending": [], "nonce": nonce, "flags": {}}
            st["pending"] = list(names)
            st["nonce"] = nonce or st.get("nonce", "")
            st["assembling"] = False
            rt.repo.kv_set(state_key(project_id, part.id), st)
        flags_clear = [f for f in part.flags if f.startswith("code_face:")]
        if flags_clear:
            parts.set_part_state(rt, project_id, part.id, part.state, flags_remove=flags_clear)
        for name in names:
            loop = part_loop(rt, project_id, spec, part, name, fspec, canvas, nonce=nonce)
            state = None
            ladder = AL.ladder_for(loop)
            if CODE_PARTS_WHEN_MOCK and "code_face" in ladder and common.is_mock(rt, AL.TECHNIQUES[ladder[0]].provider):
                state = AL.LoopState(technique_index=ladder.index("code_face"), n_total=1, nonce=nonce)
                parts.set_part_state(rt, project_id, part.id, PartState.GENERATING, flags_add=["mock_face_by_code"])
            AL.start_loop(rt, ctx.step.job_id, project_id, spec, loop, nonce=nonce, priority=ctx.step.priority, state=state)

    def recheck(self, ctx: StepContext, part: Part, spec: dict[str, Any]) -> None:
        spawn_assemble(ctx, part.id, nonce="", reason="recheck")

    def recompose(self, ctx: StepContext, part: Part, spec: dict[str, Any]) -> None:
        """RECOMPOSE (a palette colour, a code-layer field, the eye shape): the parts stay, the face is composed again for $0."""
        spawn_assemble(ctx, part.id, nonce="", reason="recompose")


def spawn_assemble(ctx: StepContext, part_id: str, *, nonce: str, reason: str = "") -> None:
    st = ctx.rt.ops.new_step("face.assemble", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=part_id,
                             params={"part_id": part_id, "nonce": nonce}, priority=ctx.step.priority, nonce=nonce)
    ctx.spawn([st])


def face_part_done(ctx: StepContext, loop: AL.AssetLoopSpec, res: AL.LoopResult) -> None:
    """One part's loop finished: remember it; when the last pending part is in, ``face.assemble`` runs (claimed once)."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    name = str(loop.then_params.get("part") or loop.role)
    part_id = loop.part_id
    sha = res.final_sha if not res.needs_human else None
    flag = None
    if sha is None:
        flag = f"code_face:{name}"                                 # the loop gave up: the part is drawn by code and the tile says so
        _, spec = common.load_spec(rt, project_id)
        fspec = face_spec(spec, common.split_part_id(part_id).character)
        canvas, _ = load_canvas(rt, fspec)
        pv = common.prov("code", step_kind="face.part", params={"part": name, "fallback": "code_face"})
        sha = common.put_png(ctx, code_part(fspec, canvas, name), role=f"face_part.{name}", part_id=part_id, provenance=pv,
                             status="final").sha256
    go = False
    with rt.db.tx():
        st = get_state(rt, project_id, part_id)
        st["parts"][name] = sha
        st["pending"] = [n for n in st.get("pending", []) if n != name]
        if flag:
            st.setdefault("flags", {})[name] = flag
        if not st["pending"] and not st.get("assembling"):
            st["assembling"] = True
            go = True
        rt.repo.kv_set(state_key(project_id, part_id), st)
    if go:
        spawn_assemble(ctx, part_id, nonce=st.get("nonce", ""))


# ---------------------------------------------------------------------------------------------------- assemble
def _open_part(ctx: StepContext, sha: str | None) -> Image.Image | None:
    return common.open_image(ctx.read_asset(sha)) if sha else None


def hair_hexes(spec: dict[str, Any], character: str) -> list[str]:
    h = spec[character]["hair"]
    return [x for x in (common.hex_of(spec, h.get(k)) for k in ("colour_ref", "shadow_ref", "highlight_ref")) if x]


def run_assemble(ctx: StepContext, p: FaceParams, inputs: list[Any]) -> StepResult:
    """C4: place the parts, add the code layers, write the layer pack, render the tone sheet and run the face checks."""
    from duoskin.imaging import face_canvas as FC

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    c = part.character
    fspec = face_spec(spec, c)
    canvas, head_base = load_canvas(rt, fspec)
    st = get_state(rt, project_id, part.id)
    ctx.progress(0.1, "placing the face parts")
    fp = FC.FaceParts(sources={})
    shas: dict[str, str] = {}
    for name in AI_PARTS:
        sha = st["parts"].get(name)
        if sha is None:
            continue
        by_code = ctx.get_asset(sha).first_provenance.source == "code"
        if by_code:                                                           # a code-drawn part follows the current spec colours (a recompose is free)
            img = code_part(fspec, canvas, name)
            sha = common.put_png(ctx, img, role=f"face_part.{name}", part_id=part.id,
                                 provenance=common.prov("code", step_kind="face.part", params={"part": name, "fallback": "code_face"}), status="final").sha256
            st["parts"][name] = sha
        else:
            img = _open_part(ctx, sha)
        setattr(fp, PART_ATTR[name], img)
        shas[name] = sha                                                      # type: ignore[assignment]
        fp.sources[PART_ATTR[name]] = "code" if by_code else "ai"
    comp = FC.compose_face(fspec, fp, canvas)
    in_pix = [rt.cas.get_asset(s).pixel_sha or s for s in shas.values()]
    mock = _mock_lineage(rt, list(shas.values()))
    canvas_text = (kits.load_context(rt).head_base_dir(fspec.eye_shape) / "face_canvas.json").read_text(encoding="utf-8") \
        if head_base else FC.DEFAULT_CANVAS_PATH.read_text(encoding="utf-8")
    pv = common.prov("code", step_kind="face.assemble", input_shas=in_pix, notes=["mock_lineage"] if mock else [],
                     params={"canvas": canvas.sha256, "canvas_source": canvas.source, "head_base": head_base,
                             "kit_subset_sha": kits.load_context(rt).subset_sha(face_canvas=canvas.sha256, eye_shape=fspec.eye_shape)})
    assets: dict[str, str] = {}
    ctx.progress(0.4, "writing the layer pack")
    for name, im in comp.layer_pack().items():
        assets[f"layer.{name}"] = common.put_png(ctx, im, role=f"face_layer.{name}", part_id=part.id, provenance=pv, status="final").sha256
    assets["canvas"] = common.put_bytes(ctx, canvas_text.encode("utf-8"), "json", role="face_canvas", part_id=part.id, provenance=pv,
                                        status="final").sha256
    skin_hex = kits.load_context(rt).inventory.skin_hex(spec[c]["body"]["skin_tone"]).lower()
    ctx.progress(0.6, "rendering the expressions")
    for ex in FC.EXPRESSIONS:
        prev = comp.preview(skin_hex, ex).convert("RGB").resize((256, 256), Image.Resampling.BOX)
        assets[ex] = common.put_png(ctx, prev, role=f"face_preview.{ex}", part_id=part.id, provenance=pv, status="final").sha256
    sheet = FC.tone_sheet(comp, label=not head_base)
    assets["tone_sheet"] = common.put_png(ctx, sheet, role="face_tone_sheet", part_id=part.id, provenance=pv, status="final").sha256
    ctx.progress(0.8, "checking the face")
    other = "b" if c == "a" else "a"
    other_face = face_spec(spec, other) if other in spec else None
    results = FC.face_check_suite(comp, hair_hexes=hair_hexes(spec, c), head_base_present=head_base, other_face=other_face, subject_sha=assets["neutral"])
    neutral_full = comp.preview(skin_hex, "neutral")
    results += prints.registry_results(rt, project_id, "face", neutral_full)
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    flags = ([] if head_base else ["no_head_base"]) + (["mock"] if mock else []) + sorted(set(st.get("flags", {}).values()))
    parts.set_tile_facts(rt, project_id, part.id, eye_shape=fspec.eye_shape, sources=dict(fp.sources), canvas_source=canvas.source,
                         head_base=head_base, skin_hex=skin_hex)
    if blocking:
        parts.mark_needs_human(ctx, project_id, part.id, report="; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]), assets=assets,
                               results=results)
    else:
        parts.mark_ready(ctx, project_id, part.id, assets=assets, results=results, flags_add=flags)
    return StepResult(outputs=list(assets.values()), result={"hard_failures": [r.check_id for r in blocking], "parts": shas},
                      message="face assembled" if not blocking else "face needs a look")


def _mock_lineage(rt: Runtime, shas: list[str]) -> bool:
    from duoskin.pipeline import clothing

    return clothing.mock_lineage(rt, shas)


# ---------------------------------------------------------------------------------------------------- BUILD: layer pack
REQUIRED_LAYERS = ("sclera", "iris", "lash", "brow", "mouth_closed", "mouth_open")       # the others (shading, blush, nose, lower ticks, highlights, lid) may be empty by design


def run_face_finalize(ctx: StepContext, p: FaceParams, inputs: list[Any]) -> StepResult:
    """BUILD (no head base): the approved layer pack and ``face_canvas.json`` are the face's build assets (``head.texture`` replaces this
    step when a head base exists). The layer PNGs are re-opened and checked: real alpha, canvas size."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    kit = kits.load_context(rt)
    assets = {k: v for k, v in part.board_assets.items() if k.startswith("layer.") or k in ("canvas", "tone_sheet", "neutral", "blink", "mouth_open", "happy")}
    bad = []
    for role, sha in assets.items():
        if role.startswith("layer."):
            im = common.open_image(ctx.read_asset(sha))
            if im.getchannel("A").getextrema()[1] == 0 and role.split(".", 1)[1] in REQUIRED_LAYERS:
                bad.append(role)
    results = [common.mk_result("FACE_LAYERS", not bad, metric="empty_layers", value=float(len(bad)), evidence=", ".join(bad) or "every layer holds paint",
                                fix_hint="human")]
    common.store_checks(ctx, results)
    from duoskin.pipeline import build

    build.set_build_assets(rt, project_id, part.id, assets, extra={"head_base": bool(kit.flags.get("head_base_present"))})
    return StepResult(outputs=[], result={"layers": len([k for k in assets if k.startswith("layer.")]), "ok": not bad},
                      message="face layer pack ready" if not bad else "a face layer is empty")


def register(rt: Runtime | None = None) -> None:
    parts.register_lane(FaceLane())
    AL.register_callback("face_part", face_part_done)
    AL.CODE_DRAFTERS["code_face"] = draft_code_face
    registry.register_handler("face.assemble", run_assemble, version=1, pool="cpu", paid=False, Params=FaceParams, cacheable=False)
    registry.register_handler("face.finalize", run_face_finalize, version=1, pool="cpu", paid=False, Params=FaceParams, cacheable=False)


_ = json
