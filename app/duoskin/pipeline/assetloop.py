"""The asset loop: every generated 2D asset goes through the same sub-graph (APP_SPEC §8.9, §8.10; bible §2.7, §19).

::

    img.draft (n <= 4 per request) -> img.gate_a (code checks + the free rung-1 fix) -> img.gate_b (L11 yes/no, one rule per call,
    top 2 survivors, hard rules confirmed by a 3-vote majority) -> img.rank
        survivors -> img.finalize (I0 Sunburst edit of the best draft) -> img.recheck (Gate A + A_DRIFT + Gate B on the final)
        none      -> ``engine.ladder.next_action``: regenerate (new nonce, n up to 8, sent as requests of <= 4), the next technique,
                     a masked repair (L10 -> I11 -> paste-back -> ring check) or rung 6, a NEEDS_HUMAN tile with the best version so far

Design rules this module keeps:

* **A step is a unit of paid work.** The draft and finalize steps are content-addressed (their inputs and the compiled prompt are in
  the cache key), so a retry or a repeated request never pays twice; the gate, rank and recheck steps read their input from the
  previous step (``from_step``), are not engine-cached and keep their own content cache for the paid Gate B calls.
* **The prompt is compiled when the step is created** (``prompts.compiler`` + ``prompts.dna_router``: at most 2 DNA fields from the right
  character, at most 5 MUST lines) and travels in the step params with its sha256, so the cache key and the provenance are exact.
* **SOFT checks never climb the ladder** (``engine.ladder.next_action`` sees HARD failures only); an approved part is never touched.
* The best version so far is always kept (``Part.ladder.best_asset_sha``).
* A lane ends its loop through a **callback** (``register_callback``): the loop does not know what a print or a face is.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from PIL import Image
from pydantic import Field

from duoskin.checks import policy
from duoskin.checks.model import CheckResult
from duoskin.engine import ladder as L
from duoskin.engine import registry
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import StepResult
from duoskin.models.common import CharKey, PartId, Sha256, Strict
from duoskin.models.job import Step
from duoskin.pipeline import common, llmcall

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.assetloop")

LOOP_STREAMS = ("pipeline", "drill", "regression")


# ---------------------------------------------------------------------------------------------------- models
class TechOverride(Strict):
    """What one technique of a loop needs differently from the loop's defaults (``None`` keeps the default; ``""`` for ``mask`` means no mask)."""

    images: list[Sha256] | None = None
    mask: str | None = None
    slots: dict[str, Any] | None = None


class AssetLoopSpec(Strict):
    """The parameters of one asset loop (APP_SPEC §8.10, plus the additive fields every lane needs)."""

    part_id: PartId
    character: CharKey
    role: str                                  # "print", "iris_imgR", "hair_front", "acc_front", "badge", "view_left", ...
    template_id: str                           # a bible id of the primary technique, e.g. "I2.print"
    slots: dict[str, Any] = Field(default_factory=dict)   # filled from the spec by pipeline code (never raw user text)
    images: list[Sha256] = Field(default_factory=list)    # ordered references (Image 1 first); the guide first when masked;
                                                          # the style sheet is the asset's OWN character's (APP_SPEC S35)
    mask: Sha256 | None = None
    n_drafts: int = 4
    finalize: bool = True                      # False for Recraft SVGs and for Gate 1 drafts
    gate_a: list[str] = Field(default_factory=list)
    gate_b_hard: list[str] = Field(default_factory=list)
    gate_b_soft: list[str] = Field(default_factory=list)
    technique_ladder: str = "print"            # key of ``TECHNIQUE_LADDERS``
    keep_alternatives: int = 0
    stream: Literal["pipeline", "drill", "regression"] = "pipeline"
    # ---- additive
    then: str = ""                             # the callback that takes the result (``register_callback``)
    then_params: dict[str, Any] = Field(default_factory=dict)
    palette_hex: list[str] = Field(default_factory=list)  # colours the asset may use (A_PALETTE and the free snap fix)
    expected: dict[str, Any] = Field(default_factory=dict)  # lane facts for Gate A (guide colour, component count, placed scale ...)
    gate_b_slots: dict[str, dict[str, str]] = Field(default_factory=dict)   # rule id -> statement slots
    gate_b_facts: str = ""                     # measured facts handed to the judge
    background: Literal["transparent", "opaque"] = "transparent"
    reference_shas: list[Sha256] = Field(default_factory=list)   # images only the judge sees (the concept crop)
    nonce: str = ""
    kit_subset_sha: str = ""
    available: list[str] = Field(default_factory=list)       # providers the technique ladder may use (key routing, bible D22)
    overrides: dict[str, TechOverride] = Field(default_factory=dict)   # a technique that needs other references, mask or inputs (hair: I4k, I5g)


class LoopState(Strict):
    technique_index: int = 0
    n_total: int = 4
    regen_count: int = 0
    masked_repairs: int = 0
    code_fix_tried: bool = False
    finalize_attempts: int = 0
    attempts: int = 0                          # draft chains started
    history: list[str] = Field(default_factory=list)
    rejected_phashes: list[str] = Field(default_factory=list)   # perceptual hashes of rejected drafts (A_PHASH, Reimagine dedupe)
    best_sha: Sha256 | None = None
    nonce: str = ""


class PromptSnap(Strict):
    """The compiled prompt as it travels in the step params (the exact text, its hash and the call parameters)."""

    template_id: str
    template_version: int
    text: str
    sha256: Sha256
    size: str | None = None
    background: str = "transparent"
    image1_role: str = "none"
    images_roles: list[str] = Field(default_factory=list)
    model: str = ""
    quality: str = "low"
    dna_fields: list[str] = Field(default_factory=list)
    provider_fields: dict[str, Any] = Field(default_factory=dict)


class LoopStepParams(Strict):
    loop: AssetLoopSpec
    state: LoopState = Field(default_factory=LoopState)
    technique: str = "I2"
    prompt: PromptSnap | None = None
    n_total: int = 4
    from_step: str = ""                        # the previous step of the chain (its outputs and result are this step's input)
    draft_sha: Sha256 | None = None            # finalize/recheck: the chosen draft
    final_step: str = ""                       # recheck: the finalize step
    alternatives: list[Sha256] = Field(default_factory=list)   # runner-up drafts kept for the tile (faces: the top 2-3)
    failed: list[dict[str, Any]] = Field(default_factory=list)  # repair: the failed Gate B rules with their locations
    stage: str = "draft"


# ---------------------------------------------------------------------------------------------------- techniques
@dataclass(frozen=True)
class Technique:
    name: str
    template_id: str
    provider: str                              # "openai" | "recraft" | "code"
    finalizable: bool = True
    vector: bool = False


TECHNIQUES: dict[str, Technique] = {
    "I2": Technique("I2", "I2.print", "openai"),
    "I2f": Technique("I2f", "I2.print_frame", "openai"),
    "I2s": Technique("I2s", "I2.shoe_decal", "openai"),
    "R2": Technique("R2", "R2.print", "recraft", finalizable=False, vector=True),
    "R1": Technique("R1", "R1.face_part", "recraft", finalizable=False, vector=True),
    "I3": Technique("I3", "I3.face_part", "openai"),
    "code_face": Technique("code_face", "", "code", finalizable=False),
    "I4": Technique("I4", "I4.hair_front", "openai"),
    "I4k": Technique("I4k", "I4k.hair_kit_first", "openai"),
    "I5": Technique("I5", "I5.accessory_front", "openai"),
    "I5f": Technique("I5f", "I5.accessory_frame", "openai"),
    "I5g": Technique("I5g", "I5g.accessory_guided", "openai"),
    "I6": Technique("I6", "I6.badge_art", "openai"),
    "I6f": Technique("I6f", "I6.badge_frame", "openai"),
    "R2_badge": Technique("R2_badge", "R2.print", "recraft", finalizable=False, vector=True),
    "I10": Technique("I10", "I10.side_view", "openai"),
}

#: the per-asset technique ladders (bible §19, rung 4 options in order); the first entry is the default route
TECHNIQUE_LADDERS: dict[str, list[str]] = {
    "print": ["I2", "R2"],
    "print_frame": ["I2f", "R2"],
    "shoe_print": ["I2s"],
    "face_part": ["R1", "I3", "code_face"],
    "hair": ["I4", "I4k"],
    "accessory": ["I5", "I5g"],
    "accessory_frame": ["I5f", "I5g"],
    "badge": ["I6", "R2_badge"],
    "badge_frame": ["I6f", "R2_badge"],
    "view": ["I10"],
}

_CALLBACKS: dict[str, Callable[..., None]] = {}


def register_callback(name: str, fn: Callable[..., None]) -> None:
    """``fn(ctx, loop, result)`` takes the finished loop of a lane (``LoopResult``)."""
    _CALLBACKS[name] = fn


@dataclass
class LoopResult:
    final_sha: str | None = None
    draft_sha: str | None = None
    alternatives: list[str] = field(default_factory=list)
    results: list[CheckResult] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    needs_human: bool = False
    best_sha: str | None = None
    report: str = ""
    technique: str = ""
    cost_usd: float = 0.0


def images_of(loop: AssetLoopSpec, technique: str) -> list[str]:
    """The reference images of ``technique`` (Image 1 first): the loop's own, or what the loop lists for that technique."""
    ov = loop.overrides.get(technique)
    return list(ov.images) if ov and ov.images is not None else list(loop.images)


def mask_of(loop: AssetLoopSpec, technique: str) -> str | None:
    ov = loop.overrides.get(technique)
    if ov and ov.mask is not None:
        return ov.mask or None
    return loop.mask


def slots_of(loop: AssetLoopSpec, technique: str) -> dict[str, Any]:
    ov = loop.overrides.get(technique)
    return dict(ov.slots) if ov and ov.slots is not None else dict(loop.slots)


def ladder_for(loop: AssetLoopSpec) -> list[str]:
    """The technique ladder of this loop with the techniques whose provider is not available skipped (D22)."""
    names = TECHNIQUE_LADDERS.get(loop.technique_ladder, [loop.technique_ladder])
    avail = set(loop.available) if loop.available else {"openai", "recraft", "code"}
    avail |= {"code"}
    return [n for n in names if TECHNIQUES[n].provider in avail]


# ---------------------------------------------------------------------------------------------------- prompt compile
def compile_snap(rt: Runtime, spec: dict[str, Any], loop: AssetLoopSpec, technique: str, *, project_id: str | None = None) -> PromptSnap:
    """Compile and lint the prompt of ``technique`` for this loop (CHK-P01); the router picks <= 2 DNA fields of the own character."""
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts import registry as preg
    from duoskin.prompts.catalog import default_ctx

    tech = TECHNIQUES[technique]
    if not tech.template_id:
        raise StepFailure(f"technique {technique} has no prompt", kind="bad_request", billed="no")
    ctx_kit = kits.load_context(rt)
    cctx = default_ctx(ctx_kit.inventory)
    cp = compiler.compile(tech.template_id, spec, loop.character, slots_of(loop, technique), cctx)
    meta = preg.get(tech.template_id).meta
    route = meta.route.get("draft") or meta.route.get("style") or meta.route.get("default") or {}
    models = {}
    if project_id:
        pins = rt.repo.get_project(project_id).pins
        models = dict(pins.models) if pins else {}
    model = str(route.get("model", ""))
    if tech.provider == "openai" and models.get("image_draft"):
        model = models["image_draft"]
    size = cp.provider_fields.get("size")
    return PromptSnap(template_id=cp.template_id, template_version=cp.template_version, text=cp.text, sha256=cp.sha256,
                      size=str(size) if size else None, background=str(cp.provider_fields.get("background") or loop.background),
                      image1_role=str(cp.provider_fields.get("image1_role") or "none"), images_roles=list(cp.images), model=model,
                      quality=str(route.get("quality", "low")), dna_fields=list(cp.dna_fields), provider_fields=dict(cp.provider_fields))


# ---------------------------------------------------------------------------------------------------- starting a loop
def _new(rt: Runtime, kind: str, job_id: str, loop: AssetLoopSpec, params: LoopStepParams, *, project_id: str | None = None,
         deps: list[str] | None = None, inputs: list[str] | None = None, nonce: str = "", priority: int = 100) -> Step:
    return rt.ops.new_step(kind, job_id=job_id, project_id=project_id, part_id=loop.part_id, params=params.model_dump(mode="json"),
                           inputs=inputs or [], deps=deps or [], nonce=nonce, priority=priority)


def draft_chain(rt: Runtime, job_id: str, project_id: str, loop: AssetLoopSpec, state: LoopState, spec: dict[str, Any], *,
                deps: list[str] | None = None, priority: int = 100) -> list[Step]:
    """The four steps of one draft attempt: draft -> gate_a -> gate_b -> rank (ids and ``from_step`` links filled in)."""
    names = ladder_for(loop)
    if not names:
        raise StepFailure("no technique of this asset can run: its providers are not available", kind="bad_request", billed="no")
    technique = names[min(state.technique_index, len(names) - 1)]
    snap = compile_snap(rt, spec, loop, technique, project_id=project_id) if TECHNIQUES[technique].template_id else None
    n_total = state.n_total
    nonce = state.nonce or loop.nonce
    base = {"loop": loop, "state": state, "technique": technique, "prompt": snap, "n_total": n_total}
    pj = {"project_id": project_id, "priority": priority}
    draft = _new(rt, "img.draft", job_id, loop, LoopStepParams(**base, stage="draft"), deps=deps, inputs=images_of(loop, technique)
                 + ([mask_of(loop, technique)] if mask_of(loop, technique) else []), nonce=nonce, **pj)
    gate_a = _new(rt, "img.gate_a", job_id, loop, LoopStepParams(**base, from_step=draft.id, stage="gate_a"), deps=[draft.id], **pj)
    gate_b = _new(rt, "img.gate_b", job_id, loop, LoopStepParams(**base, from_step=gate_a.id, stage="gate_b"), deps=[gate_a.id], **pj)
    rank = _new(rt, "img.rank", job_id, loop, LoopStepParams(**base, from_step=gate_b.id, stage="rank"), deps=[gate_b.id], **pj)
    return [draft, gate_a, gate_b, rank]


def start_loop(rt: Runtime, job_id: str, project_id: str, spec: dict[str, Any], loop: AssetLoopSpec, *, nonce: str = "",
               deps: list[str] | None = None, priority: int = 100, state: LoopState | None = None) -> list[Step]:
    """Create and spawn the first draft chain of ``loop``. Returns the steps."""
    st = state or LoopState(n_total=loop.n_drafts, nonce=nonce or loop.nonce)
    if nonce:
        st = st.model_copy(update={"nonce": nonce})
    steps = draft_chain(rt, job_id, project_id, loop, st, spec, deps=deps, priority=priority)
    rt.scheduler.spawn(job_id, steps)
    return steps


# ---------------------------------------------------------------------------------------------------- Gate A
@dataclass
class GateAInput:
    im: Image.Image
    loop: AssetLoopSpec
    sha: str
    rejected: list[str]


def _g_alpha(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    return C.gate_a_alpha(g.im, subject_sha=g.sha, allow_checker=bool(g.loop.expected.get("allow_checker")))


def _g_components(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    want = g.loop.expected.get("components", 1)
    exp = tuple(want) if isinstance(want, (list, tuple)) else int(want)
    return [C.check_components(g.im, exp, subject_sha=g.sha)]            # type: ignore[arg-type]


def _g_margin(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    return [C.check_margin(g.im, subject_sha=g.sha, min_margin=g.loop.expected.get("margin"))]


def _g_palette(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    if not g.loop.palette_hex:
        return []
    return [C.check_palette(g.im, g.loop.palette_hex, subject_sha=g.sha, exclude_hex=g.loop.expected.get("exclude_hex", []))]


def _g_stroke(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    ex = g.loop.expected
    return [C.check_stroke(g.im, placed_scale=float(ex.get("placed_scale", 1.0)), min_px=ex.get("stroke_min_px"),
                           min_frac_of_bbox=ex.get("stroke_min_frac"), subject_sha=g.sha)]


def _g_ocr(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import ocr

    return [ocr.check_no_text(g.im, subject_sha=g.sha)]


def _g_symmetry(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    if g.loop.expected.get("symmetric") is False:
        return []
    return [C.check_symmetry(g.im, subject_sha=g.sha, about=str(g.loop.expected.get("symmetry_about", "bbox")))]


def _g_single_colour(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    return [C.check_single_colour(g.im, expected_hex=g.loop.expected.get("line_hex"), subject_sha=g.sha)]


def _g_highlight(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    return [C.check_no_highlight(g.im, subject_sha=g.sha)]


def _g_guide_left(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    hx = g.loop.expected.get("guide_hex")
    return [C.check_guide_left(g.im, hx, subject_sha=g.sha)] if hx else []


def _g_halo_free(g: GateAInput) -> list[CheckResult]:
    return []


def _g_badge(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import checks as C

    return [C.check_badge(g.im, subject_sha=g.sha)]


def _g_phash(g: GateAInput) -> list[CheckResult]:
    from duoskin.imaging import similarity

    if not g.rejected:
        return []
    return [similarity.dedupe_check(g.im, [int(h, 16) for h in g.rejected], subject_sha=g.sha)]


def _g_sil_guide(g: GateAInput) -> list[CheckResult]:
    """A_SIL_GUIDE for hair: the hair box is editable, the lower 55% of the head's front face and the head cube stay as drawn."""
    from duoskin.checks.runner import run_check
    from duoskin.imaging import checks as C

    box = g.loop.expected.get("head_box")
    if not box:
        return []
    x0, y0, x1, y1 = box
    arr = np.asarray(g.im.convert("RGB")).astype(int)
    grey = np.array([int(g.loop.expected.get("guide_hex", "#9a9a9a").lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)])
    head = np.zeros(arr.shape[:2], bool)
    head[y0:y1, x0:x1] = True
    protect_y = y0 + round((y1 - y0) * 0.45)
    low = head.copy()
    low[:protect_y] = False
    fg = (np.abs(arr - grey).max(axis=2) < 40) & low
    guide = low
    return [run_check("A_SIL_GUIDE", g.sha, lambda: C.check_sil_guide(fg, guide, rows=(protect_y, y1)))]


def _g_views(g: GateAInput) -> list[CheckResult]:
    return []


def _g_pass(g: GateAInput) -> list[CheckResult]:
    return []


GATE_A_IMPL: dict[str, Callable[[GateAInput], list[CheckResult]]] = {
    "A_SIZE": _g_pass, "A_ALPHA": _g_alpha, "A_HALO": _g_halo_free, "A_COMPONENTS": _g_components, "A_MARGIN": _g_margin,
    "A_PALETTE": _g_palette, "A_STROKE": _g_stroke, "A_OCR": _g_ocr, "A_GLYPH": _g_pass, "A_SYMMETRY": _g_symmetry,
    "A_SINGLE_COLOUR": _g_single_colour, "A_HIGHLIGHT": _g_highlight, "A_GUIDE_LEFT": _g_guide_left, "A_BADGE": _g_badge,
    "A_PHASH": _g_phash, "A_SIL_GUIDE": _g_sil_guide, "A_VIEWS": _g_views, "A_SVG": _g_pass, "A_SENTINEL": _g_pass,
}


def run_gate_a(ids: list[str], im: Image.Image, loop: AssetLoopSpec, sha: str, rejected: list[str]) -> list[CheckResult]:
    """Every Gate A check named by the template, each fail-closed through ``checks.runner.run_check``."""
    from duoskin.checks.runner import fail_closed

    g = GateAInput(im, loop, sha, rejected)
    out: list[CheckResult] = []
    for cid in ids:
        impl = GATE_A_IMPL.get(cid)
        if impl is None:
            continue
        try:
            out.extend(impl(g))
        except Exception as exc:  # noqa: BLE001 - fail closed
            out.append(fail_closed(cid, f"{type(exc).__name__}: {exc}", sha))
    return out


def code_fix(im: Image.Image, loop: AssetLoopSpec, hints: set[str]) -> tuple[Image.Image, list[str]]:
    """Rung 1 (free, deterministic): the code auto-fix of bible §2.5 for the fix hints of the failing checks."""
    from duoskin.imaging import checks as C

    actions: list[str] = []
    out = im
    if hints & {"code_alpha_cleanup", "code_palette_snap"}:
        pal = loop.palette_hex if "code_palette_snap" in hints or loop.palette_hex else None
        out, rep = C.cleanup_alpha_asset(out, pal)
        actions += rep.actions
    if "code_recrop" in hints:
        out = C.canonical_frame(out, margin=0.10)
        actions.append("recrop")
    return out, actions


def _blocking(r: CheckResult) -> bool:
    return policy.is_blocking(r) and not r.passed


def _summary(results: list[CheckResult]) -> list[dict[str, Any]]:
    return [{"id": r.check_id, "kind": r.kind, "passed": r.passed, "status": r.status, "ran": r.ran, "ev": (r.evidence or "")[:140],
             "hint": r.fix_hint} for r in results]


def _score(results: list[CheckResult]) -> tuple[int, int]:
    """(hard passes, soft passes): the ranking key after the hard gate (best version so far, bible §19)."""
    hard = sum(1 for r in results if policy.is_blocking(r) and r.passed)
    soft = sum(1 for r in results if r.kind == "soft" and r.passed)
    return hard, soft


# ---------------------------------------------------------------------------------------------------- draft
def _provenance(rt: Runtime, ctx: StepContext, tech: Technique, p: LoopStepParams, **kw: Any):
    source = "code" if tech.provider == "code" else ("mock" if common.is_mock(rt, tech.provider) else tech.provider)
    prompt = p.prompt
    refs = [ctx.get_asset(sha) for sha in images_of(p.loop, p.technique)]
    base: dict[str, Any] = {"provider": None if tech.provider == "code" else tech.provider, "model": (prompt.model if prompt else None),
                            "prompt_id": prompt.template_id if prompt else None,
                            "prompt_version": prompt.template_version if prompt else None,
                            "prompt_sha256": prompt.sha256 if prompt else None,
                            "input_shas": [a.pixel_sha or a.sha256 for a in refs],
                            "mask_sha": (ctx.get_asset(mask_of(p.loop, p.technique)).pixel_sha if mask_of(p.loop, p.technique) else None),
                            "nonce": ctx.step.nonce}
    base.update(kw)
    prm = dict(base.get("params") or {})
    if p.loop.kit_subset_sha:
        prm["kit_subset_sha"] = p.loop.kit_subset_sha
    base["params"] = prm
    return common.prov(source, stream=p.loop.stream, **base)


def _named(sha: str, ctx: StepContext, name: str):
    from duoskin.providers.openai_images import NamedPng

    return NamedPng(name, ctx.read_asset(sha))


def _openai_request(ctx: StepContext, p: LoopStepParams, *, n: int, final: bool = False):
    from duoskin.providers.openai_images import ImageRequest, NamedPng

    snap = p.prompt
    assert snap is not None
    images = tuple(NamedPng(f"image{i + 1}.png", ctx.read_asset(sha)) for i, sha in enumerate(images_of(p.loop, p.technique)))
    mask_sha = mask_of(p.loop, p.technique)
    mask = NamedPng("mask.png", ctx.read_asset(mask_sha)) if mask_sha else None
    size = snap.size or "1024x1024"
    bg = "transparent" if snap.background == "transparent" else "opaque"
    role = snap.image1_role if snap.image1_role in ("edit_target", "reference") else "edit_target"
    return ImageRequest(model=snap.model, prompt=snap.text, size=size, quality=snap.quality, background=bg, n=n, images=images,
                        image1_role=role, mask=mask, nonce=ctx.step.nonce, tag=snap.template_id.split(".")[0])


def _normalise(raw: bytes) -> bytes:
    im = common.open_image(raw)
    return common.png_bytes(im)


def draft_openai(ctx: StepContext, p: LoopStepParams, tech: Technique) -> list[dict[str, Any]]:
    """The OpenAI draft route (I2, I3, I4, I5, I6, I10): ``run_many`` sends ``ceil(n / 4)`` requests of <= 4 images."""
    adapter = ctx.provider("openai")
    n_total = max(1, min(32, p.n_total))
    req = _openai_request(ctx, p, n=min(4, n_total))
    call_ctx = llmcall.make_call_ctx(ctx, req.tag)
    if req.images:
        res = adapter.run_many(req, n_total, call_ctx) if n_total > 4 else adapter.edit(req, call_ctx)
    else:
        res = adapter.run_many(req, n_total, call_ctx) if n_total > 4 else adapter.generate(req, call_ctx)
    cost = res.cost or {}
    common.record_cost(ctx, cost, f"images.edit:{req.tag}", fallback_provider="openai")
    out = []
    for i, raw in enumerate(res.images):
        out.append({"raw": raw, "png": _normalise(raw), "request_id": (res.request_ids[min(i // 4, len(res.request_ids) - 1)]
                                                                     if res.request_ids else res.request_id),
                    "alpha": bool(res.alpha_present[i]) if i < len(res.alpha_present) else None})
    return out


def draft_recraft(ctx: StepContext, p: LoopStepParams, tech: Technique) -> list[dict[str, Any]]:
    """The Recraft vector route (R1, R2): SVG -> sanitizer -> palette snap -> two-pass matte render (no pixel chroma key)."""
    from duoskin.imaging import palette as P
    from duoskin.imaging import svg as SVG
    from duoskin.pipeline import kits
    from duoskin.providers.recraft import VectorRequest

    snap = p.prompt
    assert snap is not None
    adapter = ctx.provider("recraft")
    rt = ctx.rt
    size = snap.size or "1024x1024"
    w, h = (int(x) for x in size.split("x"))
    palette = list(p.loop.palette_hex) or ["#2b2b33"]
    sentinel = str(snap.provider_fields.get("sentinel_hex") or P.choose_sentinel(palette) or "#00ff00").lower()
    manifest = kits.load_context(rt).manifest
    style_id = manifest.get("recraft_face_style_id") if tech.name == "R1" else None
    model = snap.model
    if tech.name == "R1":
        model = "recraftv4_styles_vector" if style_id else "recraftv4_1_utility_vector"
    elif not model or not model.startswith("recraft"):
        model = "recraftv4_1_vector"
    req = VectorRequest(model=model, prompt=snap.text, size=size, n=max(1, min(6, p.n_total)), style_id=style_id,
                        colors=tuple(P.hex_to_rgb(x) for x in palette[:5]), background_rgb=P.hex_to_rgb(sentinel), nonce=ctx.step.nonce,
                        tag=snap.template_id.split(".")[0])
    res = adapter.generate(req, llmcall.make_call_ctx(ctx, req.tag))
    common.record_cost(ctx, res.cost, f"images.generate:{req.tag}", fallback_provider="recraft")
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(res.svgs):
        entry: dict[str, Any] = {"raw": raw, "svg": raw, "request_id": res.request_id}
        try:
            part = SVG.svg_to_part(raw.decode("utf-8", "replace"), palette, (w, h), sentinel=sentinel)
            entry["png"] = common.png_bytes(part.image)
            entry["svg_ok"] = True
            entry["svg_paths"] = part.path_count
        except Exception as exc:  # noqa: BLE001 - A_SVG: the sanitizer or the renderer refused it; the candidate is rejected
            entry["png"] = None
            entry["svg_ok"] = False
            entry["svg_error"] = f"{type(exc).__name__}: {exc}"[:200]
        out.append(entry)
    return out


def _draft_impl(ctx: StepContext, p: LoopStepParams, tech: Technique) -> list[dict[str, Any]]:
    if tech.provider == "openai":
        return draft_openai(ctx, p, tech)
    if tech.provider == "recraft":
        return draft_recraft(ctx, p, tech)
    fn = CODE_DRAFTERS.get(tech.name)
    if fn is None:
        raise StepFailure(f"no drafter for technique {tech.name}", kind="bad_request", billed="no")
    return fn(ctx, p, tech)


#: code-parametric drafters registered by lanes (the last rung of the face ladder): ``fn(ctx, p, tech) -> list[{"png": bytes}]``
CODE_DRAFTERS: dict[str, Callable[..., list[dict[str, Any]]]] = {}


def run_draft(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    tech = TECHNIQUES[p.technique]
    ctx.progress(0.05, f"drafting ({tech.name})")
    cands = _draft_impl(ctx, p, tech)
    shas: list[str] = []
    info: list[dict[str, Any]] = []
    for i, c in enumerate(cands):
        pv = _provenance(ctx.rt, ctx, tech, p, request_id=c.get("request_id"), params={"n_total": p.n_total, "index": i,
                                                                                         "technique": tech.name,
                                                                                         "size": p.prompt.size if p.prompt else None})
        raw_sha = None
        if c.get("raw") is not None and c["raw"] != c.get("png"):
            ext = "svg" if c.get("svg") is not None else "png"
            raw_sha = common.put_bytes(ctx, c["raw"], ext, role="draft_raw", part_id=p.loop.part_id, provenance=pv).sha256
        if c.get("png") is None:                                   # a rejected SVG: keep the raw file, make no PNG candidate
            info.append({"sha": None, "raw": raw_sha, "svg_ok": False, "svg_error": c.get("svg_error", ""), "i": i})
            continue
        pv2 = pv.model_copy(update={"raw_sha256": raw_sha} if raw_sha else {})
        asset = common.put_bytes(ctx, c["png"], "png", role=f"draft.{p.loop.role}", part_id=p.loop.part_id, provenance=pv2)
        shas.append(asset.sha256)
        info.append({"sha": asset.sha256, "raw": raw_sha, "svg_ok": c.get("svg_ok", True), "i": i, "alpha": c.get("alpha")})
    return StepResult(outputs=shas, result={"candidates": info, "technique": tech.name, "n_total": p.n_total, "from": "draft"},
                      message=f"{len(shas)} draft(s) by {tech.name}")


def estimate_draft(p: LoopStepParams) -> float:
    from duoskin.providers import pricing

    tech = TECHNIQUES[p.technique]
    if tech.provider == "openai":
        q = p.prompt.quality if p.prompt else "low"
        return float(pricing.estimate_openai_image(quality=q, n=p.n_total, n_input_images=len(images_of(p.loop, p.technique)),
                                                   prompt_chars=len(p.prompt.text) if p.prompt else 1200).usd)
    if tech.provider == "recraft":
        return float(pricing.estimate_recraft(p.prompt.model if p.prompt and p.prompt.model.startswith("recraft") else "recraftv4_1_vector",
                                              n=max(1, min(6, p.n_total))).usd)
    return 0.0


def cache_fields_draft(p: LoopStepParams, inputs: list[Any]) -> dict[str, Any]:
    pr = p.prompt
    return {"model": pr.model if pr else None, "prompt_id": pr.template_id if pr else None,
            "prompt_version": pr.template_version if pr else None, "prompt_sha256": pr.sha256 if pr else None,
            "kit_subset_sha": p.loop.kit_subset_sha or None, "capability_flags": {"background": pr.background if pr else None}}


# ---------------------------------------------------------------------------------------------------- gate A
def _prev_outputs(ctx: StepContext, step_id: str) -> tuple[list[str], dict[str, Any]]:
    s = ctx.rt.repo.get_step(step_id)
    return list(s.outputs), dict(s.result)


def run_gate_a_step(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    shas, prev = _prev_outputs(ctx, p.from_step)
    loop = p.loop
    out_shas: list[str] = []
    per: list[dict[str, Any]] = []
    tech = TECHNIQUES[p.technique]
    for i, sha in enumerate(shas):
        ctx.check_cancel()
        im = common.open_image(ctx.read_asset(sha))
        ids_a = [] if tech.provider == "code" else loop.gate_a    # code-drawn parts are checked where they are used (the assembled face)
        results = run_gate_a(ids_a, im, loop, sha, p.state.rejected_phashes)
        fixed_sha, actions = sha, []
        blocking = [r for r in results if _blocking(r)]
        hints = {r.fix_hint for r in blocking if r.fix_hint.startswith("code_")}
        if blocking and hints and not p.state.code_fix_tried:                      # rung 1: free, deterministic, at most once per candidate
            im2, actions = code_fix(im, loop, hints)
            fixed_png = common.png_bytes(im2)
            if fixed_png != ctx.read_asset(sha):
                pv = _provenance(ctx.rt, ctx, tech, p, input_shas=[ctx.get_asset(sha).pixel_sha or sha],
                                 params={"code_fix": actions, "from": sha[:12]})
                fixed_sha = common.put_bytes(ctx, fixed_png, "png", role=f"draft_fixed.{loop.role}", part_id=loop.part_id,
                                             provenance=pv.model_copy(update={"source": "code" if pv.source != "mock" else "mock"})).sha256
                results = run_gate_a(ids_a, im2, loop, fixed_sha, p.state.rejected_phashes)
                results.append(CheckResult(check_id="A_AUTOFIX", fm_ids=[], kind="soft", passed=True, metric="rung1",
                                           evidence="code fix: " + ", ".join(actions)))   # type: ignore[call-arg]
        ids = common.store_checks(ctx, [r for r in results if r.check_id != "A_AUTOFIX"])
        out_shas.append(fixed_sha)
        hard_fail = [r for r in results if _blocking(r)]
        per.append({"sha": fixed_sha, "orig": sha, "hard_ok": not hard_fail, "score": list(_score(results)),
                    "fails": [r.check_id for r in hard_fail], "results": _summary(results), "check_ids": ids, "fixed": actions})
    for entry in prev.get("candidates", []):
        if entry.get("sha") is None:                                                # an SVG the sanitizer refused (A_SVG)
            per.append({"sha": None, "hard_ok": False, "fails": ["A_SVG"], "score": [0, 0], "results": [{"id": "A_SVG", "kind": "hard",
                        "passed": False, "status": "failed", "ran": True, "ev": entry.get("svg_error", "")}], "check_ids": [], "fixed": []})
    survivors = sum(1 for e in per if e["hard_ok"])
    return StepResult(outputs=[s for s in out_shas], result={"per": per, "survivors": survivors, "total": len(per),
                                                             "technique": p.technique, "from": "gate_a"},
                      message=f"{survivors}/{len(per)} pass Gate A")


# ---------------------------------------------------------------------------------------------------- gate B
def _gb_cache_key(sha_pixels: str, rule_ids: list[str], votes: int) -> str:
    from duoskin.models.common import sha256_of
    from duoskin.models.llm_io import RULES_VERSION

    return "gateb:" + sha256_of({"img": sha_pixels, "rules": sorted(rule_ids), "votes": votes, "v": RULES_VERSION})[:40]


def judge_images(ctx: StepContext, sha: str, loop: AssetLoopSpec) -> list[Image.Image]:
    im = common.open_image(ctx.read_asset(sha))
    views = llmcall.vlm_views(im)
    refs = []
    for r in loop.reference_shas[:1]:
        try:
            refs.append(common.open_image(ctx.read_asset(r)))
        except Exception:  # noqa: BLE001, S110 - a missing reference only means the matches_concept rule has less to look at
            pass
    return views + refs


def gate_b_for(ctx: StepContext, sha: str, loop: AssetLoopSpec, *, hard_only: bool = False, votes: int = 1,
               counter: llmcall.CallCounter | None = None) -> list[CheckResult]:
    """L11 on one candidate: every rule of the loop (hard and soft), the hard IP rules in their own calls. Cached by image content."""
    rules = list(loop.gate_b_hard) + ([] if hard_only else list(loop.gate_b_soft))
    if not rules:
        return []
    rt = ctx.rt
    pix = ctx.get_asset(sha).pixel_sha or sha
    key = _gb_cache_key(pix, rules, votes)
    hit = rt.repo.kv_get(key)
    if hit is not None:
        return [CheckResult.model_validate(d) for d in hit]
    reqs = llmcall.rule_requests(rules, loop.gate_b_slots)
    res = llmcall.run_rules(ctx, reqs, judge_images(ctx, sha, loop), measured_facts=loop.gate_b_facts, subject_sha=sha, votes=votes,
                            counter=counter)
    rt.repo.kv_set(key, [r.model_dump(mode="json") for r in res.results])
    common.store_checks(ctx, res.results)
    return list(res.results)


def run_gate_b_step(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    _, prev = _prev_outputs(ctx, p.from_step)
    per = [dict(e) for e in prev.get("per", [])]
    counter = llmcall.CallCounter()
    # rank the Gate A survivors; judge the top 2 (the next 2 only when both fail): bible §2.7
    order = sorted((e for e in per if e["hard_ok"] and e["sha"]), key=lambda e: (-e["score"][1], -e["score"][0], per.index(e)))
    judged: list[dict[str, Any]] = []
    rules_n = len(p.loop.gate_b_hard) + len(p.loop.gate_b_soft)
    if TECHNIQUES[p.technique].provider == "code":      # a code-drawn asset has nothing for a judge to find: no paid call
        rules_n = 0
    batch_start = 0
    while batch_start < len(order) and rules_n:
        batch = order[batch_start:batch_start + 2]
        for e in batch:
            ctx.check_cancel()
            res = gate_b_for(ctx, e["sha"], p.loop, counter=counter)
            failing = [r.check_id for r in res if _blocking(r)]
            if failing:                       # a hard verdict that fails must survive a 3-vote majority before it costs a candidate (bible 2.7, CHK-P12)
                again = {r.check_id: r for r in gate_b_for(ctx, e["sha"], p.loop.model_copy(update={"gate_b_hard": failing, "gate_b_soft": []}), hard_only=True,
                                                           votes=3, counter=counter)}
                res = [again.get(r.check_id, r) for r in res]
            e["gb"] = _summary(res)
            hard_fail = [r for r in res if _blocking(r)]
            e["gb_fails"] = [r.check_id for r in hard_fail]
            e["gb_soft_pass"] = sum(1 for r in res if r.kind == "soft" and r.passed)
            e["hard_ok"] = e["hard_ok"] and not hard_fail
            e["fail_loc"] = _fail_locations(res, ctx)
            judged.append(e)
        if any(e["hard_ok"] for e in batch):
            break
        batch_start += 2
    if not rules_n:
        judged = order
    survivors = [e for e in per if e["hard_ok"] and e.get("sha")]
    return StepResult(outputs=[], result={"per": per, "survivors": len(survivors), "judged": len(judged), "total": len(per),
                                          "technique": p.technique, "from": "gate_b"},
                      message=f"{len(survivors)} candidate(s) pass Gate B")


_LOCATION = re.compile(r"^\w+ \[([a-z_]+)\]:")


def _fail_locations(results: list[CheckResult], ctx: StepContext) -> dict[str, str]:
    """Where the judge says each failing rule is (the closed ``location`` values of a verdict; the repair mask seed, bible 16.1)."""
    out: dict[str, str] = {}
    for r in results:
        m = None if r.passed else _LOCATION.match(r.evidence or "")
        if m:
            out[r.check_id] = m.group(1)
    return out


def estimate_gate_b(p: LoopStepParams) -> float:
    n = len(p.loop.gate_b_hard) + len(p.loop.gate_b_soft)
    if TECHNIQUES[p.technique].provider == "code":
        return 0.0
    return llmcall.estimate_llm_usd("L11.asset_checker", calls=2 * n, image_count=2) * 0.2 if n else 0.0


# ---------------------------------------------------------------------------------------------------- rank
def _merge_best(per_a: list[dict[str, Any]], per_b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return per_b or per_a


def run_rank_step(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    _, prev = _prev_outputs(ctx, p.from_step)
    per = list(prev.get("per", []))
    loop, state = p.loop, p.state
    project_id = ctx.step.project_id or ""
    survivors = [e for e in per if e.get("hard_ok") and e.get("sha")]
    for e in per:                                    # remember rejected drafts for A_PHASH on the next attempt (Reimagine dedupe)
        if e.get("sha") and not e["hard_ok"]:
            h = _phash_hex(ctx, e["sha"])
            if h:
                state.rejected_phashes.append(h)
    state.rejected_phashes = state.rejected_phashes[-24:]
    if survivors:
        ranked = sorted(survivors, key=lambda e: (-(e["score"][1] + e.get("gb_soft_pass", 0)), -e["score"][0], per.index(e)))
        best = ranked[0]
        alts = [e["sha"] for e in ranked[1:1 + max(0, loop.keep_alternatives)]]
        state = state.model_copy(update={"best_sha": best["sha"]})
        _update_ladder(rt, project_id, loop.part_id, best=best["sha"])
        tech = TECHNIQUES[p.technique]
        if loop.finalize and tech.finalizable:
            steps = _finalize_chain(rt, ctx, p, state, best["sha"], alts)
            ctx.spawn(steps)
            return StepResult(result={"best": best["sha"], "alternatives": alts, "next": "finalize", "technique": p.technique},
                             message="best draft chosen; finalizing")
        res = LoopResult(final_sha=best["sha"], draft_sha=best["sha"], alternatives=alts, best_sha=best["sha"], technique=p.technique,
                         results=_results_of(best))
        _finish(ctx, loop, res)
        return StepResult(result={"best": best["sha"], "alternatives": alts, "next": "done", "technique": p.technique},
                         message="best version chosen")
    # no survivor: the fix ladder
    return _ladder_step(ctx, p, state, per)


def _results_of(entry: dict[str, Any]) -> list[CheckResult]:
    out = []
    for r in list(entry.get("results", [])) + list(entry.get("gb", [])):
        try:
            out.append(CheckResult(check_id=r["id"], fm_ids=[], kind=r["kind"], passed=r["passed"], metric="", evidence=r.get("ev", ""),
                                   ran=r.get("ran", True), fix_hint=r.get("hint", "none")))   # type: ignore[call-arg]
        except Exception:  # noqa: BLE001, S112
            continue
    return out


def _phash_hex(ctx: StepContext, sha: str) -> str | None:
    try:
        from duoskin.imaging import similarity

        return f"{similarity.phash(common.open_image(ctx.read_asset(sha))):x}"
    except Exception:  # noqa: BLE001
        return None


def _update_ladder(rt: Runtime, project_id: str, part_id: str, *, best: str | None = None, action: Any = None, outcome: str = "") -> None:
    if not project_id:
        return

    def apply(part: Any) -> None:
        st = part.ladder
        if action is not None:
            st = L.record(st, action, outcome=outcome, best=best)
        elif best:
            st = st.model_copy(update={"best_asset_sha": best})
        part.ladder = st

    try:
        rt.repo.mutate_part(project_id, part_id, apply)
    except Exception:  # noqa: BLE001 - the part may have been replaced by a change
        log.debug("could not update the ladder of %s", part_id)


def _ladder_step(ctx: StepContext, p: LoopStepParams, state: LoopState, per: list[dict[str, Any]]) -> StepResult:
    rt = ctx.rt
    loop = p.loop
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, loop.part_id)
    budget = rt.budget.budget(project_id)
    results: list[CheckResult] = []
    best_entry = max((e for e in per if e.get("sha")), key=lambda e: tuple(e["score"]), default=None)
    if best_entry:
        results = _results_of(best_entry)
    if not any(_blocking(r) for r in results):                       # candidates all died on something we cannot name: say so
        results.append(CheckResult(check_id="A_NONE", fm_ids=[], kind="hard", passed=False, metric="survivors", ran=False,
                                   evidence="no candidate passed", fix_hint="regenerate"))   # type: ignore[call-arg]
    total = max(1, len(per))
    pass_rate = sum(1 for e in per if e.get("hard_ok")) / total
    near = bool(best_entry and best_entry.get("fail_loc") and len(best_entry.get("gb_fails", [])) == 1 and not best_entry.get("fails"))
    tech = TECHNIQUES[p.technique]
    next_est = estimate_draft(p) + 0.05
    outcome = L.LoopOutcome.from_results(results, best=best_entry["sha"] if best_entry else part.ladder.best_asset_sha,
                                         code_fix_tried=True, batch_pass_rate=pass_rate, near_miss=near,
                                         masked_repairs=state.masked_repairs, regenerated_once=state.regen_count >= 1, n=state.n_total,
                                         next_est=next_est, report=_report(per))
    action = L.next_action(part, outcome, budget)
    names = ladder_for(loop)
    state = state.model_copy(update={"history": [*state.history, f"{tech.name}:{action.action}"], "attempts": state.attempts + 1})
    best_sha = outcome.best
    if best_sha:
        state = state.model_copy(update={"best_sha": best_sha})
    _update_ladder(rt, project_id, loop.part_id, best=best_sha, action=action, outcome="fail")
    job_id = ctx.step.job_id
    spec = _spec_for(rt, project_id, loop)
    if isinstance(action, L.NextTechnique):
        idx = action.index
        while idx < len(names) and not _technique_ok(rt, names[idx]):
            idx += 1
        if idx < len(names):
            nstate = state.model_copy(update={"technique_index": idx, "n_total": loop.n_drafts, "regen_count": 0,
                                              "nonce": common.new_nonce()})
            ctx.spawn(draft_chain(rt, job_id, project_id, loop, nstate, spec, priority=ctx.step.priority))
            return StepResult(result={"ladder": "next_technique", "technique": names[idx]}, message=f"trying {names[idx]}")
        action = L.Human(best=best_sha, report=_report(per))
    if isinstance(action, L.Regenerate):
        nstate = state.model_copy(update={"regen_count": state.regen_count + 1, "n_total": min(8, action.n), "nonce": common.new_nonce()})
        ctx.spawn(draft_chain(rt, job_id, project_id, loop, nstate, spec, priority=ctx.step.priority))
        return StepResult(result={"ladder": "regenerate", "n": nstate.n_total}, message="regenerating the asset (new nonce)")
    if isinstance(action, L.MaskedEdit) and best_entry:
        steps = _repair_chain(rt, ctx, p, state, best_entry)
        ctx.spawn(steps)
        return StepResult(result={"ladder": "masked_edit"}, message="repairing with a masked edit")
    res = LoopResult(needs_human=True, best_sha=best_sha, report=_report(per), results=results, technique=p.technique,
                     flags=["needs_human"])
    _finish(ctx, loop, res)
    return StepResult(result={"ladder": "human", "best": best_sha}, message="needs a human look")


def _technique_ok(rt: Runtime, name: str) -> bool:
    prov = TECHNIQUES[name].provider
    return prov == "code" or common.provider_available(rt, prov)


def _report(per: list[dict[str, Any]]) -> str:
    fails: dict[str, int] = {}
    for e in per:
        for f in list(e.get("fails", [])) + list(e.get("gb_fails", [])):
            fails[f] = fails.get(f, 0) + 1
    if not fails:
        return "no candidate was usable"
    return "; ".join(f"{k} failed on {v} of {len(per)}" for k, v in sorted(fails.items(), key=lambda kv: -kv[1])[:4])


def _spec_for(rt: Runtime, project_id: str, loop: AssetLoopSpec) -> dict[str, Any]:
    _, spec = common.load_spec(rt, project_id)
    return spec


def _finish(ctx: StepContext, loop: AssetLoopSpec, res: LoopResult) -> None:
    cb = _CALLBACKS.get(loop.then)
    if cb is None:
        log.warning("loop %s has no callback %r", loop.part_id, loop.then)
        return
    cb(ctx, loop, res)


# ---------------------------------------------------------------------------------------------------- finalize and recheck
def _finalize_chain(rt: Runtime, ctx: StepContext, p: LoopStepParams, state: LoopState, draft_sha: str, alts: list[str]) -> list[Step]:
    loop = p.loop
    snap = _finalize_snap(rt, ctx, p, draft_sha)
    common_kw = {"job_id": ctx.step.job_id, "project_id": ctx.step.project_id, "part_id": loop.part_id, "priority": ctx.step.priority}
    base = {"loop": loop, "state": state, "technique": p.technique, "prompt": snap, "n_total": 1, "draft_sha": draft_sha, "alternatives": alts}
    fin = rt.ops.new_step("img.finalize", params=LoopStepParams(**base, stage="finalize").model_dump(mode="json"), inputs=[draft_sha],
                          nonce=state.nonce, **common_kw)
    rec = rt.ops.new_step("img.recheck", params=LoopStepParams(**base, final_step=fin.id, from_step=fin.id, stage="recheck").model_dump(mode="json"),
                          deps=[fin.id], **common_kw)
    return [fin, rec]


def _finalize_snap(rt: Runtime, ctx: StepContext, p: LoopStepParams, draft_sha: str) -> PromptSnap:
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts import registry as preg
    from duoskin.prompts.catalog import default_ctx

    loop = p.loop
    asset = {"I2": "print", "I2f": "print", "I2s": "print", "R2": "print", "I3": "face_part", "R1": "face_part", "I4": "hair", "I4k": "hair", "I5": "accessory",
             "I5f": "accessory", "I5g": "accessory", "I6": "badge", "I6f": "badge", "R2_badge": "badge", "I10": "accessory"}.get(p.technique, "print")
    _, spec = common.load_spec(rt, ctx.step.project_id or "")
    kit = kits.load_context(rt)
    transparent = loop.background == "transparent"
    cp = compiler.compile("I0.finalize", spec, loop.character, {"asset": asset, "transparent": transparent, "style_ref": False},
                          default_ctx(kit.inventory))
    meta = preg.get("I0.finalize").meta
    route = meta.route.get("final", {})
    pins = rt.repo.get_project(ctx.step.project_id or "").pins
    model = (pins.models.get("image_final") if pins else None) or str(route.get("model", ""))
    return PromptSnap(template_id=cp.template_id, template_version=cp.template_version, text=cp.text, sha256=cp.sha256,
                      size=p.prompt.size if p.prompt else None, background="transparent" if transparent else "opaque",
                      image1_role="edit_target", images_roles=list(cp.images), model=model, quality=str(route.get("quality", "high")),
                      dna_fields=[], provider_fields=dict(cp.provider_fields))


def run_finalize(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    """I0: the Sunburst edit of the chosen draft. Image 1 is the edit target; a transparent draft goes with an explicit all-editable
    mask (U26), so its transparency is never read as an implicit mask."""
    from duoskin.imaging import masks as M
    from duoskin.providers.openai_images import ImageRequest, NamedPng

    snap = p.prompt
    assert snap is not None and p.draft_sha
    draft_bytes = ctx.read_asset(p.draft_sha)
    im = common.open_image(draft_bytes)
    size = f"{im.width}x{im.height}"
    mask = None
    if snap.background == "transparent":
        mask = NamedPng("mask.png", M.make_mask(np.ones((im.height, im.width), bool)))
    req = ImageRequest(model=snap.model, prompt=snap.text, size=size, quality=snap.quality, background="transparent" if snap.background == "transparent" else "opaque",
                       n=1, images=(NamedPng("image1.png", draft_bytes),), image1_role="edit_target", mask=mask, nonce=ctx.step.nonce,
                       tag="I0")
    res = ctx.provider("openai").edit(req, llmcall.make_call_ctx(ctx, "I0"))
    common.record_cost(ctx, res.cost, "images.edit:I0", fallback_provider="openai")
    tech = TECHNIQUES[p.technique]
    pv = _provenance(ctx.rt, ctx, tech, p, request_id=res.request_id, input_shas=[ctx.inputs[0].pixel_sha or p.draft_sha],
                     params={"size": size, "finalize": True, "quality": snap.quality})
    raw = res.images[0]
    raw_sha = common.put_bytes(ctx, raw, "png", role="final_raw", part_id=p.loop.part_id, provenance=pv).sha256
    final = common.put_bytes(ctx, _normalise(raw), "png", role=f"final.{p.loop.role}", part_id=p.loop.part_id,
                             provenance=pv.model_copy(update={"raw_sha256": raw_sha}))
    return StepResult(outputs=[final.sha256], result={"final": final.sha256, "from": "finalize"}, message="final drawn")


def estimate_finalize(p: LoopStepParams) -> float:
    from duoskin.providers import pricing

    return float(pricing.estimate_openai_image(quality=p.prompt.quality if p.prompt else "high", n=1, n_input_images=1,
                                               prompt_chars=len(p.prompt.text) if p.prompt else 600).usd)


def run_recheck(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    """Gate A + A_DRIFT + Gate B on the final. Drift failing twice shows draft and final side by side; the default is the draft (FM X4)."""
    from duoskin.checks.runner import run_check
    from duoskin.imaging import checks as C

    shas, _ = _prev_outputs(ctx, p.from_step)
    final_sha = shas[0]
    loop = p.loop
    draft_sha = p.draft_sha or ""
    final_im = common.open_image(ctx.read_asset(final_sha))
    draft_im = common.open_image(ctx.read_asset(draft_sha))
    results = run_gate_a(loop.gate_a, final_im, loop, final_sha, [])
    drift = run_check("A_DRIFT", final_sha, lambda: C.check_drift(final_im, draft_im, subject_sha=final_sha))
    results.append(drift)
    flags: list[str] = []
    alts = list(p.alternatives)
    use = final_sha
    if not drift.passed:
        if p.state.finalize_attempts < 1:                                            # one re-run with a new nonce
            st = p.state.model_copy(update={"finalize_attempts": p.state.finalize_attempts + 1, "nonce": common.new_nonce()})
            steps = _finalize_chain(ctx.rt, ctx, p.model_copy(update={"loop": loop}), st, draft_sha, alts)
            ctx.spawn(steps)
            return StepResult(result={"drift": "retry"}, message="the final drifted from the draft; redrawing once")
        use = draft_sha                                                              # second failure: the draft is what the user approves
        flags.append("finalize_drift")
        results = [r for r in results if r.check_id != "A_DRIFT"] + [drift.model_copy(update={"kind": "soft"})]
    else:
        gb = gate_b_for(ctx, final_sha, loop)
        results += gb
    hard = [r for r in results if _blocking(r)]
    ids = common.store_checks(ctx, results)
    res = LoopResult(final_sha=use, draft_sha=draft_sha, alternatives=alts, results=results, flags=flags, best_sha=use,
                     technique=p.technique, needs_human=bool(hard))
    if hard:
        # the final broke a HARD rule that the draft passed: the ladder starts again from the draft (the best version is kept)
        res.final_sha = draft_sha
        res.flags.append("final_failed_checks")
        res.needs_human = not all(r.passed for r in run_gate_a(loop.gate_a, draft_im, loop, draft_sha, []) if _blocking(r))
    _finish(ctx, loop, res)
    return StepResult(result={"final": use, "hard_fails": [r.check_id for r in hard], "check_ids": ids, "flags": flags,
                              "results": _summary(results), "from": "recheck"}, message="final checked")


def estimate_recheck(p: LoopStepParams) -> float:
    n = len(p.loop.gate_b_hard)
    return llmcall.estimate_llm_usd("L11.asset_checker", calls=n, image_count=2) * 0.2 if n else 0.0


# ---------------------------------------------------------------------------------------------------- repair (rung 2)
def _repair_chain(rt: Runtime, ctx: StepContext, p: LoopStepParams, state: LoopState, entry: dict[str, Any]) -> list[Step]:
    loop = p.loop
    st = state.model_copy(update={"masked_repairs": state.masked_repairs + 1})
    failed = [{"id": r, "location": entry.get("fail_loc", {}).get(r, "whole")} for r in entry.get("gb_fails", [])]
    rep = rt.ops.new_step("img.repair", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=loop.part_id,
                          params=LoopStepParams(loop=loop, state=st, technique=p.technique, prompt=p.prompt, n_total=1,
                                                draft_sha=entry["sha"], failed=failed, stage="repair").model_dump(mode="json"),
                          inputs=[entry["sha"]], nonce=common.new_nonce(), priority=ctx.step.priority)
    ga = rt.ops.new_step("img.gate_a", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=loop.part_id,
                         params=LoopStepParams(loop=loop, state=st, technique=p.technique, prompt=p.prompt, from_step=rep.id,
                                               stage="gate_a").model_dump(mode="json"), deps=[rep.id], priority=ctx.step.priority)
    gb = rt.ops.new_step("img.gate_b", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=loop.part_id,
                         params=LoopStepParams(loop=loop, state=st, technique=p.technique, prompt=p.prompt, from_step=ga.id,
                                               stage="gate_b").model_dump(mode="json"), deps=[ga.id], priority=ctx.step.priority)
    rk = rt.ops.new_step("img.rank", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=loop.part_id,
                         params=LoopStepParams(loop=loop, state=st, technique=p.technique, prompt=p.prompt, from_step=gb.id,
                                               stage="rank").model_dump(mode="json"), deps=[gb.id], priority=ctx.step.priority)
    return [rep, ga, gb, rk]


_LOC_BOX = {"top_left": (0, 0, 1, 1), "top": (1, 0, 2, 1), "top_right": (2, 0, 3, 1), "left": (0, 1, 1, 2), "centre": (1, 1, 2, 2),
            "right": (2, 1, 3, 2), "bottom_left": (0, 2, 1, 3), "bottom": (1, 2, 2, 3), "bottom_right": (2, 2, 3, 3)}


def location_mask(shape: tuple[int, int], location: str) -> np.ndarray:
    """A coarse editable mask for a verdict location (3 x 3 grid, ``whole`` = everything)."""
    h, w = shape
    m = np.zeros((h, w), bool)
    if location not in _LOC_BOX:
        m[:] = True
        return m
    c0, r0, c1, r1 = _LOC_BOX[location]
    m[int(r0 * h / 3):int(r1 * h / 3), int(c0 * w / 3):int(c1 * w / 3)] = True
    return m


def run_repair(ctx: StepContext, p: LoopStepParams, inputs: list[Any]) -> StepResult:
    """Rung 2: L10 writes the repair plan, I11 edits under a mask, then paste-back and the ring check (APP_SPEC §8.9)."""
    from duoskin.imaging import masks as M
    from duoskin.models.llm_io import RepairPlan
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts.catalog import default_ctx
    from duoskin.providers.openai_images import ImageRequest, NamedPng

    rt = ctx.rt
    loop = p.loop
    draft = common.open_image(ctx.read_asset(p.draft_sha or ""))
    failed = p.failed or []
    fv = "; ".join(f"{f['id']} at {f['location']}" for f in failed) or "unknown"
    masks_txt = ", ".join(sorted(_LOC_BOX) + ["whole"])
    plan: RepairPlan = llmcall.ask_llm(ctx, "L10.repair_writer", {
        "asset": f"{loop.role} via {p.technique}", "failed_verdicts": fv, "measured_facts": loop.gate_b_facts or "none",
        "masks": masks_txt, "attempts_so_far": ", ".join(p.state.history) or "none"}, images=[draft], counter=llmcall.CallCounter())
    op = next((o for o in plan.ops if o.method in ("masked_edit", "global_edit")), None)
    if op is None or plan.give_up_reason:
        # nothing to edit: pass the draft on; rank will find no survivor again and the ladder moves on
        asset = ctx.get_asset(p.draft_sha or "")
        return StepResult(outputs=[asset.sha256], result={"candidates": [{"sha": asset.sha256, "i": 0}], "repair": "no_edit",
                                                          "from": "draft"}, message="no masked edit was possible")
    editable = location_mask((draft.height, draft.width), op.mask_id if op.method == "masked_edit" else "whole")
    kit = kits.load_context(rt)
    _, spec = common.load_spec(rt, ctx.step.project_id or "")
    asset_noun = {"I2": "print", "I2f": "print", "I2s": "print", "R2": "print", "I3": "face_part", "R1": "face_part", "I4": "hair", "I4k": "hair", "I5": "accessory", "I5f": "accessory", "I5g": "accessory", "I6": "badge", "I6f": "badge", "I10": "accessory"}.get(p.technique, "print")
    edits = [e for e in [op.edit_prompt] if e]
    cp = compiler.compile("I11.repair", spec, loop.character, {"asset": asset_noun, "form": "masked" if op.method == "masked_edit" else "global",
                                                              "subject_sentence": plan.subject_sentence or "The same asset with the fix applied.",
                                                              "edit": edits or ["Fix the named problem."], "keep": list(op.keep),
                                                              "transparent": loop.background == "transparent"}, default_ctx(kit.inventory))
    snap = p.prompt
    model = snap.model if snap else ""
    req = ImageRequest(model=model, prompt=cp.text, size=f"{draft.width}x{draft.height}", quality="high",
                       background="transparent" if loop.background == "transparent" else "opaque", n=1,
                       images=(NamedPng("image1.png", ctx.read_asset(p.draft_sha or "")),), image1_role="edit_target",
                       mask=NamedPng("mask.png", M.make_mask(editable)), nonce=ctx.step.nonce, tag="I11")
    res = ctx.provider("openai").edit(req, llmcall.make_call_ctx(ctx, "I11"))
    common.record_cost(ctx, res.cost, "images.edit:I11", fallback_provider="openai")
    out = common.open_image(res.images[0])
    pasted = M.paste_back(draft, out, editable)
    ring = M.check_paste(draft, out, editable)
    tech = TECHNIQUES[p.technique]
    pv = _provenance(ctx.rt, ctx, tech, p, params={"repair": op.method, "mask": op.mask_id, "ring_ok": ring.passed}, prompt_id=cp.template_id,
                     prompt_version=cp.template_version, prompt_sha256=cp.sha256)
    asset = common.put_bytes(ctx, common.png_bytes(pasted), "png", role=f"draft_repaired.{loop.role}", part_id=loop.part_id, provenance=pv)
    common.store_checks(ctx, [ring])
    cands = [{"sha": asset.sha256, "i": 0, "ring_ok": ring.passed}] if ring.passed else []
    return StepResult(outputs=[asset.sha256] if ring.passed else [], result={"candidates": cands, "repair": op.method, "from": "draft"},
                      message="masked repair done" if ring.passed else "the repair moved the surroundings; discarded")


def estimate_repair(p: LoopStepParams) -> float:
    from duoskin.providers import pricing

    return float(pricing.estimate_openai_image(quality="high", n=1, n_input_images=1).usd) + llmcall.estimate_llm_usd("L10.repair_writer")


# ---------------------------------------------------------------------------------------------------- registration
def register(rt: Runtime | None = None) -> None:
    registry.register_handler("img.draft", run_draft, version=1, pool="api", paid=True, provider="openai", Params=LoopStepParams,
                              estimate=estimate_draft, cache_fields=cache_fields_draft)
    registry.register_handler("img.gate_a", run_gate_a_step, version=1, pool="cpu", paid=False, Params=LoopStepParams, cacheable=False)
    registry.register_handler("img.gate_b", run_gate_b_step, version=1, pool="api", paid=True, provider="anthropic", Params=LoopStepParams,
                              estimate=estimate_gate_b, cacheable=False)
    registry.register_handler("img.rank", run_rank_step, version=1, pool="cpu", paid=False, Params=LoopStepParams, cacheable=False)
    registry.register_handler("img.finalize", run_finalize, version=1, pool="api", paid=True, provider="openai", Params=LoopStepParams,
                              estimate=estimate_finalize, cache_fields=cache_fields_draft)
    registry.register_handler("img.recheck", run_recheck, version=1, pool="api", paid=True, provider="anthropic", Params=LoopStepParams,
                              estimate=estimate_recheck, cacheable=False)
    registry.register_handler("img.repair", run_repair, version=1, pool="api", paid=True, provider="openai", Params=LoopStepParams,
                              estimate=estimate_repair, cacheable=False)

