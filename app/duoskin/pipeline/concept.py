"""Concept previews, Gate 1 and the concept lock (APP_SPEC §9.2, §10.3; bible §10, §13).

::

    plan.select ──► concept.char (per spec and character: I1 front+back, Gate A, Gate B)  x 6
                        │
                 concept.assemble (per spec: the 4-up sheet, A_LEAK, duo rules, what the kits cannot build)  x 3
                        │
                   concept.gate  (opens Gate 1: three plan tiles with front and back of both characters)
                        │  the person approves one tile
                 concept.lock  (C3: I0 final redraw, A_DRIFT, palette extracted from the approved picture, DNA card v1 locked,
                                the per-duo style sheet and the part crops) ──► ``plan.start_parts`` (the part board)

Every Gate 1 decision is applied by ``apply_concept`` (registered for ``GateKind.CONCEPT``):

* **Approve** needs a READY tile (and ``choice="ack:CON-04"`` when the picture shows something the kits cannot build as drawn).
* **Reimagine** (target a, b or both) draws the chosen characters again with a **new nonce** and the pHashes of the drafts that were
  seen (A_PHASH), through I1 on the code guide. The character that is not reimagined stays exactly as it is.
* **Change** goes to the change flow (``pipeline.change``): L7 reads the text, the person confirms, and an edit of the picture runs **I1e on
  the chosen draft of the affected character only**; I1 runs only when L7 asks to regenerate.
* **New plan** records the rejected plans for ``<avoid>`` and starts a new PLAN job.

No private asset loop here, deliberately: the generic loop of ``pipeline.assetloop`` knows one part id per loop and no concept ids, no
I1/I1e technique and no tight-mask ladder, so the concept loop is small and private: draw n drafts, paste back only where the mask allows
(A_PASTE), Gate A in code (A_SIL_GUIDE per figure, A_OCR, A_SWATCH, A_PHASH), Gate B (L11) on the two best, and a short ladder when
every draft fails only the silhouette check: the **tight mask** (body boxes grown by a few pixels), then 8 drafts, then NEEDS_HUMAN.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from PIL import Image
from pydantic import Field

from duoskin.checks import gate_b as GB
from duoskin.checks import plan_rules as PR
from duoskin.checks import policy, runner
from duoskin.checks.model import CheckResult
from duoskin.engine import registry as reg
from duoskin.engine.cache import CachedResult, pixel_sha
from duoskin.engine.errors import StepFailure
from duoskin.engine.gates import ApplyContext, ApplyResult, GateError, allowed_actions_for
from duoskin.engine.registry import StepResult
from duoskin.imaging import checks as C
from duoskin.imaging import guides, masks, ocr, similarity
from duoskin.imaging import palette as P
from duoskin.models.common import CharKey, Strict, sha256_of, utcnow
from duoskin.models.gate import Gate, GateAction, GateKind, GateTile, TileState
from duoskin.models.job import JobKind, Step
from duoskin.models.project import Project, Stage
from duoskin.models.spec_record import SpecRecord
from duoskin.pipeline import brief as BR
from duoskin.pipeline import common
from duoskin.pipeline import lint as LI
from duoskin.pipeline import plan as PL
from duoskin.providers.openai_images import ImageRequest, NamedPng

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.concept")

CHARS: tuple[str, str] = ("a", "b")
BG = guides.BG_HEX
SLOT_NAMES = ("front", "back")
SIZE = f"{guides.CANVAS_W}x{guides.CANVAS_H}"
DRAFTS = 4                         # drafts per request (the template's draft n)
REGENERATE_DRAFTS = 8              # the last rung of the ladder
JUDGED = 2                         # Gate B looks at the two best drafts
ALTERNATIVES = 3
TIGHT_GROW_PX = 3                  # how far the tight mask grows the body boxes (A_SIL_GUIDE allows 3% of the body area outside)
RULES_PER_CALL = 5
NO_SHEET_IMAGE2 = "Image 2 = house style reference; match its rendering only."   # the I1e template's second image, dropped when no sheet exists
ACK_TOKEN = "ack:CON-04"
PALETTE_PLANNED = "palette:planned"
CONCEPT_HARD_RULES = ("cn_blocky_body", "cn_front_face", "cn_back_view", "cn_views_match", "cn_flat_clothing",
                      "ip_no_brand", "ip_no_known_character", "ip_no_text", "ip_age_appropriate")
CONCEPT_SOFT_RULES = ("cn_accessories_listed", "cn_hair_in_box", "cn_restraint")
WARNING_TEXT = {
    "A_SWATCH": "A colour in the picture differs from the plan. The final colours are taken from the picture you approve.",
    "A_PHASH": "This new picture looks close to one you did not choose.",
    "cn_accessories_listed": "The picture shows an accessory that is not in the plan.",
    "cn_hair_in_box": "The hair in the picture may be bigger than the hair kit can build.",
    "cn_restraint": "The picture looks a little busy. Small details may disappear at a small size.",
    "dj_same_world": "The two characters may not look like they belong to the same world.",
    "dj_anchor_visible": "The shared detail may not be visible on both characters.",
    "CHK-G1-11": "The two characters look quite similar.",
}
SKIN_FALLBACK = "#F0C8A0"
SLEEVES = {"none": "sleeveless", "short": "short", "three_quarter": "three_quarter", "long": "long"}
LEGS = {"mini": "short", "above_knee": "above_knee", "knee": "knee", "midi": "knee", "full": "full"}
BLOCKS = {"solid": "solid", "contrast_sleeves": "raglan_split", "raglan_split": "raglan_split", "horizontal_band": "colour_block",
          "vertical_split": "colour_block", "yoke": "colour_block"}


# ---------------------------------------------------------------------------------------------------- parameters
class ConceptStep(Strict):
    project_id: str
    plan_set_id: str
    slot: int = 0


class CharParams(ConceptStep):
    spec_id: str
    char: CharKey
    mode: Literal["i1", "i1e"] = "i1"
    fix_sentence: str = ""
    base_sha: str = ""                          # i1e: the chosen draft that is edited (Image 1)
    region_hint: str = "none"
    mask_mode: Literal["auto", "figure", "tight", "head"] = "auto"
    reason: str = "draw"                        # draw | reimagine | change | leak
    generation: int = 0


class AssembleParams(ConceptStep):
    spec_id: str


class GateParams(ConceptStep):
    slots: list[int] = Field(default_factory=list)
    refresh: bool = False


class LockParams(ConceptStep):
    spec_id: str
    palette_choice: Literal["picture", "planned"] = "picture"
    acknowledged: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------------- state in the key-value store
def state_key(psid: str, slot: int, c: str) -> str:
    return f"concept:{psid}:{slot}:{c}"


def sheet_key(psid: str, slot: int) -> str:
    return f"conceptsheet:{psid}:{slot}"


def get_state(rt: Runtime, psid: str, slot: int, c: str) -> dict[str, Any]:
    return dict(rt.repo.kv_get(state_key(psid, slot, c)) or {})


def put_state(rt: Runtime, psid: str, slot: int, c: str, st: dict[str, Any]) -> None:
    rt.repo.kv_set(state_key(psid, slot, c), st)


def get_sheet(rt: Runtime, psid: str, slot: int) -> dict[str, Any]:
    return dict(rt.repo.kv_get(sheet_key(psid, slot)) or {})


def put_sheet(rt: Runtime, psid: str, slot: int, doc: dict[str, Any]) -> None:
    rt.repo.kv_set(sheet_key(psid, slot), doc)


def shown_records(rt: Runtime, project_id: str, plan_set_id: str) -> list[SpecRecord]:
    """The plans of a plan set that Gate 1 shows, in slot order (slot = rank)."""
    recs = [r for r in PL.plan_records(rt, project_id, plan_set_id) if r.status in ("shown", "approved")]
    return sorted(recs, key=lambda r: (r.rank if r.rank is not None else r.plan_index, r.plan_index))


# ---------------------------------------------------------------------------------------------------- the code guide and its masks
def figure_from_spec(spec: dict[str, Any], c: str) -> guides.FigureSpec:
    """The colour-block figure of one character: what the I1 guide is painted with (from the spec's palette, never from a model)."""
    from duoskin.models import kitenums

    pal = common.palette_map(spec)
    ch = spec[c]
    top, bottom = ch["top"], ch["bottom"]
    inv = kitenums.current_inventory()
    tone = inv.skin_tones.get(ch["body"]["skin_tone"])
    skin = getattr(tone, "hex", None) or SKIN_FALLBACK

    def hx(ref: str | None, default: str | None = None) -> str | None:
        return pal.get(str(ref)) or default

    base = hx(top["base_ref"], "#888888") or "#888888"
    bottom_base = hx(bottom["base_ref"], "#555555") or "#555555"
    swatches = [x for x in (hx(ch["hair"]["colour_ref"]), base, bottom_base, hx(bottom["shoes"]["base_ref"]),
                            hx(top.get("trim_ref")) or hx(top.get("second_ref"))) if x]
    return guides.FigureSpec(skin=skin, top_base=base, bottom_base=bottom_base, top_second=hx(top.get("second_ref")),
                             legwear=hx(bottom.get("legwear_ref")) if bottom.get("legwear") != "bare" else None,
                             shoes=hx(bottom["shoes"]["base_ref"]), sleeve=SLEEVES.get(top["sleeve"], "long"),    # type: ignore[arg-type]
                             hem=top["hem"], leg=LEGS.get(bottom["leg"], "full"),                                  # type: ignore[arg-type]
                             block_layout=BLOCKS.get(top["block_layout"], "solid"), swatches=tuple(swatches))      # type: ignore[arg-type]


def build_guide(spec: dict[str, Any], c: str) -> guides.Guide:
    return guides.guide_concept_char(figure_from_spec(spec, c))


def tight_editable(guide: guides.Guide, grow: int = TIGHT_GROW_PX) -> np.ndarray:
    """The tight mask: everything above the neckline stays editable (hair, head, hats), below it only the body boxes grown by ``grow``
    pixels. It keeps a drawing inside the silhouette the code guide fixed (the I1 ladder's first retry, bible §10.3)."""
    from scipy import ndimage

    out = np.zeros_like(guide.editable)
    for name, sl in guide.slots.items():
        x0, y0, x1, _y1 = sl.editable_box
        out[y0:guides.NECK_Y, x0:x1] = True
        body = guide.body_masks[name]
        out |= ndimage.binary_dilation(body, structure=np.ones((3, 3), bool), iterations=grow) & guide.editable
    return out


def head_editable(guide: guides.Guide, grow: int = TIGHT_GROW_PX) -> np.ndarray:
    """The head cubes only (grown by ``grow`` pixels). Used under the mock image provider, which repaints the whole editable area with one flat
    colour: with the body left alone the drafts keep the guide's exact colours and silhouette, so the checks run on real pictures."""
    from scipy import ndimage

    out = np.zeros_like(guide.editable)
    for head in guide.head_masks.values():
        out |= ndimage.binary_dilation(head, structure=np.ones((3, 3), bool), iterations=grow)
    for body in guide.body_masks.values():
        out &= ~body                                    # the grown edge never covers the shoulders
    return out & guide.editable


def editable_for(guide: guides.Guide, mask_mode: str) -> np.ndarray:
    if mask_mode == "head":
        return head_editable(guide)
    return tight_editable(guide) if mask_mode == "tight" else guide.editable


def house_sheet(rt: Runtime) -> bytes | None:
    """The newest house-style sheet (Image 2 of I1 and I1e), or None when the install has none (the ``@s0`` variant of I1 then runs)."""
    v = PL.house_style_version(rt)
    if not v:
        return None
    path = rt.paths.kits_dir / "style" / f"house_style_v{v}.png"
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------------------------------------------- one candidate
@dataclass
class Cand:
    index: int
    png: bytes                                   # the pasted-back picture, opaque RGB
    raw_sha: str
    sha: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    hard_ok: bool = True
    phash_close: bool = False
    judged: list[CheckResult] = field(default_factory=list)
    judged_ok: bool = True
    swatch_de: float = 0.0

    @property
    def soft_passes(self) -> int:
        return sum(1 for r in [*self.checks, *self.judged] if r.kind == "soft" and r.passed)

    def key(self) -> tuple[int, int, float, int]:
        return (int(self.phash_close), -self.soft_passes, self.swatch_de, self.index)


def blocking_failures(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if policy.is_blocking(r) and not r.passed]


BG_TOL = 12                        # a pixel is figure when any channel is farther than this from the flat background


def fg_mask(im: Image.Image) -> np.ndarray:
    """The figure: every pixel that is not the flat #F2F2F2 background (a cheap RGB test; the perceptual one is slow on 1536x1024)."""
    bg = np.array(P.hex_to_rgb(BG), dtype=np.int16)
    return np.abs(np.asarray(im.convert("RGB"), dtype=np.int16) - bg).max(axis=2) > BG_TOL


LEAK_SCALE = 4                     # the leak share is a share of pixels: measured on a picture a quarter of the size (the Lab conversion is slow)


def leak_check(im: Image.Image, hexes: dict[str, list[str]], sha: str) -> CheckResult:
    """A_LEAK (CHK-G1-04, HARD) on one character's picture."""
    small = im.convert("RGB").resize((im.width // LEAK_SCALE, im.height // LEAK_SCALE), Image.Resampling.NEAREST)
    return runner.run_check("A_LEAK", sha, lambda: C.check_leak(small, hexes["own"], hexes["partner"], hexes["shared"], bg_hex=BG, subject_sha=sha))


def fg_in_slot(fg: np.ndarray, sl: guides.Slot) -> np.ndarray:
    out = np.zeros_like(fg)
    out[:, sl.x0:sl.x0 + guides.SLOT_W] = fg[:, sl.x0:sl.x0 + guides.SLOT_W]
    return out


def zones_for(guide: guides.Guide, spec: dict[str, Any], c: str) -> dict[str, tuple[np.ndarray, str]]:
    pal = common.palette_map(spec)
    masks_ = guides.zone_masks(guide, "front")
    top = pal.get(spec[c]["top"]["base_ref"])
    bottom = pal.get(spec[c]["bottom"]["base_ref"])
    out: dict[str, tuple[np.ndarray, str]] = {}
    if top:
        out["torso"] = (masks_["torso"], top)
    if bottom:
        out["leg_l"] = (masks_["leg_l"], bottom)
        out["leg_r"] = (masks_["leg_r"], bottom)
    return out


def gate_a(spec: dict[str, Any], c: str, guide: guides.Guide, im: Image.Image, sha: str, orig: Image.Image, raw: Image.Image,
           editable: np.ndarray, rejected: list[int]) -> tuple[list[CheckResult], bool, float]:
    """Gate A of one concept draft, in code: A_SIZE, A_PASTE, A_SIL_GUIDE per figure, A_OCR, A_LEAK, A_SWATCH (SOFT), A_PHASH (SOFT)."""
    results: list[CheckResult] = []
    results.append(runner.build_result("A_SIZE", passed=im.size == (guides.CANVAS_W, guides.CANVAS_H), subject_sha=sha, metric="size",
                                       evidence=f"{im.size[0]}x{im.size[1]}", fix_hint="regenerate")
                   if policy.is_registered("A_SIZE") else common.mk_result("A_SIZE", im.size == (guides.CANVAS_W, guides.CANVAS_H), metric="size",
                                                                         subject_sha=sha))
    results.append(runner.run_check("A_PASTE", sha, lambda: masks.check_paste(orig, raw, editable, subject_sha=sha)))
    fg = fg_mask(im)
    for name in SLOT_NAMES:
        sl = guide.slots[name]
        results.append(runner.run_check("A_SIL_GUIDE", sha, lambda sl=sl, name=name: C.check_sil_guide(
            fg_in_slot(fg, sl), guide.body_masks[name], rows=(guides.NECK_Y, guides.FEET_Y), subject_sha=sha)))
    results.append(runner.run_check("A_OCR", sha, lambda: ocr.check_no_text(im, subject_sha=sha)))
    hexes = figure_hexes(spec, c)
    results.append(leak_check(im, hexes, sha))
    zones = zones_for(guide, spec, c)
    swatch = runner.run_check("A_SWATCH", sha, lambda: C.check_swatch(im, zones, exclude_hex=[BG], subject_sha=sha)) if zones else None
    if swatch is not None:
        results.append(swatch)
    close = False
    if rejected:
        ph = runner.run_check("A_PHASH", sha, lambda: similarity.dedupe_check(im, rejected, subject_sha=sha))
        results.append(ph)
        close = not ph.passed
    return results, close, float(swatch.value or 0.0) if swatch is not None else 0.0


def vlm_png(png: bytes) -> bytes:
    return common.png_bytes(guides.vlm_image(common.open_image(png), with_checkerboard=False))


def accessory_list(spec: dict[str, Any], c: str) -> str:
    accs = spec[c].get("accessories") or []
    return "; ".join(str(a.get("description", a.get("kind", ""))) for a in accs if a) or "none"


def judge_concept(ctx: StepContext, spec: dict[str, Any], c: str, png: bytes, sha: str, facts: dict[str, Any], counter: PL.CallCounter,
                  nonce: str) -> list[CheckResult]:
    """Gate B (L11) on one character's concept: the structural rules (HARD), the IP rules in their own call (HARD) and three SOFT rules."""
    reqs = [GB.RuleRequest(r) for r in (*CONCEPT_HARD_RULES, *CONCEPT_SOFT_RULES) if r != "cn_accessories_listed"]
    reqs.append(GB.RuleRequest("cn_accessories_listed", {"accessory_list": accessory_list(spec, c)}))
    res = GB.run_gate_b(PL.gate_b_provider(ctx, [vlm_png(png)], counter, nonce), reqs, measured_facts=PL.canonical(facts) if facts else "none",
                        rules_per_call=RULES_PER_CALL, votes=1, subject_sha=sha)
    return res.results


# ---------------------------------------------------------------------------------------------------- images
def named(data: bytes, name: str) -> NamedPng:
    return NamedPng(name, data)


def image_call(ctx: StepContext, req: Any, n_total: int) -> tuple[list[bytes], str | None, bool]:
    """Send an image request through the (real or mock) OpenAI adapter. An identical request (same prompt, inputs, mask, model, nonce) is
    served from the content cache: never pay twice. Returns ``(raw PNG bytes, request id, from cache)``."""
    rt = ctx.rt
    key = sha256_of({"kind": "concept_img", "model": req.model, "prompt": sha256_of(req.prompt), "size": req.size, "quality": req.quality,
                     "n": n_total, "nonce": req.nonce, "images": [pixel_sha(bytes(i.data)) for i in req.images],
                     "mask": pixel_sha(bytes(req.mask.data)) if req.mask else None})
    hit = rt.cache.lookup(key)
    if hit is not None and hit.outputs:
        return [rt.cas.get(s) for s in hit.outputs], hit.result.get("request_id"), True
    adapter = ctx.provider("openai")
    call_ctx = PL.make_call_ctx(ctx, req.tag)
    ctx.check_cancel()
    res = adapter.run_many(req, n_total, call_ctx) if n_total > DRAFTS else adapter.edit(req, call_ctx)
    common.record_cost(ctx, res.cost, f"images.edit:{req.tag}#{ctx.step.attempt}:{n_total}:{req.nonce or '-'}", fallback_provider="openai")
    raws = [bytes(b) for b in res.images]
    shas = [common.put_bytes(ctx, b, "png", role="concept_raw", part_id=None,
                             provenance=common.prov("mock" if common.is_mock(rt, "openai") else "openai", provider="openai", model=req.model,
                                                    nonce=req.nonce, request_id=res.request_id, params={"n_total": n_total, "tag": req.tag})).sha256
            for b in raws]
    rt.cache.store(key, CachedResult(step_kind=f"img:{req.tag}", outputs=shas, result={"request_id": res.request_id}))
    return raws, res.request_id, False


def compile_concept(rt: Runtime, spec: dict[str, Any], c: str, mode: str, fix_sentence: str, has_sheet: bool) -> Any:
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts.catalog import default_ctx

    cctx = default_ctx(kits.load_context(rt).inventory)
    if mode == "i1e":
        return compiler.compile("I1e.concept_edit", spec, c, {"fix_sentence": fix_sentence}, cctx)
    return compiler.compile("I1.concept_char" if has_sheet else "I1.concept_char@s0", spec, c, {}, cctx)


def route_model(rt: Runtime, project: Project, template_id: str, quality: str) -> tuple[str, str]:
    from duoskin.prompts import registry as preg

    route = preg.get(template_id.split("@")[0]).meta.route.get("draft") or {}
    pins = project.pins.models if project.pins else {}
    return str(pins.get("image_draft") or route.get("model", "")), str(quality or route.get("quality", "low"))


def initial_mask_mode(rt: Runtime, p: CharParams) -> str:
    """``auto``: the figure-boxes mask of the template; under the mock the head-only one (the mock repaints the whole editable area with one flat
    colour, which the figure boxes would turn into rectangles). The ladder still runs for a real model that draws outside the silhouette."""
    if p.mask_mode != "auto":
        return p.mask_mode
    return "head" if common.is_mock(rt, "openai") else "figure"


def ladder(mode0: str) -> list[tuple[str, int, str]]:
    """The rungs of the concept ladder: the first drafts, the tight mask (only after the figure-boxes mask), then eight drafts."""
    rungs = [(mode0, DRAFTS, "first drafts")]
    if mode0 == "figure":
        rungs.append(("tight", DRAFTS, "tight mask"))
    rungs.append((mode0 if mode0 != "figure" else "tight", REGENERATE_DRAFTS, "eight drafts"))
    return rungs


def draw_rung(ctx: StepContext, p: CharParams, spec: dict[str, Any], mask_mode: str, n: int, rung: int, st: dict[str, Any],
              quality: str, project: Project, counter: PL.CallCounter) -> tuple[list[Cand], dict[str, Any]]:
    rt = ctx.rt
    guide = build_guide(spec, p.char)
    editable = editable_for(guide, mask_mode)
    sheet = house_sheet(rt)
    if p.mode == "i1e":
        if not p.base_sha:
            raise StepFailure("an edit of the picture needs the draft to edit", kind="bad_request", billed="no")
        orig_png = rt.cas.get(p.base_sha)
        cp = compile_concept(rt, spec, p.char, "i1e", p.fix_sentence, sheet is not None)
        images = [orig_png] + ([sheet] if sheet else [])
    else:
        orig_png = guide.image_png()
        cp = compile_concept(rt, spec, p.char, "i1", "", sheet is not None)
        images = [orig_png] + ([sheet] if sheet else [])
    model, q = route_model(rt, project, cp.template_id, quality)
    prompt = cp.text if sheet or p.mode != "i1e" else cp.text.replace(" " + NO_SHEET_IMAGE2, "")
    nonce = f"{ctx.step.nonce}:{rung}" if ctx.step.nonce else (f"r{rung}" if rung else "")
    req = ImageRequest(
        model=model, prompt=prompt, size=SIZE, quality=q, background="opaque", n=min(DRAFTS, n),
        images=tuple(named(b, f"image{i + 1}.png") for i, b in enumerate(images)), image1_role="edit_target",
        mask=named(masks.make_mask(editable), "mask.png"), nonce=nonce, tag=cp.template_id.split(".")[0].split("@")[0])
    raws, rid, cached = image_call(ctx, req, n)
    orig = common.open_image(orig_png).convert("RGBA")
    rejected = [int(h, 16) for h in st.get("rejected_hashes", [])]
    cands: list[Cand] = []
    for i, raw in enumerate(raws):
        ctx.check_cancel()
        out_im = common.open_image(raw).convert("RGBA")
        merged = masks.paste_back(orig, out_im, editable).convert("RGB")
        png = common.png_bytes(merged)
        sha = sha256_of(png)
        results, close, de = gate_a(spec, p.char, guide, merged, sha, orig, out_im, editable, rejected)
        cand = Cand(index=i, png=png, raw_sha=sha256_of(raw), sha=sha, checks=results, phash_close=close, swatch_de=de)
        cand.hard_ok = not blocking_failures(results)
        cands.append(cand)
    info = {"rung": rung, "mask_mode": mask_mode, "n": n, "template": cp.template_id, "prompt_sha": cp.sha256, "request_id": rid,
            "cached": cached, "model": model, "quality": q}
    return cands, info


def pick_survivors(cands: list[Cand]) -> list[Cand]:
    """Candidates that passed Gate A, best first. A draft that is a near-copy of a rejected one (A_PHASH) goes last, never out: when
    every draft is close to a rejected one (a mock, or a very constrained design) the person still gets a new picture, with a warning."""
    return sorted((c for c in cands if c.hard_ok), key=Cand.key)


def run_char(ctx: StepContext, p: CharParams, inputs: list[Any]) -> StepResult:
    """One character's concept (front and back) for one plan: draw, Gate A, Gate B, choose, keep the alternatives."""
    rt = ctx.rt
    rec = rt.repo.get_spec(p.spec_id)
    spec = rec.spec
    project = rt.repo.get_project(p.project_id)
    st = get_state(rt, p.plan_set_id, p.slot, p.char)
    counter = PL.CallCounter()
    quality = project.settings.concept_quality
    mode0 = initial_mask_mode(rt, p)
    survivors: list[Cand] = []
    all_checks: list[CheckResult] = []
    rungs_log: list[dict[str, Any]] = []
    chosen: Cand | None = None
    pool: list[Cand] = []
    for rung, (mask_mode, n, label) in enumerate(ladder(mode0)):
        ctx.progress(0.1 + 0.2 * rung, f"drawing character {p.char.upper()} ({label})")
        cands, info = draw_rung(ctx, p, spec, mask_mode, n, rung, st, quality, project, counter)
        survivors = pick_survivors(cands)
        for c in cands:
            all_checks.extend(c.checks)
        info.update({"drafts": len(cands), "survivors": len(survivors),
                     "failed": sorted({r.check_id for c in cands for r in blocking_failures(c.checks)})})
        rungs_log.append(info)
        if not survivors:
            continue
        # Gate B on the best two; the first that passes everything is chosen, the rest stay as alternatives
        facts = {"mask_mode": mask_mode, "mode": p.mode, "guide_iou": {s: round(float(next((r.value for r in survivors[0].checks
                                                                                         if r.check_id == "A_SIL_GUIDE"), 0.0) or 0.0), 3) for s in SLOT_NAMES}}
        ctx.progress(0.6, f"judging character {p.char.upper()}")
        for cand in survivors[:JUDGED]:
            cand.judged = judge_concept(ctx, spec, p.char, cand.png, cand.sha, facts, counter, f"{p.char}:{cand.sha[:8]}")
            cand.judged_ok = not blocking_failures(cand.judged)
            all_checks.extend(cand.judged)
        ok = [c for c in survivors[:JUDGED] if c.judged_ok]
        if ok:
            chosen = min(ok, key=Cand.key)
            pool = [c for c in sorted(ok, key=Cand.key) if c is not chosen]
            break
        survivors = []
    ctx.record_checks(all_checks)
    st.update({"spec_id": p.spec_id, "mode": p.mode, "nonce": ctx.step.nonce, "rungs": rungs_log, "generation": p.generation,
               "fix_sentence": p.fix_sentence, "base_sha": p.base_sha, "reason": p.reason})
    if chosen is None:
        failed = sorted({r.check_id for r in all_checks if policy.is_blocking(r) and not r.passed})
        st.update({"failed": {"checks": failed, "message": "None of the drawings passed the required checks."}, "chosen": None, "alts": []})
        put_state(rt, p.plan_set_id, p.slot, p.char, st)
        return StepResult(result={"char": p.char, "failed": True, "checks": failed, "rungs": rungs_log},
                          message=f"character {p.char.upper()} needs a person to look at it")
    stored = store_candidate(ctx, p, chosen, rank=0, status="chosen")
    alts = [store_candidate(ctx, p, c, rank=i + 1, status="candidate") for i, c in enumerate(pool[:ALTERNATIVES])]
    rest = [c for c in survivors if c is not chosen and c not in pool][:max(0, ALTERNATIVES - len(alts))]
    alts += [store_candidate(ctx, p, c, rank=len(alts) + i + 1, status="candidate", judged=False) for i, c in enumerate(rest)]
    seen = list(st.get("rejected_hashes", []))
    st.update({"chosen": stored, "alts": alts, "failed": None, "checks": [r_summary(r) for r in [*chosen.checks, *chosen.judged]],
               "rejected_hashes": seen, "phash_close": chosen.phash_close})
    put_state(rt, p.plan_set_id, p.slot, p.char, st)
    return StepResult(outputs=[stored["sha"]], result={"char": p.char, "chosen": stored["sha"], "alts": len(alts), "rungs": rungs_log, "mode": p.mode,
                                                       "base_sha": p.base_sha, "mask_mode": rungs_log[-1]["mask_mode"],
                                                       "nonce": ctx.step.nonce, "fix_sentence": p.fix_sentence},
                      message=f"character {p.char.upper()} is drawn")


def r_summary(r: CheckResult) -> dict[str, Any]:
    return {"id": r.check_id, "kind": r.kind, "passed": r.passed, "ran": r.ran, "ev": (r.evidence or "")[:160]}


def store_candidate(ctx: StepContext, p: CharParams, c: Cand, *, rank: int, status: str, judged: bool = True) -> dict[str, Any]:
    """Store a draft and its front and back crops (the crops are what Gate 1 shows). Returns the state entry."""
    im = common.open_image(c.png)
    pv = common.prov("mock" if common.is_mock(ctx.rt, "openai") else "openai", provider="openai", prompt_id=f"I1{'e' if p.mode == 'i1e' else ''}",
                     nonce=ctx.step.nonce, params={"char": p.char, "mode": p.mode, "slot": p.slot, "judged": judged})
    asset = common.put_bytes(ctx, c.png, "png", role=f"draft.concept.{p.char}", part_id=None, provenance=pv, status=status, rank=rank)   # type: ignore[arg-type]
    crops = {}
    for name, sl_x in zip(SLOT_NAMES, (0, guides.SLOT_W), strict=True):
        crop = im.crop((sl_x, 0, sl_x + guides.SLOT_W, guides.CANVAS_H))
        crops[name] = common.put_png(ctx, crop, role=f"concept.crop.{p.char}_{name}", part_id=None, provenance=pv, status=status,   # type: ignore[arg-type]
                                     rank=rank).sha256
    return {"sha": asset.sha256, "front": crops["front"], "back": crops["back"], "judged": judged, "soft": c.soft_passes,
            "phash": f"{similarity.phash(im):016x}", "swatch_de": round(c.swatch_de, 2)}


# ---------------------------------------------------------------------------------------------------- C2: the sheet and the duo checks
def figure_hexes(spec: dict[str, Any], c: str) -> dict[str, list[str]]:
    sets = PR.leak_sets(LI.parse_loose(spec), c)
    return {"own": sets.own_hex, "partner": sets.partner_only_hex, "shared": sets.shared_hex}


def signature_item(spec: dict[str, Any], c: str) -> str:
    return str(spec[c]["dna"].get("motif_object") or spec[c]["top"]["recipe_id"]).replace("_", " ")


def structure_checks_leak(spec: dict[str, Any]) -> bool:
    return PR.profile(spec["world"]["pair_structure"])["leak_policy"].get("mode") == "standard"


def not_buildable_list(items: list[Any]) -> list[str]:
    return [f"{i['element']} ({i['where']})" for i in items if not i.get("buildable", True)]


def warning(check_id: str, text: str, *, char: str = "", severity: str = "medium") -> dict[str, Any]:
    return {"id": f"{check_id}:{char}" if char else check_id, "text": text, "severity": severity, "catch_rate": 0.0, "visible": True,
            "fresh": False, "check_id": check_id}


def run_assemble(ctx: StepContext, p: AssembleParams, inputs: list[Any]) -> StepResult:
    """C2 for one plan: the 4-up sheet (no rescaling), A_LEAK per character, the duo rules on the assembled sheet, the clone warning and
    the inventory of what the picture shows (what the kits cannot build)."""
    rt = ctx.rt
    rec = rt.repo.get_spec(p.spec_id)
    spec = rec.spec
    sts = {c: get_state(rt, p.plan_set_id, p.slot, c) for c in CHARS}
    doc: dict[str, Any] = {"spec_id": p.spec_id, "ok": False, "hard_failures": [], "warnings": [], "not_buildable": [], "notes": []}
    missing = [c for c in CHARS if not sts[c].get("chosen")]
    if missing:
        doc["notes"] = [f"character {c.upper()} has no usable drawing: " + ", ".join(sts[c].get("failed", {}).get("checks", []) or ["not drawn"])
                        for c in missing]
        doc["hard_failures"] = sorted({x for c in missing for x in (sts[c].get("failed") or {}).get("checks", [])}) or ["not_drawn"]
        put_sheet(rt, p.plan_set_id, p.slot, doc)
        return StepResult(result=doc, message="a character needs a person to look at it")
    if any(sts[c].get("spec_id") != p.spec_id for c in CHARS):
        raise StepFailure("the drawings belong to another version of the plan", kind="bad_request", billed="no")
    ims = {c: common.open_image(rt.cas.get(sts[c]["chosen"]["sha"])) for c in CHARS}
    sheet = guides.assemble_concept_sheet(ims["a"], ims["b"])
    sheet_png = common.png_bytes(sheet)
    sheet_sha = sha256_of(sheet_png)
    pv = common.prov("code", params={"slot": p.slot, "spec_id": p.spec_id, "a": sts["a"]["chosen"]["sha"], "b": sts["b"]["chosen"]["sha"]})
    sheet_asset = common.put_bytes(ctx, sheet_png, "png", role="concept.sheet", part_id=None, provenance=pv, status="candidate")
    labelled = common.put_png(ctx, guides.add_sheet_labels(sheet), role="concept.sheet_labelled", part_id=None, provenance=pv, status="candidate")
    results: list[CheckResult] = []
    warnings: list[dict[str, Any]] = []
    spec_model = LI.parse_loose(spec)
    for c in CHARS:
        hexes = figure_hexes(spec, c)
        results.append(leak_check(ims[c], hexes, sheet_sha))
    leaking = [c for c, r in zip(CHARS, results, strict=True) if not r.passed]
    if leaking and all(int(sts[c].get("leak_reruns", 0)) < 1 for c in leaking):
        steps = []
        for c in leaking:
            sts[c]["leak_reruns"] = int(sts[c].get("leak_reruns", 0)) + 1
            put_state(rt, p.plan_set_id, p.slot, c, sts[c])
            steps.append(char_step(rt, ctx.step.job_id, p.project_id, p.plan_set_id, p.slot, p.spec_id, c, reason="leak",
                                   nonce=common.new_nonce(), generation=int(sts[c].get("generation", 0)) + 1))
        again = assemble_step(rt, ctx.step.job_id, p.project_id, p.plan_set_id, p.slot, p.spec_id, deps=[s.id for s in steps])
        refresh = gate_step(rt, ctx.step.job_id, p.project_id, p.plan_set_id, refresh=True, deps=[again.id])
        ctx.spawn([*steps, again, refresh])
        ctx.record_checks(results)
        doc["notes"] = ["a colour of the partner showed up: drawing the character again"]
        put_sheet(rt, p.plan_set_id, p.slot, {**doc, "pending": True})
        return StepResult(result={**doc, "pending": True}, message="a colour leaked: drawing again")
    counter = PL.CallCounter()
    judge_sheet = common.png_bytes(guides.downscale_for_judges(sheet))
    duo_reqs = [GB.RuleRequest("dj_same_world"), GB.RuleRequest("dj_not_clones"),
                GB.RuleRequest("dj_anchor_visible", {"anchor": str((spec["shared_anchors"][0] or {}).get("description", "the shared anchor"))})]
    if structure_checks_leak(spec):
        duo_reqs.append(GB.RuleRequest("dj_no_leak", {"A_signature_item": signature_item(spec, "a"), "B_signature_item": signature_item(spec, "b")}))
    duo = GB.run_gate_b(PL.gate_b_provider(ctx, [judge_sheet], counter, f"duo:{sheet_sha[:8]}"), duo_reqs, measured_facts="none",
                        rules_per_call=RULES_PER_CALL, votes=1, subject_sha=sheet_sha)
    results.extend(duo.results)
    clone = clone_check(rt, ims, sheet_sha)
    results.append(clone)
    inventory: dict[str, list[dict[str, Any]]] = {}
    for c in CHARS:
        inv = sts[c].get("inventory")
        if not inv or inv.get("sha") != sts[c]["chosen"]["sha"]:
            out = PL.llm_call(ctx, "L15.concept_inventory", {"spec_json": PL.spec_for_critic(spec), "measured_facts": PL.canonical({"character": c})},
                              images=[vlm_png(rt.cas.get(sts[c]["chosen"]["sha"]))], counter=counter, nonce=f"inv:{c}")
            inv = {"sha": sts[c]["chosen"]["sha"], "items": [i.model_dump(mode="json") for i in out.parsed.items]}
            sts[c]["inventory"] = inv
            put_state(rt, p.plan_set_id, p.slot, c, sts[c])
        inventory[c] = inv["items"]
    nb = [f"{x}: character {c.upper()}" for c in CHARS for x in not_buildable_list(inventory[c])]
    results.append(runner.build_result("CHK-G1-09", passed=not nb, subject_sha=sheet_sha, metric="not_buildable", value=float(len(nb)),
                                       evidence="; ".join(nb) or "everything in the picture can be built"))
    for c in CHARS:
        for r in [*(x for x in sts[c].get("checks", []) if not x["passed"] and x["kind"] == "soft")]:
            if r["id"] in WARNING_TEXT:
                warnings.append(warning(r["id"], WARNING_TEXT[r["id"]], char=c))
    for r in results:
        if r.kind == "soft" and r.ran and not r.passed and r.check_id in WARNING_TEXT:
            warnings.append(warning(r.check_id, WARNING_TEXT[r.check_id]))
    unique: dict[str, dict[str, Any]] = {}
    for w in warnings:
        unique.setdefault(w["id"], w)
    ctx.record_checks(results)
    hard = sorted({r.check_id for r in blocking_failures(results)})
    doc.update({"ok": not hard, "hard_failures": hard, "warnings": list(unique.values()), "not_buildable": nb, "sheet": sheet_asset.sha256,
                "labelled": labelled.sha256, "pending": False, "checks": [r_summary(r) for r in results], "notes": [],
                "inventory": {c: inventory[c] for c in CHARS}})
    put_sheet(rt, p.plan_set_id, p.slot, doc)
    _ = spec_model
    return StepResult(outputs=[sheet_asset.sha256], result=doc, message="the sheet is ready" if not hard else "a required check failed")


def clone_check(rt: Runtime, ims: dict[str, Image.Image], sheet_sha: str) -> CheckResult:
    """CHK-G1-11 (SOFT, concept stage): are the two characters near-clones? Shown only while DreamSim exists; without it the check passes
    and says so (``similarity.check_clone_band``)."""
    from duoskin.pipeline import kits

    model = None
    try:
        path = kits.dreamsim_path(rt)
        model = similarity.load_dreamsim(path) if path else None
    except Exception:                                      # noqa: BLE001 - a missing model only silences a SOFT warning
        model = None
    views = {c: {"front": ims[c].crop((0, 0, guides.SLOT_W, guides.CANVAS_H)), "back": ims[c].crop((guides.SLOT_W, 0, guides.CANVAS_W, guides.CANVAS_H))}
             for c in CHARS}
    return runner.run_check("CHK-G1-11", sheet_sha, lambda: similarity.check_clone_band(views["a"], views["b"], model=model, stage="concept",
                                                                                    subject_sha=sheet_sha))


# ---------------------------------------------------------------------------------------------------- steps and the job
def _step(rt: Runtime, kind: str, job_id: str, project_id: str, params: Strict, *, deps: list[str] | None = None, nonce: str = "",
          priority: int = 30) -> Step:
    return rt.ops.new_step(kind, job_id=job_id, project_id=project_id, params=params.model_dump(mode="json"), deps=list(deps or []), nonce=nonce,
                           priority=priority)


def char_step(rt: Runtime, job_id: str, project_id: str, plan_set_id: str, slot: int, spec_id: str, c: str, *, mode: str = "i1",
              fix_sentence: str = "", base_sha: str = "", region_hint: str = "none", reason: str = "draw", nonce: str = "", generation: int = 0,
              mask_mode: str = "auto", deps: list[str] | None = None) -> Step:
    return _step(rt, "concept.char", job_id, project_id, CharParams(project_id=project_id, plan_set_id=plan_set_id, slot=slot, spec_id=spec_id,
                                                                    char=c, mode=mode, fix_sentence=fix_sentence, base_sha=base_sha,   # type: ignore[arg-type]
                                                                    region_hint=region_hint, reason=reason, generation=generation,
                                                                    mask_mode=mask_mode), deps=deps, nonce=nonce)   # type: ignore[arg-type]


def assemble_step(rt: Runtime, job_id: str, project_id: str, plan_set_id: str, slot: int, spec_id: str, *, deps: list[str] | None = None) -> Step:
    return _step(rt, "concept.assemble", job_id, project_id, AssembleParams(project_id=project_id, plan_set_id=plan_set_id, slot=slot, spec_id=spec_id),
                 deps=deps)


def gate_step(rt: Runtime, job_id: str, project_id: str, plan_set_id: str, *, refresh: bool = False, deps: list[str] | None = None,
              slots: list[int] | None = None) -> Step:
    return _step(rt, "concept.gate", job_id, project_id, GateParams(project_id=project_id, plan_set_id=plan_set_id, slots=slots or [], refresh=refresh),
                 deps=deps, priority=20)


def start_previews(rt: Runtime, job_id: str, project: Project, plan_set_id: str, recs: list[SpecRecord], deps: list[str] | None = None) -> list[Step]:
    """The concept steps of the shown plans (called by ``plan.select``): front and back of both characters per plan, then Gate 1."""
    steps: list[Step] = []
    tails: list[str] = []
    for slot, rec in enumerate(recs):
        chars = [char_step(rt, job_id, project.id, plan_set_id, slot, rec.id, c, deps=deps) for c in CHARS]
        asm = assemble_step(rt, job_id, project.id, plan_set_id, slot, rec.id, deps=[s.id for s in chars])
        steps += [*chars, asm]
        tails.append(asm.id)
    steps.append(gate_step(rt, job_id, project.id, plan_set_id, refresh=False, deps=tails, slots=list(range(len(recs)))))
    return steps


def estimate_char(p: CharParams) -> float:
    from duoskin.providers import pricing

    img = float(pricing.estimate_openai_image(quality="low", n=DRAFTS, n_input_images=2, prompt_chars=2000).usd)
    return img + PL.estimate_llm(None, "L11.asset_checker", calls=2 * 3, in_tokens=6000, out_tokens=800, cached=5000)


def estimate_assemble(p: AssembleParams) -> float:
    return PL.estimate_llm(None, "L11.asset_checker", calls=4, in_tokens=6000, out_tokens=800, cached=5000)


# ---------------------------------------------------------------------------------------------------- Gate 1: tiles
def tile_for(rt: Runtime, project: Project, psid: str, slot: int, rec: SpecRecord, shown: list[SpecRecord], env: dict[str, Any],
             previous: GateTile | None = None) -> GateTile:
    """The Gate 1 tile of one plan from the concept state: front and back of both characters, the facts the page shows."""
    sts = {c: get_state(rt, psid, slot, c) for c in CHARS}
    doc = get_sheet(rt, psid, slot)
    assets: dict[str, str] = {}
    alternatives: list[dict[str, str]] = []
    for c in CHARS:
        ch = sts[c].get("chosen")
        if ch:
            assets[f"{c}_front"], assets[f"{c}_back"] = ch["front"], ch["back"]
    for k in range(ALTERNATIVES):
        alt = {f"{c}_front": sts[c]["alts"][k]["front"] for c in CHARS if sts[c].get("alts") and k < len(sts[c]["alts"])}
        if alt:
            alternatives.append(alt)
    if doc.get("labelled"):
        assets["sheet"] = doc["labelled"]
    drawn = all(sts[c].get("chosen") for c in CHARS)
    failed = [c for c in CHARS if sts[c].get("failed")]
    if failed or (doc.get("spec_id") == rec.id and doc.get("hard_failures")):
        state = TileState.NEEDS_HUMAN
    elif drawn and doc.get("spec_id") == rec.id and doc.get("ok") and not doc.get("pending"):
        state = TileState.READY
    else:
        state = TileState.GENERATING
    cons = env.get("brief_constraints", [])
    facts: dict[str, Any] = {
        "spec_id": rec.id, "plan_set_id": psid, "slot": slot, "rank": slot, "critic_levels": dict(rec.critic_levels),
        "must_include": BR.must_include_coverage(project.must_include, cons, rec.spec), "not_buildable": list(doc.get("not_buildable", [])),
        "brief_read_as": BR.brief_read_as([r.spec for r in shown]), "warnings": list(doc.get("warnings", [])),
        "hard_failures": list(doc.get("hard_failures", [])), "notice": env.get("notice", ""), "notes": list(doc.get("notes", [])),
        "wildcard": bool(rec.spec.get("is_wildcard")), "version": rec.version,
        "failed": {c: sts[c]["failed"] for c in failed},
    }
    return GateTile(tile_id=f"plan{slot}", label=f"Plan {slot + 1}", state=state, assets=assets, alternatives=alternatives, facts=facts,
                    badges=["Wildcard"] if rec.spec.get("is_wildcard") else [], allowed_actions=allowed_actions_for(GateKind.CONCEPT),
                    version=previous.version if previous is not None else 0)


def open_concept_gate(rt: Runtime, project_id: str) -> Gate | None:
    return next((g for g in rt.repo.list_gates(project_id, "open") if g.kind == GateKind.CONCEPT), None)


def run_gate(ctx: StepContext, p: GateParams, inputs: list[Any]) -> StepResult:
    """Open Gate 1 once every plan's pictures are ready, or refresh the tiles of the gate that is already open (after a Reimagine, a Change
    or a chosen alternative). The picture checks of the plan set (CHK-G1-07) are recorded when the gate opens."""
    rt = ctx.rt
    project = rt.repo.get_project(p.project_id)
    shown = shown_records(rt, p.project_id, p.plan_set_id)
    env = PL.get_envelope(rt, p.plan_set_id)
    existing = open_concept_gate(rt, p.project_id)
    if p.refresh and existing is None:             # the gate was decided (approved, new plan) while this refresh waited: never open another
        return StepResult(result={"skipped": True}, message="Gate 1 is no longer open")
    tiles = [tile_for(rt, project, p.plan_set_id, slot, rec, shown, env,
                      previous=next((t for t in existing.tiles if t.tile_id == f"plan{slot}"), None) if existing else None)
             for slot, rec in enumerate(shown)]
    if existing is not None:
        changed = refresh_gate(rt, existing.id, tiles)
        return StepResult(result={"gate_id": existing.id, "refreshed": changed}, message="the concept tiles were updated")
    ready = all(t.assets.get(f"{c}_front") and t.assets.get(f"{c}_back") for t in tiles for c in CHARS) if tiles else False
    shape = LI.gate1_set_check([r.spec for r in shown], wildcard_dropped=bool(env.get("notice")), front_back_ready=ready or any(
        t.state == TileState.NEEDS_HUMAN for t in tiles))
    ctx.record_checks([shape])
    gate = Gate(id="", project_id=p.project_id, job_id=ctx.step.job_id, kind=GateKind.CONCEPT, tiles=tiles, opened_at=utcnow())
    rt.repo.set_project_stage(p.project_id, Stage.GATE1, bus=rt.bus)
    opened = ctx.open_gate(gate)
    return StepResult(result={"gate_id": opened.id, "tiles": len(tiles)}, message="Gate 1 is open")


def refresh_gate(rt: Runtime, gate_id: str, tiles: list[GateTile]) -> list[str]:
    """Replace the tiles of an open gate where something changed (version + 1 for each); returns the changed tile ids."""
    changed: list[str] = []
    with rt.db.tx():
        gate = rt.repo.get_gate(gate_id)
        if gate.state != "open":
            return []
        by_id = {t.tile_id: t for t in tiles}
        out: list[GateTile] = []
        for old in gate.tiles:
            new = by_id.get(old.tile_id)
            if new is None:
                out.append(old)
                continue
            new = new.model_copy(update={"version": old.version})
            if new.model_dump_json() == old.model_dump_json():
                out.append(old)
                continue
            out.append(new.model_copy(update={"version": old.version + 1}))
            changed.append(old.tile_id)
        if changed:
            rt.repo.save_gate(gate.model_copy(update={"tiles": out}))
    for tid in changed:
        t = next(x for x in out if x.tile_id == tid)
        rt.bus.emit("tile.updated", {"gate_id": gate_id, "tile_id": tid, "state": t.state.value, "version": t.version}, gate.project_id or None)
    if changed:
        rt.bus.emit("gate.updated", {"gate_id": gate_id, "state": "open", "kind": GateKind.CONCEPT.value}, gate.project_id or None)
    return changed


def set_tile_generating(rt: Runtime, gate: Gate, tile_id: str) -> GateTile:
    """Mark a tile of the gate being decided as GENERATING (the applier works on the gate copy the service saves)."""
    tile = next(t for t in gate.tiles if t.tile_id == tile_id)
    tile.state = TileState.GENERATING
    return tile


def mark_gate_tile_generating(rt: Runtime, project_id: str, tile_id: str) -> None:
    """Same, for a gate that is not the one being decided (the change flow decides a CHANGE_CONFIRM gate and redraws a Gate 1 tile)."""
    gate = open_concept_gate(rt, project_id)
    if gate is None:
        return
    tile = next((t for t in gate.tiles if t.tile_id == tile_id), None)
    if tile is None:
        return
    new = [t.model_copy(update={"state": TileState.GENERATING, "version": t.version + 1}) if t.tile_id == tile_id else t for t in gate.tiles]
    rt.repo.save_gate(gate.model_copy(update={"tiles": new}))
    rt.bus.emit("tile.updated", {"gate_id": gate.id, "tile_id": tile_id, "state": "generating", "version": tile.version + 1}, project_id)


# ---------------------------------------------------------------------------------------------------- Gate 1: decisions
def slot_of(tile: GateTile) -> int:
    return int(tile.facts.get("slot", int(tile.tile_id.removeprefix("plan") or 0)))


def targets_of(decision: Any) -> list[str]:
    return list(CHARS) if decision.target in (None, "", "both") else [str(decision.target)]


def spawn(rt: Runtime, job_id: str, steps: list[Step]) -> list[str]:
    rt.scheduler.spawn(job_id, steps)
    return [s.id for s in steps]


def redraw_steps(rt: Runtime, gate: Gate, tile: GateTile, chars: list[str], *, mode: str = "i1", reason: str = "reimagine",
                 fixes: dict[str, Any] | None = None, spec_id: str | None = None) -> list[Step]:
    """The steps that draw ``chars`` of one tile again (and the assemble and refresh steps behind them)."""
    psid = str(tile.facts["plan_set_id"])
    slot = slot_of(tile)
    sid = spec_id or str(tile.facts["spec_id"])
    steps: list[Step] = []
    for c in chars:
        st = get_state(rt, psid, slot, c)
        seen = list(st.get("rejected_hashes", []))
        for entry in [st.get("chosen"), *(st.get("alts") or [])]:
            if entry and entry.get("phash") and entry["phash"] not in seen:
                seen.append(entry["phash"])
        st.update({"rejected_hashes": seen, "chosen_before": st.get("chosen")})
        put_state(rt, psid, slot, c, st)
        fx = (fixes or {}).get(c)
        route = fx.route if fx is not None else mode
        steps.append(char_step(rt, gate.job_id, gate.project_id, psid, slot, sid, c, mode=route, fix_sentence=fx.fix_sentence if fx else "",
                               base_sha=(st.get("chosen") or {}).get("sha", "") if route == "i1e" else "", region_hint=fx.region_hint if fx else "none",
                               reason=reason, nonce=common.new_nonce(), generation=int(st.get("generation", 0)) + 1))
    asm = assemble_step(rt, gate.job_id, gate.project_id, psid, slot, sid, deps=[s.id for s in steps])
    refresh = gate_step(rt, gate.job_id, gate.project_id, psid, refresh=True, deps=[asm.id])
    return [*steps, asm, refresh]


def apply_concept(ac: ApplyContext) -> ApplyResult | None:
    """The applier of Gate 1 (see the module docstring). Runs inside the decision's transaction: rows and steps only, no provider calls."""
    rt, gate, tile, d = ac.rt, ac.gate, ac.tile, ac.decision
    a = d.action
    if a == GateAction.NEW_PLAN:
        return apply_new_plan(ac)
    if tile.state == TileState.GENERATING and a in (GateAction.REIMAGINE, GateAction.CHANGE, GateAction.SELECT_ALTERNATIVE, GateAction.APPROVE):
        raise GateError("this plan is still being drawn", 409, "tile_busy")
    if a == GateAction.APPROVE:
        return apply_approve(ac)
    if a == GateAction.REIMAGINE:
        chars = targets_of(d)
        steps = redraw_steps(rt, gate, tile, chars)
        tile.state = TileState.GENERATING
        return ApplyResult(close_gate=False, spawned_step_ids=spawn(rt, gate.job_id, steps), step="keep")
    if a == GateAction.SELECT_ALTERNATIVE:
        return apply_select_alternative(ac)
    if a == GateAction.CHANGE:
        from duoskin.pipeline import change

        return change.start_change(ac)
    raise GateError(f"'{a.value}' does nothing on a concept tile", 422, "action_not_allowed")


def apply_select_alternative(ac: ApplyContext) -> ApplyResult:
    rt, gate, tile, d = ac.rt, ac.gate, ac.tile, ac.decision
    try:
        c, k = str(d.choice or "").split(":", 1)
        k_i = int(k)
    except ValueError:
        raise GateError("choose an alternative like 'a:0'", 422, "bad_choice") from None
    if c not in CHARS:
        raise GateError("the alternative must be for character a or b", 422, "bad_choice")
    psid, slot = str(tile.facts["plan_set_id"]), slot_of(tile)
    st = get_state(rt, psid, slot, c)
    alts = list(st.get("alts") or [])
    if not 0 <= k_i < len(alts) or not st.get("chosen"):
        raise GateError("that alternative does not exist", 422, "bad_choice")
    swapped = alts[k_i]
    alts[k_i] = st["chosen"]
    st.update({"chosen": swapped, "alts": alts})
    put_state(rt, psid, slot, c, st)
    asm = assemble_step(rt, gate.job_id, gate.project_id, psid, slot, str(tile.facts["spec_id"]))
    refresh = gate_step(rt, gate.job_id, gate.project_id, psid, refresh=True, deps=[asm.id])
    tile.state = TileState.GENERATING
    return ApplyResult(close_gate=False, spawned_step_ids=spawn(rt, gate.job_id, [asm, refresh]), step="keep")


def apply_approve(ac: ApplyContext) -> ApplyResult:
    rt, gate, tile, d = ac.rt, ac.gate, ac.tile, ac.decision
    if tile.state != TileState.READY:
        raise GateError("this plan is not ready to approve yet", 422, "not_ready")
    if tile.facts.get("not_buildable") and d.choice != ACK_TOKEN:
        raise GateError("confirm that the parts the kits cannot build exactly as drawn will be built differently", 422, "ack_required")
    spec_id = str(tile.facts["spec_id"])
    psid, slot = str(tile.facts["plan_set_id"]), slot_of(tile)
    palette_choice = "planned" if d.choice == PALETTE_PLANNED else "picture"
    tile.state = TileState.APPROVED
    rt.repo.insert_label("gate1_pick", "gate", [gate.id, tile.tile_id, spec_id], {"wildcard": bool(tile.facts.get("wildcard")),
                                                                                   "slot": slot, "plan_set_id": psid})
    if d.choice == ACK_TOKEN:
        rt.repo.insert_label("warning_override", "gate", [gate.id, tile.tile_id, "CON-04"], True)
    for rec in shown_records(rt, gate.project_id, psid):
        if rec.id == spec_id:
            PL.save_spec(rt, rec.model_copy(update={"status": "approved"}))
        else:
            PL.save_spec(rt, rec.model_copy(update={"status": "superseded"}))
    rt.repo.mutate_project(gate.project_id, lambda pr: (setattr(pr, "approved_spec_id", spec_id), setattr(pr, "current_spec_id", spec_id)))
    for g in rt.repo.list_gates(gate.project_id, "open"):
        if g.id != gate.id and g.kind in (GateKind.CLARIFY, GateKind.CHANGE_CONFIRM):
            rt.gates.close_gate(g.id, "superseded")
    step = _step(rt, "concept.lock", gate.job_id, gate.project_id, LockParams(project_id=gate.project_id, plan_set_id=psid, slot=slot, spec_id=spec_id,
                                                                              palette_choice=palette_choice,   # type: ignore[arg-type]
                                                                              acknowledged=[d.choice] if d.choice == ACK_TOKEN else []),
                 priority=10)
    return ApplyResult(close_gate=True, spawned_step_ids=spawn(rt, gate.job_id, [step]), resulting_spec_id=spec_id, step="auto")


def apply_new_plan(ac: ApplyContext) -> ApplyResult:
    rt, gate, d = ac.rt, ac.gate, ac.decision
    psid = str(ac.tile.facts.get("plan_set_id", ""))
    shown = shown_records(rt, gate.project_id, psid)
    BR.remember_rejected(rt, gate.project_id, [r.spec for r in shown], d.text or "none of these")
    for rec in shown:
        PL.save_spec(rt, rec.model_copy(update={"status": "superseded"}))
    for g in rt.repo.list_gates(gate.project_id, "open"):
        if g.id != gate.id and g.kind in (GateKind.CLARIFY, GateKind.CHANGE_CONFIRM):
            rt.gates.close_gate(g.id, "superseded")
    job = rt.scheduler.submit_job(JobKind.PLAN, gate.project_id, {"new_plan": True})
    rt.repo.mutate_project(gate.project_id, lambda pr: (setattr(pr, "plan_job_id", job.id), setattr(pr, "stage", Stage.PLANNING),
                                                        setattr(pr, "approved_spec_id", None), setattr(pr, "current_spec_id", None)))
    rt.bus.emit("project.stage", {"project_id": gate.project_id, "stage": Stage.PLANNING.value}, gate.project_id)
    return ApplyResult(close_gate=True, step="auto", step_result={"new_plan_job": job.id})


# ---------------------------------------------------------------------------------------------------- C3: the concept lock
def finalize_character(ctx: StepContext, spec: dict[str, Any], c: str, draft_sha: str, project: Project, counter: PL.CallCounter) -> dict[str, Any]:
    """I0 on one chosen draft (Sunburst, high), checked with A_DRIFT; one more try with a new nonce; then the draft is used and flagged."""
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts import registry as preg
    from duoskin.prompts.catalog import default_ctx

    rt = ctx.rt
    cp = compiler.compile("I0.finalize", spec, c, {"asset": "concept", "transparent": False, "style_ref": False},
                          default_ctx(kits.load_context(rt).inventory))
    route = preg.get("I0.finalize").meta.route.get("final") or {}
    pins = project.pins.models if project.pins else {}
    model, quality = str(pins.get("image_final") or route.get("model", "")), str(route.get("quality", "high"))
    draft_png = rt.cas.get(draft_sha)
    draft = common.open_image(draft_png)
    out: dict[str, Any] = {"used": "draft", "attempts": 0, "checks": []}
    for attempt in range(2):
        req = ImageRequest(model=model, prompt=cp.text, size=SIZE, quality=quality, background="opaque", n=1, images=(named(draft_png, "image1.png"),),
                           image1_role="edit_target", nonce=ctx.step.nonce + (f":redo{attempt}" if attempt else ""), tag="I0")
        raws, rid, _ = image_call(ctx, req, 1)
        raw_sha = sha256_of(raws[0])
        final = common.open_image(raws[0]).convert("RGB")
        chk = runner.run_check("A_DRIFT", raw_sha, lambda final=final, raw_sha=raw_sha: C.check_drift(final, draft, bg_hex=BG, subject_sha=raw_sha))
        out["checks"].append(r_summary(chk))
        out.setdefault("results", []).append(chk)
        out["attempts"] = attempt + 1
        if chk.passed:
            out.update({"used": "final", "png": common.png_bytes(final), "request_id": rid})
            return out
    ctx.emit("toast", {"message": f"The final redraw of character {c.upper()} changed too much. The approved draft is used instead.", "level": "warn"})
    out["png"] = draft_png
    out["flag"] = "drift_fallback"
    return out


def part_boxes(spec: dict[str, Any], c: str) -> dict[str, tuple[int, int, int, int]]:
    """Where each part of one character is in the finalized concept (front slot), from the code guide's regions: face, hair, shirt, pants,
    one box per accessory and per print region, and the shoe band. Part ids follow ``pipeline.common.PART_RE``."""
    guide = build_guide(spec, c)
    reg_ = guides.build_guide_regions(guide)["figures"]["front"]            # type: ignore[index]
    geo = guides.figure_geometry(guide.slots["front"].cx)
    s = guides.PX_PER_STUD
    out: dict[str, tuple[int, int, int, int]] = {}
    out[f"{c}.face"] = tuple(reg_["head_box"])                              # type: ignore[assignment]
    out[f"{c}.hair"] = tuple(reg_["hair_box"])                              # type: ignore[assignment]
    out[f"{c}.shirt"] = (geo["arm_l"][0], geo["torso"][1], geo["arm_r"][2], geo["torso"][3])
    out[f"{c}.pants"] = (geo["leg_l"][0], geo["leg_l"][1], geo["leg_r"][2], geo["leg_l"][3])
    att = reg_["attachments"]
    for i, acc in enumerate(spec[c].get("accessories") or []):
        x, y = att.get(acc.get("attachment", ""), [guide.slots["front"].cx, guides.NECK_Y])
        out[f"{c}.acc.{i}"] = (int(x) - int(1.5 * s), int(y) - int(1.5 * s), int(x) + int(1.5 * s), int(y) + int(1.5 * s))
    for j, _pr in enumerate(spec[c]["top"].get("prints") or []):
        out[f"{c}.print.top.{j}"] = out[f"{c}.shirt"]
    for j, _pr in enumerate(spec[c]["bottom"].get("prints") or []):
        out[f"{c}.print.bottom.{j}"] = out[f"{c}.pants"]
    if spec[c]["bottom"]["shoes"].get("motif"):
        out[f"{c}.print.shoes.0"] = tuple(reg_["shoe_band"]["right"])        # type: ignore[assignment]
    return out


def clamp_box(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return max(0, x0), max(0, y0), min(size[0], x1), min(size[1], y1)


def palette_lock(rt: Runtime, rec: SpecRecord, finals: dict[str, Image.Image], choice: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract the colours of the approved picture per zone and snap or confirm them against the plan (``imaging.palette.snap_or_confirm``,
    snap within dE 10). By default a colour that differs is taken from the picture ("use the colour from the picture"); ``planned`` keeps the
    planned colours. A change is kept only while the spec still passes the plan linter's HARD rules: otherwise the planned colour stays."""
    spec = {**rec.spec, "palette": [dict(c) for c in rec.spec["palette"]]}
    index = {c["id"]: i for i, c in enumerate(spec["palette"])}
    ctx_lint = LI.lint_context(rt, rt.repo.get_project(rec.project_id), recent_cards=[])
    log_: list[dict[str, Any]] = []
    for c in CHARS:
        guide = build_guide(spec, c)
        zmasks = guides.zone_masks(guide, "front")
        targets = {"top": (spec[c]["top"]["base_ref"], zmasks["torso"]),
                   "bottom": (spec[c]["bottom"]["base_ref"], zmasks["leg_l"] | zmasks["leg_r"])}
        extracted, planned, refs = {}, {}, {}
        for zone, (ref, mask) in targets.items():
            dom = C.dominant_colour(finals[c], mask, exclude_hex=[BG])
            if dom is None or ref not in index:
                continue
            extracted[f"{c}.{zone}"], planned[f"{c}.{zone}"], refs[f"{c}.{zone}"] = dom, spec["palette"][index[ref]]["hex"], ref
        for dec in P.snap_or_confirm(extracted, planned):
            ref = refs[dec.zone]
            entry = {"zone": dec.zone, "planned": dec.planned, "picture": dec.extracted, "de": round(dec.de, 1), "action": dec.action, "used": dec.planned}
            if dec.action == "confirm" and choice == "picture":
                trial = {**spec, "palette": [dict(x) for x in spec["palette"]]}
                trial["palette"][index[ref]]["hex"] = dec.extracted.upper()
                if not LI.lint_candidates([("lock", trial)], ctx_lint, check_set=False).hard_findings("lock"):
                    spec = trial
                    entry["used"] = dec.extracted.upper()
                else:
                    entry["kept_for_lint"] = True
            log_.append(entry)
    return spec, log_


def run_lock(ctx: StepContext, p: LockParams, inputs: list[Any]) -> StepResult:
    """C3: finalize both chosen drafts (I0, A_DRIFT), extract the palette from the approved picture, write the locked spec and the DNA card,
    the per-duo style sheet and the part crops, then hand over to the part board."""
    rt = ctx.rt
    project = rt.repo.get_project(p.project_id)
    rec = rt.repo.get_spec(p.spec_id)
    sts = {c: get_state(rt, p.plan_set_id, p.slot, c) for c in CHARS}
    if not all(sts[c].get("chosen") for c in CHARS):
        raise StepFailure("the approved plan has no drawings to lock", kind="bad_request", billed="no")
    counter = PL.CallCounter()
    finals: dict[str, dict[str, Any]] = {}
    for n, c in enumerate(CHARS):
        ctx.progress(0.1 + 0.3 * n, f"final drawing of character {c.upper()}")
        finals[c] = finalize_character(ctx, rec.spec, c, sts[c]["chosen"]["sha"], project, counter)
    ims = {c: common.open_image(finals[c]["png"]).convert("RGB") for c in CHARS}
    ctx.record_checks([r for c in CHARS for r in finals[c].get("results", [])])
    ctx.progress(0.7, "reading the colours of the picture")
    locked_spec, palette_log = palette_lock(rt, rec, ims, p.palette_choice)
    snapped = sum(1 for e in palette_log if e["used"].upper() == e["planned"].upper())
    ctx.record_checks([runner.build_result("CHK-G1-08", passed=True, subject_sha=sha256_of(locked_spec), metric="palette_snap", value=float(snapped),
                                           evidence=f"{snapped} of {len(palette_log)} zone colours snapped to the plan; the picture used is "
                                                    + ", ".join(f"{c.upper()}: {finals[c]['used']}" for c in CHARS))])
    pv = common.prov("mock" if common.is_mock(rt, "openai") else "openai", provider="openai", prompt_id="I0.finalize", nonce=ctx.step.nonce,
                     params={"spec_id": p.spec_id, "palette": palette_log})
    sheet = guides.assemble_concept_sheet(ims["a"], ims["b"])
    record = common.put_png(ctx, sheet, role="concept_of_record", part_id=None, provenance=pv, status="final")
    for c in CHARS:
        common.put_png(ctx, ims[c], role=f"concept_of_record.{c}", part_id=None, provenance=pv, status="final")
        common.put_png(ctx, ims[c], role=f"style_sheet_{c}", part_id=None, provenance=pv, status="final")
    common.put_png(ctx, guides.downscale_for_judges(sheet), role="style_sheet_judge", part_id=None, provenance=pv, status="final")
    crops: dict[str, str] = {}
    for c in CHARS:
        kinds: dict[str, str] = {}
        for pid, box in part_boxes(locked_spec, c).items():
            crop = guides.prepare_part_crop(ims[c], clamp_box(box, ims[c].size))
            sha = common.put_png(ctx, crop, role=f"crop.{pid}", part_id=None, provenance=pv, status="final").sha256
            crops[pid] = sha
            kinds.setdefault(pid.split(".")[1], sha)
        for kind, sha in kinds.items():
            common.link_asset(rt, p.project_id, None, sha, f"crop.{c}.{kind}", "final", pv)
        if kinds:
            common.link_asset(rt, p.project_id, None, next(iter(kinds.values())), f"crop.{c}", "final", pv)
    parent = rec
    new = PL.new_spec_record(rt, p.project_id, p.plan_set_id, parent.plan_index, locked_spec, parent=parent, version=parent.version,
                             created_by="palette_lock", palette_source="concept_extracted", status="approved", locked=True, source="concept_extracted")
    PL.save_spec(rt, parent.model_copy(update={"status": "superseded"}))
    rt.repo.mutate_project(p.project_id, lambda pr: (setattr(pr, "approved_spec_id", new.id), setattr(pr, "current_spec_id", new.id)))
    gate1 = [g for g in rt.repo.list_gates(p.project_id) if g.kind == GateKind.CONCEPT]
    _ = gate1
    ctx.emit("spec.updated", {"spec_id": new.id, "locked": True})
    job = PL.start_parts(p.project_id, rt)
    return StepResult(outputs=[record.sha256],
                      result={"spec_id": new.id, "parent_spec_id": parent.id, "palette": palette_log, "crops": sorted(crops),
                              "finalize": {c: {k: v for k, v in finals[c].items() if k not in ("png", "results")} for c in CHARS},
                              "parts_job": getattr(job, "id", None)},
                      message="the concept is locked; the part board is starting")


def estimate_lock(p: LockParams) -> float:
    from duoskin.providers import pricing

    return float(pricing.estimate_openai_image(quality="high", n=1, n_input_images=1, prompt_chars=500).usd) * 2


# ---------------------------------------------------------------------------------------------------- registration
def register_handlers() -> None:
    reg.register_handler("concept.char", run_char, version=1, pool="api", paid=True, provider="openai", Params=CharParams,
                         estimate=estimate_char, cacheable=False)
    reg.register_handler("concept.assemble", run_assemble, version=1, pool="api", paid=True, provider="anthropic", Params=AssembleParams,
                         estimate=estimate_assemble, cacheable=False)
    reg.register_handler("concept.gate", run_gate, version=1, pool="cpu", paid=False, Params=GateParams, cacheable=False)
    reg.register_handler("concept.lock", run_lock, version=1, pool="api", paid=True, provider="openai", Params=LockParams,
                         estimate=estimate_lock, cacheable=False)


def register(rt: Runtime | None = None) -> None:
    """Called by ``pipeline.register``: the concept handlers and, with a runtime, the Gate 1 applier."""
    register_handlers()
    if rt is not None:
        rt.gates.register_applier(GateKind.CONCEPT, apply_concept)

