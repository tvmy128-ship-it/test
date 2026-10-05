"""The plan loop: the PLAN job from the brief to the concept previews (APP_SPEC §8.2, §10.2; bible §9).

::

    plan.reference (L1, only with references)   plan.taste (L2, only when the data changed)
              \\                                  /
               plan.planner (L3: three specs, exactly one is the wildcard)
                      |
               plan.lint (C1: HARD rules per spec and for the set; ingests the planner's answer)
                      |
        plan.critic x3 (L4)   plan.pairwise x3 (L5, both orders)
                      \\          /
               plan.revise (L6: HARD findings + high-severity critic fixes) -> plan.lint
                      |                  (a second round only for specs that still fail HARD lint)
        a spec that still fails after 2 rounds is dropped: ONE replacement request (it keeps the wildcard role)
                      |
               plan.select (final order; the wildcard is always among the shown plans, or the notice says why not)
                      |
               concept steps (pipeline/concept.py): I1 front and back per character, C2, then Gate 1

Design rules (they are what the tests pin down):

* **A plan that fails HARD lint is revised, never shown.** ``plan.select`` shows only HARD-clean specs.
* **No worked example.** L3 receives the brief, the structure request, the must-include lines, the reference analysis, the taste
  profile, the last 5 DNA cards, ``<recently_used>`` and ``<avoid>``: nothing that could be copied (PROPOSAL_DECISION).
* **Never pay twice.** Every Claude call goes through ``llm_call``, which serves an identical request (same prompt, same images, same
  nonce) from the content cache. A retried step, a resumed job or a regression run therefore costs nothing for what already ran.
* **Steps do the paid work, ``plan.lint`` and ``plan.select`` write the specs.** The paid steps are pure functions of their parameters;
  ``plan.lint`` reads the planner's answer from its output asset and the reviser's patches from its result, so a cached answer
  produces the same records.
* Dynamic fan-out: ``plan.lint`` and ``plan.select`` spawn the next steps into the job (``ctx.spawn``) once they know what the next step has
  to do (which specs need a revision, which were dropped).

The module also owns the hand-off to the part board: ``start_parts(project_id)`` (called after the concept lock; the PARTS job is built by
another lane, which may register a starter with ``register_parts_starter``).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError

from duoskin import __version__
from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.engine import registry as reg
from duoskin.engine.cache import CachedResult, pixel_sha
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import StepResult
from duoskin.engine.scheduler import register_job_factory
from duoskin.models.common import Sha256, Strict, new_id, sha256_of
from duoskin.models.dna import DnaCard, card_from_spec
from duoskin.models.job import JobKind, Step
from duoskin.models.llm_io import (
    Critique,
    PairJudgment,
    ReferenceAnalysis,
    Revision,
    TasteProfile,
)
from duoskin.models.project import Project, Stage, VersionPins
from duoskin.models.spec import BriefConstraint, PlanSet
from duoskin.models.spec_record import SpecRecord
from duoskin.pipeline import brief as BR
from duoskin.pipeline import change as CH
from duoskin.pipeline import common
from duoskin.pipeline import lint as LI

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.plan")

ROUTES: dict[str, str] = {"L1": "L1_reference", "L2": "L2_taste", "L3": "L3_planner", "L4": "L4_critic", "L5": "L5_pairwise",
                          "L6": "L6_reviser", "L7": "L7_change", "L9": "L9_hair_match", "L10": "L10_repair", "L11": "L11_checker",
                          "L12": "L12_duo_judge", "L13": "L13_ip", "L14": "L14_similarity", "L15": "L11_checker"}
MAX_REVISION_ROUNDS = int(TH.get("revise.max_rounds"))
LEVEL_POINTS = {"fail": 0, "weak": 1, "ok": 2, "strong": 3}      # bible §9.5
LIVE = ("candidate", "shown", "approved")
SHOWN_PLANS = 3
STORY_KEYS = ("story",)


# ---------------------------------------------------------------------------------------------------- step parameters
class PlanStep(Strict):
    project_id: str
    plan_set_id: str


class ReferenceParams(PlanStep):
    ref_shas: list[Sha256] = Field(default_factory=list)
    note: str = ""


class TasteParams(PlanStep):
    has_data: bool = False
    tables_sha: str = ""


class PlannerParams(PlanStep):
    reference_step: str = ""
    taste_step: str = ""
    replacement: bool = False
    wildcard: bool = False
    replaces: str = ""                      # the dropped spec's id (a replacement request)
    dropped_reasons: list[str] = Field(default_factory=list)
    round: int = 0                          # how many times "New plan" was used (a new request, never a cache hit)


class LintParams(PlanStep):
    round: int = 0
    mode: Literal["initial", "revised", "replacement"] = "initial"
    from_step: str = ""
    replaces: str = ""


class CriticParams(PlanStep):
    spec_id: str = ""                       # empty: the spec the lint step ingested as a replacement (``replacement_spec`` of its result)
    label: str = "X"
    lint_step: str = ""


class PairParams(PlanStep):
    first_id: str
    second_id: str
    lint_step: str = ""


class ReviseParams(PlanStep):
    round: int = 1
    lint_step: str = ""
    critic_steps: list[str] = Field(default_factory=list)


class SelectParams(PlanStep):
    lint_steps: list[str] = Field(default_factory=list)
    critic_steps: list[str] = Field(default_factory=list)
    pair_steps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------------- LLM calls
class CallCounter:
    """Gives each Claude call of one handler run a distinct ledger operation (the ledger is idempotent per step, attempt, operation)."""

    def __init__(self) -> None:
        self.n = 0
        self._lock = threading.Lock()

    def next(self, prefix: str) -> str:
        with self._lock:
            self.n += 1
            return f"{prefix}#{self.n}"


@dataclass
class LlmOut:
    parsed: Any
    cached: bool = False
    thinking: str = ""
    request_id: str | None = None
    model: str = ""
    usd: float = 0.0


def route_of(template_id: str) -> str:
    family = template_id.split(".")[0]
    return ROUTES.get(family, family)


def make_call_ctx(ctx: StepContext, tag: str = "") -> Any:
    """A ``providers.base.CallCtx`` with heartbeat, cancel, progress, the remote-ref hook and the template tag (mock fault selectors)."""
    import dataclasses

    base = ctx.call_ctx()
    try:
        return dataclasses.replace(base, set_remote_ref=ctx.set_remote_ref, tag=tag)
    except TypeError:
        return base


def image_blocks(images: Sequence[bytes]) -> list[dict[str, Any]]:
    from duoskin.providers.anthropic_llm import image_block

    return [image_block(bytes(b)) for b in images]


def _llm_key(template_id: str, prompt: Any, schema_name: str, images: Sequence[bytes], nonce: str) -> str:
    from duoskin.prompts.system_blocks import system_sha

    return sha256_of({"call": template_id, "version": prompt.template_version, "prompt": prompt.sha256, "system": system_sha(prompt.system),
                      "schema": schema_name, "images": [pixel_sha(b) for b in images], "nonce": nonce, "kind": "llm"})


def llm_call(ctx: StepContext, template_id: str, inputs: dict[str, Any], *, images: Sequence[bytes] = (), counter: CallCounter | None = None,
             nonce: str = "", op: str | None = None, validate_context: dict[str, Any] | None = None, think: bool = False) -> LlmOut:
    """Compile an LLM role template and send it (the one way a plan or change handler talks to Claude).

    * An identical request (prompt text, system blocks, images, nonce) is served from the content cache: no call, no cost.
    * Otherwise the provider is called through ``ctx.provider("anthropic")`` (real, mock or disabled as configured), the cost is
      recorded under a distinct ledger operation, and the validated answer is cached. A schema violation records its cost and raises
      the provider's ``ProviderError(kind="validation")`` with the raw text in ``context["raw_text"]``.
    * ``think=True`` emits the summarised thinking (400 characters at most) as an ``llm.thinking`` event for the Plan page.
    """
    from duoskin.prompts.llm import compile_llm

    seed = kit_order_seed(ctx)
    return send_prompt(ctx, compile_llm(template_id, inputs, order_seed=seed), images=images, counter=counter, nonce=nonce, op=op,
                       validate_context=validate_context, think=think)


def kit_order_seed(ctx: StepContext) -> int | None:
    """The order seed of the project round this step belongs to (``None`` for a step that has no project). Every Claude route of one round
    gets the same seed, so they share one prompt-cache prefix; a new project or a "New plan" shows the kits in another order (no kit id is
    listed first in every duo's prompt). The seed is logged."""
    from duoskin.prompts.system_blocks import order_seed

    try:
        pid = getattr(ctx, "project_id", None) or getattr(getattr(ctx, "step", None), "project_id", None)
        if not pid:
            return None
        seed = order_seed(str(pid), BR.plan_round(ctx.rt, str(pid)))
    except Exception:    # noqa: BLE001 - the order is a nicety; a context without a runtime simply gets the sorted blocks
        return None
    log.debug("kit inventory order seed %s (project %s)", seed, pid)
    return seed


def send_prompt(ctx: StepContext, prompt: Any, *, images: Sequence[bytes] = (), counter: CallCounter | None = None, nonce: str = "",
                op: str | None = None, validate_context: dict[str, Any] | None = None, think: bool = False) -> LlmOut:
    """Send an already compiled ``LlmPrompt`` (``llm_call`` and the Gate B judge both end here): content cache, provider call, ledger row."""
    from duoskin.prompts.llm import schema_class
    from duoskin.providers.anthropic_llm import text_block

    rt = ctx.rt
    template_id = prompt.template_id
    schema = schema_class(prompt)
    key = _llm_key(template_id, prompt, schema.__name__, images, nonce)
    hit = rt.cache.lookup(key)
    if hit is not None and isinstance(hit.result.get("parsed"), dict):
        try:
            parsed = schema.model_validate(hit.result["parsed"], context=validate_context)
            if think and hit.result.get("thinking"):
                ctx.emit("llm.thinking", {"text": str(hit.result["thinking"])[:400], "cached": True})
            return LlmOut(parsed, True, str(hit.result.get("thinking", "")), hit.result.get("request_id"), str(hit.result.get("model", "")))
        except (ValidationError, ValueError):
            rt.cache.drop(key)
    content = [*image_blocks(images), text_block(prompt.user_text)]
    adapter = ctx.provider("anthropic")
    scale = int((ctx.hints or {}).get("max_tokens_scale", 1) or 1)
    override = int(prompt.route.get("max_tokens", 0)) * scale if scale > 1 else None
    counter = counter or CallCounter()
    operation = op or counter.next(f"messages.stream:{template_id.split('.')[0]}")
    ctx.check_cancel()
    try:
        result = adapter.call(route_of(template_id), system=prompt.system, content=content, out=schema, ctx=make_call_ctx(ctx, template_id),
                              prompt_version=prompt.template_version, max_tokens_override=override)
    except Exception as exc:
        common.record_cost(ctx, getattr(exc, "cost", None), operation, fallback_provider="anthropic")
        raise
    entry = common.record_cost(ctx, getattr(result, "cost", None), operation, fallback_provider="anthropic")
    thinking = str(getattr(result, "thinking_summary", "") or "")
    if think and thinking:
        ctx.emit("llm.thinking", {"text": thinking[:400]})
    parsed = result.parsed
    rt.cache.store(key, CachedResult(step_kind=f"llm:{template_id}", outputs=[],
                                     result={"parsed": parsed.model_dump(mode="json"), "thinking": thinking[:800],
                                             "request_id": getattr(result, "request_id", None),
                                             "model": getattr(result, "served_model", "")}))
    return LlmOut(parsed, False, thinking, getattr(result, "request_id", None), getattr(result, "served_model", ""),
                  float(entry.usd) if entry is not None else 0.0)


def gate_b_provider(ctx: StepContext, images: Sequence[bytes], counter: CallCounter | None = None, nonce: str = "") -> Callable[[Any], Any]:
    """The ``ProviderFn`` of ``checks.gate_b.run_gate_b``: the compiled L11 prompt with the candidate images, through ``send_prompt``."""
    counter = counter or CallCounter()

    def fn(req: Any) -> Any:
        return send_prompt(ctx, req.prompt, images=images, counter=counter, nonce=f"{nonce}:{req.attempt}:{req.vote}").parsed

    return fn


def estimate_llm(rt: Runtime | None, template_id: str, *, calls: int = 1, in_tokens: int = 9000, out_tokens: int = 3000, cached: int = 6000) -> float:
    """A USD estimate for ``calls`` Claude calls of a template (for step estimates and the budget gate, never for billing)."""
    from duoskin.providers import pricing

    family = template_id.split(".")[0]
    s = rt.effective_settings().models if rt is not None else None
    model = {"L3": s.planner if s else "claude-opus-5", "L1": s.planner if s else "claude-opus-5", "L6": s.planner if s else "claude-opus-5",
             "L7": s.planner if s else "claude-opus-5", "L4": s.critic if s else "claude-opus-5", "L5": s.critic if s else "claude-opus-5",
             "L12": s.judge if s else "claude-opus-5"}.get(family, s.checker if s else "claude-sonnet-5")
    return float(pricing.estimate_claude(model, input_tokens=in_tokens, output_tokens=out_tokens, cached_input_tokens=cached).usd) * calls


def _is_mock(rt: Runtime, provider: str = "anthropic") -> bool:
    return common.is_mock(rt, provider)


def _prov(rt: Runtime, **kw: Any):
    return common.prov("mock" if _is_mock(rt) else "anthropic", **kw)


# ---------------------------------------------------------------------------------------------------- spec store
def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_for_critic(spec: dict[str, Any]) -> str:
    """Canonical JSON with the story removed (verbosity bias, bible §9.5)."""
    doc = json.loads(json.dumps(spec))
    (doc.get("world") or {}).pop("story", None)
    return canonical(doc)


def save_spec(rt: Runtime, rec: SpecRecord) -> SpecRecord:
    """Write a changed ``SpecRecord`` back (lint results, critic levels, rank, status). Specs are immutable in content: only the
    bookkeeping fields change; a content change is a new record (``new_spec_record``)."""
    with rt.db.tx() as c:
        c.execute("UPDATE specs SET status=?, sha256=?, json=? WHERE id=?", (rec.status, rec.sha256, rec.model_dump_json(), rec.id))
    return rec


def new_spec_record(rt: Runtime, project_id: str, plan_set_id: str, plan_index: int, spec: dict[str, Any], *, parent: SpecRecord | None = None,
                    version: int | None = None, created_by: str = "planner", ops: Sequence[Any] = (), palette_source: str = "planner",
                    status: str = "candidate", locked: bool = False, source: str | None = None, emit: bool = True) -> SpecRecord:
    """Create and store a ``SpecRecord`` and its ``DnaCard`` (version = the record's version). A patch passes ``parent`` (version + 1),
    the palette lock keeps the parent's version, a planner answer is version 1."""
    ver = version if version is not None else ((parent.version + 1) if parent else 1)
    rec = SpecRecord(id=new_id("spc"), project_id=project_id, plan_set_id=plan_set_id, plan_index=plan_index,
                     parent_spec_id=parent.id if parent else None, version=ver, created_by=created_by,   # type: ignore[arg-type]
                     patch_from_parent=list(ops), spec=spec, palette_source=palette_source, status=status,   # type: ignore[arg-type]
                     sha256=sha256_of(spec))
    rt.repo.add_spec(rec)
    previous = None
    if parent is not None:
        raw = rt.repo.get_dna_card(parent.id)
        previous = DnaCard.model_validate(raw) if raw else None
    card = card_from_spec(rec, locked=locked, source=source or {"planner": "planner", "reviser": "planner", "change": "change",
                                                                "palette_lock": "concept_extracted"}.get(created_by, "planner"),
                          previous=previous)
    rt.repo.save_dna_card(rec.id, rec.version, project_id, card.model_dump(mode="json"))
    if emit:
        rt.bus.emit("spec.updated", {"project_id": project_id, "spec_id": rec.id, "plan_set_id": plan_set_id, "version": rec.version},
                    project_id)
    return rec


def plan_records(rt: Runtime, project_id: str, plan_set_id: str, *, live_only: bool = True) -> list[SpecRecord]:
    """The records of one plan set, one per plan index (the newest live one), in plan order."""
    recs = [r for r in rt.repo.list_specs(project_id) if r.plan_set_id == plan_set_id]
    by_index: dict[int, SpecRecord] = {}
    for r in recs:                                    # list_specs is oldest first: a later record replaces an earlier one
        if live_only and r.status not in LIVE:
            continue
        by_index[r.plan_index] = r
    return [by_index[i] for i in sorted(by_index)]


def envelope_key(plan_set_id: str) -> str:
    return f"planset:{plan_set_id}"


def get_envelope(rt: Runtime, plan_set_id: str) -> dict[str, Any]:
    return dict(rt.repo.kv_get(envelope_key(plan_set_id)) or {"brief_constraints": [], "how_they_differ": "", "dropped": [], "notice": ""})


def set_envelope(rt: Runtime, plan_set_id: str, env: dict[str, Any]) -> None:
    rt.repo.kv_set(envelope_key(plan_set_id), env)


def _step_result(rt: Runtime, step_id: str) -> dict[str, Any]:
    if not step_id:
        return {}
    s = rt.repo.find_step(step_id)
    return dict(s.result) if s is not None else {}


def _step_outputs(rt: Runtime, step_id: str) -> list[str]:
    s = rt.repo.find_step(step_id) if step_id else None
    return list(s.outputs) if s is not None else []


def _new_step(rt: Runtime, kind: str, job_id: str, project_id: str, params: Strict, *, deps: Sequence[str] = (), nonce: str = "",
              priority: int = 100) -> Step:
    return rt.ops.new_step(kind, job_id=job_id, project_id=project_id, params=params.model_dump(mode="json"), deps=list(deps), nonce=nonce,
                           priority=priority)


# ---------------------------------------------------------------------------------------------------- pins
def house_style_version(rt: Runtime) -> int:
    """The newest ``house_style_v<N>.png`` (0 when there is no sheet: the pins then say ``none``, APP_SPEC §16)."""
    import re

    best = 0
    folder = rt.paths.kits_dir / "style"
    if folder.is_dir():
        for p in folder.glob("house_style_v*.png"):
            m = re.match(r"house_style_v(\d+)\.png$", p.name)
            if m:
                best = max(best, int(m.group(1)))
    return best


def make_pins(rt: Runtime) -> VersionPins:
    """The versions a project pins when it leaves BRIEF (ENG-08): models, prompt versions, schema hashes, house style, kit manifest,
    thresholds and rules."""
    from duoskin.models.llm_io import RULES_VERSION
    from duoskin.pipeline import kits
    from duoskin.prompts import registry as preg
    from duoskin.prompts.catalog import data_json

    m = rt.effective_settings().models
    models = {k: v for k, v in m.model_dump().items() if isinstance(v, str)}
    inv = kits.load_context(rt).inventory
    style_version = int(data_json("style_guide.json")["version"])
    return VersionPins(models=models, prompt_versions=preg.prompt_versions(), schema_hashes=preg.schema_hashes(),
                       house_style_version=house_style_version(rt), style_guide_version=style_version, kit_manifest_sha=inv.sha256,
                       thresholds_version=TH.THRESHOLDS_VERSION, rules_version=RULES_VERSION, app_version=__version__)


# ---------------------------------------------------------------------------------------------------- the job
def plan_job_factory(rt: Runtime, job: Any, project: Project | None) -> list[Step]:
    """The first steps of a PLAN job: L1 (only with references), L2, then L3. The rest of the loop is spawned by ``plan.lint`` and
    ``plan.select`` as they learn what is needed. Pins are frozen here (the project leaves BRIEF)."""
    if project is None:
        raise StepFailure("a PLAN job needs a project", kind="bad_request", billed="no")
    if project.pins is None:
        pins = make_pins(rt)
        rt.repo.mutate_project(project.id, lambda p: setattr(p, "pins", pins), bump=False)
        project = rt.repo.get_project(project.id)
    plan_set_id = new_id("pls")
    steps: list[Step] = []
    deps: list[str] = []
    refs = [r for r in project.references if r.role == "reference"]
    ref_step = taste_step = ""
    if refs:
        s = _new_step(rt, "plan.reference", job.id, project.id, ReferenceParams(project_id=project.id, plan_set_id=plan_set_id,
                                                                                ref_shas=[r.asset_sha for r in refs], note=refs[0].note))
        steps.append(s)
        deps.append(s.id)
        ref_step = s.id
    tables = taste_tables(rt, exclude_project=project.id)
    s2 = _new_step(rt, "plan.taste", job.id, project.id, TasteParams(project_id=project.id, plan_set_id=plan_set_id,
                                                                      has_data=taste_has_data(tables), tables_sha=sha256_of(tables)))
    steps.append(s2)
    deps.append(s2.id)
    taste_step = s2.id
    rnd = BR.plan_round(rt, project.id)
    planner = _new_step(rt, "plan.planner", job.id, project.id,
                        PlannerParams(project_id=project.id, plan_set_id=plan_set_id, reference_step=ref_step, taste_step=taste_step, round=rnd),
                        deps=deps, nonce=f"plan{rnd}" if rnd else "", priority=20)
    steps.append(planner)
    steps.append(_new_step(rt, "plan.lint", job.id, project.id, LintParams(project_id=project.id, plan_set_id=plan_set_id, round=0, mode="initial",
                                                                          from_step=planner.id), deps=[planner.id]))
    return steps


# ---------------------------------------------------------------------------------------------------- L1: reference analysis
def _clean_analysis(analysis: ReferenceAnalysis) -> tuple[ReferenceAnalysis, list[str]]:
    """Gate A of L1 (bible §9.1): every axis at most 3 times; each rule passes the free-text lint; no noun phrase of ``do_not_copy`` in any
    rule (token overlap). A failing rule is dropped (never edited). Returns the cleaned analysis and what was dropped."""
    import re

    from duoskin.prompts import freetext
    from duoskin.prompts.catalog import default_ctx

    bans = default_ctx().banned
    avoid_words = {w for phrase in analysis.do_not_copy for w in re.findall(r"[a-z]{4,}", phrase.lower())}
    kept, dropped, per_axis = [], [], {}
    for r in analysis.rules:
        problems = freetext.free_text_problems(r.rule, None, bans, strict=False)
        leak = sorted(w for w in re.findall(r"[a-z]{4,}", r.rule.lower()) if w in avoid_words)
        if per_axis.get(r.axis, 0) >= 3:
            dropped.append(f"{r.axis}: more than 3 rules on one axis")
        elif problems or leak:
            dropped.append(f"{r.axis}: " + ("; ".join(str(p) for p in problems) or f"copies: {', '.join(leak)}"))
        else:
            kept.append(r)
            per_axis[r.axis] = per_axis.get(r.axis, 0) + 1
    return analysis.model_copy(update={"rules": kept}), dropped


def run_reference(ctx: StepContext, p: ReferenceParams, inputs: list[Any]) -> StepResult:
    """L1: study the reference set (cached by the image hashes) and return content-free construction rules."""
    from duoskin.imaging import guides

    ctx.progress(0.1, "reading your reference pictures")
    imgs: list[bytes] = []
    for sha in p.ref_shas:
        im = common.open_image(ctx.read_asset(sha))
        imgs.append(common.png_bytes(guides.vlm_image(im, with_checkerboard=False)))
    out = llm_call(ctx, "L1.reference_analyst", {"user_note": p.note or ""}, images=imgs, counter=CallCounter(), think=True)
    analysis, dropped = _clean_analysis(out.parsed)
    flags = [f.model_dump() for f in analysis.brand_or_character_flags if f.confidence in ("medium", "high")]
    if flags:
        ctx.emit("toast", {"message": "Your reference may contain a brand or a known character. The plan will not copy it.", "level": "warn"})
    return StepResult(result={"analysis": analysis.model_dump(mode="json"), "dropped_rules": dropped, "flags": flags, "cached": out.cached},
                      message=f"{len(analysis.rules)} construction rules")


def estimate_reference(p: ReferenceParams) -> float:
    return estimate_llm(None, "L1.reference_analyst", in_tokens=4000 + 1400 * len(p.ref_shas), out_tokens=6000, cached=3000)


# ---------------------------------------------------------------------------------------------------- L2: taste profile
def taste_tables(rt: Runtime, *, exclude_project: str | None = None) -> dict[str, Any]:
    """The frequency tables of the taste profile, computed in code (the model only interprets them, FAILURE_MODES PLN-17): for each spec
    field, each value with the ids of the approved duos and of the plans that were shown and not chosen. Drill labels never count."""
    approved = BR.approved_specs(rt, exclude_project=exclude_project, limit=200)
    rows = rt.db.conn().execute("SELECT s.id AS id, s.json AS json FROM specs s WHERE s.status IN ('superseded','dropped') "
                                "AND s.version = 1 ORDER BY s.created_at DESC LIMIT 200").fetchall()
    rejected: list[dict[str, Any]] = []
    for r in rows:
        try:
            rec = json.loads(r["json"])
        except ValueError:
            continue
        if rec.get("project_id") != exclude_project and rec.get("created_by") == "planner" and rec.get("rank") is not None:
            rejected.append({"id": rec.get("id", r["id"]), "spec": rec.get("spec", {})})     # shown to the person and not chosen

    def fields_of(spec: dict[str, Any]) -> dict[str, list[str]]:
        w = spec.get("world", {})
        chars = [spec.get("a", {}), spec.get("b", {})]
        return {
            "pair_structure": [w.get("pair_structure", "")],
            "theme_family": [str(w.get("theme", "")).split(" ")[0].lower()],
            "palette_temperature": [str(w.get("palette_family", "")).split("_")[0]],
            "saturation": [str(w.get("palette_family", "")).split("_")[-1]],
            "hair_style": [c.get("hair", {}).get("kit_style_id", "") for c in chars],
            "garment_recipe": [c.get("top", {}).get("recipe_id", "") for c in chars],
            "print_density": [str(len(c.get("top", {}).get("prints", []))) for c in chars],
            "accessory_kind": [a.get("kind", "") for c in chars for a in c.get("accessories", [])],
            "face_eyes": [c.get("face", {}).get("eye_shape", "") for c in chars],
            "face_mouth": [c.get("face", {}).get("mouth_style", "") for c in chars],
            "anchor_kind": [a.get("kind", "") for a in spec.get("shared_anchors", [])],
        }

    table: dict[str, dict[str, dict[str, list[str]]]] = {}
    for kind, items in (("approved", [(a["spec_id"], a["spec"]) for a in approved]), ("rejected", [(r["id"], r["spec"]) for r in rejected])):
        for sid, spec in items:
            for field_name, values in fields_of(spec).items():
                for v in dict.fromkeys(values):
                    if v:
                        table.setdefault(field_name, {}).setdefault(v, {"approved": [], "rejected": []})[kind].append(sid)
    return {"fields": table, "approved_count": len(approved), "rejected_count": len(rejected)}


def taste_has_data(tables: dict[str, Any]) -> bool:
    """The profile needs at least two approved duos (a like needs two examples, PLN-17); without data L2 is skipped."""
    return int(tables.get("approved_count", 0)) >= 2


def _clean_taste(profile: TasteProfile, tables: dict[str, Any]) -> TasteProfile:
    """Gate A of L2: every ``evidence_ids`` entry exists in the tables, and a like or dislike has at least two of them (PLN-17)."""
    known = {sid for vals in tables.get("fields", {}).values() for row in vals.values() for ids in row.values() for sid in ids}

    def keep(rule: Any) -> bool:
        ids = [e for e in rule.evidence_ids if e in known]
        return len(set(ids)) >= 2

    return profile.model_copy(update={"likes": [r for r in profile.likes if keep(r)], "dislikes": [r for r in profile.dislikes if keep(r)]})


def run_taste(ctx: StepContext, p: TasteParams, inputs: list[Any]) -> StepResult:
    """L2: interpret the code-computed frequency tables as soft preferences (skipped without data)."""
    if not p.has_data:
        return StepResult(result={"skipped": True, "profile": None}, message="no taste data yet")
    tables = taste_tables(ctx.rt, exclude_project=p.project_id)
    ref = {}
    out = llm_call(ctx, "L2.taste_builder", {"frequency_tables": canonical(tables), "rating_summary": canonical({"approved": tables["approved_count"],
                                                                                                                  "rejected": tables["rejected_count"]}),
                                             "reference_rules": canonical(ref) if ref else "none"}, counter=CallCounter())
    profile = _clean_taste(out.parsed, tables)
    doc = {"version": int(ctx.rt.repo.kv_get("taste.version") or 0) + 1, "tables_sha": p.tables_sha, "profile": profile.model_dump(mode="json")}
    ctx.rt.repo.kv_set("taste.version", doc["version"])
    path = ctx.rt.paths.user_data_dir / "taste_profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return StepResult(result={"profile": profile.model_dump(mode="json"), "version": doc["version"]}, message="taste profile rebuilt")


def estimate_taste(p: TasteParams) -> float:
    return estimate_llm(None, "L2.taste_builder", in_tokens=3000, out_tokens=2000, cached=2000) if p.has_data else 0.0


# ---------------------------------------------------------------------------------------------------- L3: planner
def planner_inputs_for(ctx: StepContext, p: PlannerParams) -> dict[str, Any]:
    rt = ctx.rt
    project = rt.repo.get_project(p.project_id)
    analysis = _step_result(rt, p.reference_step).get("analysis")
    taste = _step_result(rt, p.taste_step).get("profile")
    if taste is None:
        f = rt.paths.user_data_dir / "taste_profile.json"
        if f.exists():
            try:
                taste = json.loads(f.read_text(encoding="utf-8")).get("profile")
            except (OSError, ValueError):
                taste = None
    rules = (analysis or {}).get("rules") if analysis else None
    taste_doc = {"profile": taste, "reference_rules": rules} if (taste or rules) else None
    return BR.planner_inputs(rt, project, reference_analysis=analysis, taste_profile=taste_doc, replacement=p.replacement,
                             wildcard=p.wildcard, dropped_reasons=p.dropped_reasons)


def run_planner(ctx: StepContext, p: PlannerParams, inputs: list[Any]) -> StepResult:
    """L3: three complete specs (or one replacement spec). The answer goes to the CAS as JSON; ``plan.lint`` ingests it."""
    from duoskin.providers.base import ProviderError

    rt = ctx.rt
    ctx.progress(0.05, "the planner is writing")
    inputs_ = planner_inputs_for(ctx, p)
    seed = kit_order_seed(ctx)
    log.info("planner %s: kit inventory order seed %s, structure suggestion %s", p.plan_set_id, seed, inputs_.get("structure_suggestion", "none"))
    counter = CallCounter()
    problems: list[str] = []
    try:
        out = llm_call(ctx, "L3.planner", inputs_, counter=counter, nonce=ctx.step.nonce, validate_context={"skip_rules": True}, think=True)
        plan: PlanSet = out.parsed
        cached = out.cached
        thinking, rid, model = out.thinking, out.request_id, out.model
    except ProviderError as exc:
        raw = (exc.context or {}).get("raw_text") if exc.kind == "validation" else None
        if not raw:
            raise
        try:
            plan = PlanSet.model_validate_json(raw, context={"skip_rules": True})
        except (ValidationError, ValueError) as inner:
            raise StepFailure("the planner's answer could not be read", kind="bad_request", billed="yes",
                              user_hint="The planner's answer did not have the expected shape. Try again.") from inner
        problems = [str(e.get("msg", ""))[:200] for e in (exc.context or {}).get("errors", [])[:6]]
        cached, thinking, rid, model = False, "", getattr(exc, "request_id", None), ""
    data = json.dumps(plan.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    asset = common.put_bytes(ctx, data, "json", role="plan_set", part_id=None,
                             provenance=_prov(rt, prompt_id="L3.planner", prompt_version=1, nonce=ctx.step.nonce, request_id=rid, model=model or None,
                                              params={"replacement": p.replacement, "plan_set_id": p.plan_set_id, "order_seed": seed}))
    return StepResult(outputs=[asset.sha256], result={"plan_sha": asset.sha256, "specs": len(plan.specs), "rule_problems": problems,
                                                      "thinking": thinking[:400], "cached": cached, "request_id": rid, "order_seed": seed,
                                                      "structure_suggestion": inputs_.get("structure_suggestion", "")},
                      message=f"{len(plan.specs)} spec(s) written")


def estimate_planner(p: PlannerParams) -> float:
    return estimate_llm(None, "L3.planner", in_tokens=14000, out_tokens=(8000 if p.replacement else 24000), cached=9000)


# ---------------------------------------------------------------------------------------------------- ingest and lint
def _load_plan(ctx: StepContext, step_id: str) -> PlanSet:
    shas = _step_outputs(ctx.rt, step_id)
    if not shas:
        raise StepFailure("the planner step has no answer to read", kind="bad_request", billed="no")
    return PlanSet.model_validate_json(ctx.read_asset(shas[0]), context={"skip_rules": True})


def _ingest_initial(ctx: StepContext, p: LintParams) -> list[SpecRecord]:
    rt = ctx.rt
    plan = _load_plan(ctx, p.from_step)
    existing = plan_records(rt, p.project_id, p.plan_set_id, live_only=False)
    if existing:
        return plan_records(rt, p.project_id, p.plan_set_id)         # a retried step: the records already exist
    recs = []
    for i, spec in enumerate(plan.specs[:SHOWN_PLANS]):
        recs.append(new_spec_record(rt, p.project_id, p.plan_set_id, i, spec.model_dump(mode="json")))
    for i in range(SHOWN_PLANS, len(plan.specs)):                    # more than the contract asks for: the extra ones are never shown
        extra = new_spec_record(rt, p.project_id, p.plan_set_id, i, plan.specs[i].model_dump(mode="json"), status="dropped")
        recs.append(extra)
    env = get_envelope(rt, p.plan_set_id)
    env.update({"brief_constraints": [c.model_dump(mode="json") for c in plan.brief_constraints], "how_they_differ": plan.how_they_differ,
                "project_id": p.project_id, "extra_specs": max(0, len(plan.specs) - SHOWN_PLANS)})
    set_envelope(rt, p.plan_set_id, env)
    return [r for r in recs if r.status in LIVE]


def _apply_revisions(ctx: StepContext, p: LintParams) -> list[dict[str, Any]]:
    """Apply the reviser's patches (``plan.revise`` result) to the live specs; returns one note per spec. A rejected patch keeps the old
    spec and uses up the round."""
    rt = ctx.rt
    res = _step_result(rt, p.from_step)
    notes: list[dict[str, Any]] = []
    live = {r.id: r for r in plan_records(rt, p.project_id, p.plan_set_id)}
    for spec_id, rev in (res.get("revisions") or {}).items():
        rec = live.get(spec_id)
        if rec is None or not rev.get("patch"):
            continue
        out = CH.apply_patch(rec.spec, rev["patch"], kind="reviser", finding_paths=rev.get("finding_paths", []), subject_sha=rec.sha256)
        if out.check is not None:
            ctx.record_checks([out.check])                      # CHK-G0-11: the patch stayed inside its findings
        if not out.ok or out.spec is None:
            notes.append({"spec_id": spec_id, "applied": False, "problems": out.problems})
            continue
        new = new_spec_record(rt, p.project_id, p.plan_set_id, rec.plan_index, out.spec, parent=rec, created_by="reviser", ops=out.ops,
                              status="candidate")
        old = rec.model_copy(update={"status": "superseded"})
        save_spec(rt, old)
        notes.append({"spec_id": spec_id, "new_spec_id": new.id, "applied": True, "version": new.version, "note": rev.get("note", ""),
                      "ops": len(out.ops)})
    env_rev = res.get("envelope")
    if env_rev and env_rev.get("patch"):
        env = get_envelope(rt, p.plan_set_id)
        doc = {"brief_constraints": env.get("brief_constraints", []), "how_they_differ": env.get("how_they_differ", "")}
        out = CH.apply_envelope_patch(doc, env_rev["patch"], env_rev.get("finding_paths", []))
        if out.ok and out.spec is not None:
            env.update(out.spec)
            set_envelope(rt, p.plan_set_id, env)
            notes.append({"spec_id": "envelope", "applied": True})
        else:
            notes.append({"spec_id": "envelope", "applied": False, "problems": out.problems})
    return notes


def _ingest_replacement(ctx: StepContext, p: LintParams) -> SpecRecord:
    """Ingest the single spec of a replacement request at the plan index of the spec it replaces (or the missing index)."""
    rt = ctx.rt
    plan = _load_plan(ctx, p.from_step)
    if not plan.specs:
        raise StepFailure("the replacement answer has no spec", kind="bad_request", billed="no")
    spec = plan.specs[0].model_dump(mode="json")
    if p.replaces.startswith("missing:"):
        index = int(p.replaces.split(":", 1)[1])
    else:
        old = next((r for r in rt.repo.list_specs(p.project_id) if r.id == p.replaces), None)
        index = old.plan_index if old else SHOWN_PLANS - 1
    sha = sha256_of(spec)
    for r in plan_records(rt, p.project_id, p.plan_set_id):
        if r.plan_index == index and r.sha256 == sha:                # a retried step: already ingested
            return r
    return new_spec_record(rt, p.project_id, p.plan_set_id, index, spec, status="candidate")


def _lint_findings_by_spec(bundle: LI.LintBundle) -> dict[str, list[dict[str, Any]]]:
    return {sid: LI.reviser_findings(bundle.hard_findings(sid)) for sid in bundle.order}


def measured_facts(rec: SpecRecord, results: Sequence[CheckResult]) -> dict[str, Any]:
    """The facts the critic and the ranker get next to the spec (bible §9.5): linter results, the differing CHARACTER fields, restraint
    counts, the hair IoU and the nearest-card share. Counts and distances come from code, never from the model's eye."""
    spec = LI.parse_loose(rec.spec)
    card = card_from_spec(rec)
    from duoskin.models.dna import character_field_differences

    values: dict[str, Any] = {}
    for r in results:
        if r.value is not None and r.metric:
            values[r.metric] = round(float(r.value), 3)
    return {"hard_failed": sorted({r.metric for r in results if r.kind in ("hard", "assert") and not r.passed}),
            "warnings": sorted({r.metric for r in results if r.kind == "soft" and r.ran and not r.passed}),
            "values": values, "differing_character_fields": character_field_differences(card),
            "accessories": {"a": len(spec.a.accessories), "b": len(spec.b.accessories)},
            "detail_level": spec.world.detail_level, "pair_structure": spec.world.pair_structure}


def run_lint(ctx: StepContext, p: LintParams, inputs: list[Any]) -> StepResult:
    """C1. Round 0 ingests the planner's answer, later rounds apply the reviser's patches, a replacement ingests one new spec; then every
    live spec is linted with the plan-set rules, the results are stored on the records, and the next steps are spawned."""
    rt = ctx.rt
    project = rt.repo.get_project(p.project_id)
    notes: list[dict[str, Any]] = []
    replacement_id = ""
    if p.mode == "initial":
        _ingest_initial(ctx, p)
    elif p.mode == "revised":
        notes = _apply_revisions(ctx, p)
    else:
        replacement_id = _ingest_replacement(ctx, p).id
    recs = plan_records(rt, p.project_id, p.plan_set_id)
    env = get_envelope(rt, p.plan_set_id)
    cons = [BriefConstraint.model_validate(c) for c in env.get("brief_constraints", [])]
    lctx = LI.lint_context(rt, project, recent_cards=BR.recent_cards(rt, p.project_id))
    bundle = LI.lint_candidates([(r.id, r.spec) for r in recs], lctx, brief_constraints=cons, how_they_differ=env.get("how_they_differ", ""))
    if p.mode == "replacement":                  # the wildcard count is settled by the notice, the set rules ran in the rounds before
        bundle = bundle.relaxed()
    ctx.progress(0.6, "the rules check ran")
    all_results: list[CheckResult] = []
    summary: list[dict[str, Any]] = []
    for r in recs:
        results = bundle.results(r.id)
        save_spec(rt, r.model_copy(update={"lint": results}))
        rt.repo.kv_set(f"speclint:{r.id}", {"clean": bundle.clean(r.id), "hard": [f.rule for f in bundle.hard_findings(r.id)]})
        all_results.extend(results)
        summary.append({"spec_id": r.id, "plan_index": r.plan_index, "version": r.version, "clean": bundle.clean(r.id),
                        "hard": [f.rule for f in bundle.hard_findings(r.id)], "warnings": len(bundle.warnings(r.id))})
    ctx.record_checks(all_results)
    result: dict[str, Any] = {"round": p.round, "mode": p.mode, "specs": summary, "findings": _lint_findings_by_spec(bundle),
                              "envelope_findings": LI.reviser_findings(bundle.envelope_findings),
                              "global_findings": LI.reviser_findings(bundle.global_findings), "unclean": bundle.unclean(),
                              "revision_notes": notes, "set_clean": bundle.set_clean, "order": bundle.order,
                              "replacement_spec": replacement_id}
    if p.mode != "replacement":
        _spawn_after_lint(ctx, p, recs, bundle, result)
    rt.bus.emit("spec.updated", {"project_id": p.project_id, "plan_set_id": p.plan_set_id, "lint": True}, p.project_id)
    return StepResult(result=result, message=f"{len(bundle.unclean())} spec(s) need a revision" if bundle.unclean() else "all plans pass the rules")


def _spawn_after_lint(ctx: StepContext, p: LintParams, recs: list[SpecRecord], bundle: LI.LintBundle, result: dict[str, Any]) -> None:
    """The next steps of the loop (see the module docstring). Called from inside the running ``plan.lint`` step, so every new step depends
    on it."""
    rt, job_id, me = ctx.rt, ctx.step.job_id, ctx.step.id
    pid, psid = p.project_id, p.plan_set_id
    unclean = bundle.unclean()
    need_set_fix = bool(bundle.envelope_findings)
    if p.round == 0:
        critics = [] if rt.effective_settings().checks.cheap_critic_mode else _critic_step(rt, job_id, pid, psid, recs, me)
        pairs = _pair_steps(rt, job_id, pid, psid, recs, me)
        rev = _new_step(rt, "plan.revise", job_id, pid, ReviseParams(project_id=pid, plan_set_id=psid, round=1, lint_step=me,
                                                                    critic_steps=[s.id for s in critics]),
                        deps=[me, *[s.id for s in critics]])
        lint1 = _new_step(rt, "plan.lint", job_id, pid, LintParams(project_id=pid, plan_set_id=psid, round=1, mode="revised", from_step=rev.id),
                          deps=[rev.id])
        ctx.spawn([*critics, *pairs, rev, lint1])
        return
    if (unclean or need_set_fix) and p.round < MAX_REVISION_ROUNDS:
        rev = _new_step(rt, "plan.revise", job_id, pid, ReviseParams(project_id=pid, plan_set_id=psid, round=p.round + 1, lint_step=me), deps=[me])
        nxt = _new_step(rt, "plan.lint", job_id, pid, LintParams(project_id=pid, plan_set_id=psid, round=p.round + 1, mode="revised",
                                                                from_step=rev.id), deps=[rev.id])
        ctx.spawn([rev, nxt])
        return
    # the last round: a spec that still fails HARD lint is dropped (never shown) and gets ONE replacement request
    dropped: list[SpecRecord] = []
    env = get_envelope(rt, psid)
    for r in recs:
        if r.id in unclean:
            reasons = [f.message for f in bundle.hard_findings(r.id)][:3]
            save_spec(rt, r.model_copy(update={"status": "dropped"}))
            dropped.append(r)
            env["dropped"] = [*env.get("dropped", []), {"spec_id": r.id, "plan_index": r.plan_index, "wildcard": bool(r.spec.get("is_wildcard")),
                                                        "reasons": reasons}]
    set_envelope(rt, psid, env)
    result["dropped"] = [r.id for r in dropped]
    kept = [r for r in recs if r.id not in unclean]
    kept_wild = any(r.spec.get("is_wildcard") for r in kept)
    taken = {r.plan_index for r in kept} | {r.plan_index for r in dropped}
    wanted: list[tuple[str, bool, list[str]]] = []                   # (replaces, wildcard, reasons)
    for r in dropped:
        wanted.append((r.id, bool(r.spec.get("is_wildcard")), [f.message for f in bundle.hard_findings(r.id)][:3]))
    for i in range(SHOWN_PLANS):
        if i not in taken:
            wanted.append((f"missing:{i}", False, ["a plan is missing from the set"]))
    if wanted and not kept_wild and not any(w[1] for w in wanted):
        wanted[0] = (wanted[0][0], True, wanted[0][2])               # the set still needs its wildcard
    steps: list[Step] = []
    tails: list[str] = []
    for replaces, wild, why in wanted:
        pl = _new_step(rt, "plan.planner", job_id, pid, PlannerParams(project_id=pid, plan_set_id=psid, replacement=True, wildcard=wild,
                                                                     replaces=replaces, dropped_reasons=why, round=BR.plan_round(rt, pid)),
                       deps=[me], nonce=f"repl:{replaces}", priority=20)
        li = _new_step(rt, "plan.lint", job_id, pid, LintParams(project_id=pid, plan_set_id=psid, round=p.round + 1, mode="replacement",
                                                               from_step=pl.id, replaces=replaces), deps=[pl.id])
        steps += [pl, li]
        tails.append(li.id)
        if not rt.effective_settings().checks.cheap_critic_mode:
            cr = _new_step(rt, "plan.critic", job_id, pid, CriticParams(project_id=pid, plan_set_id=psid, spec_id="", label="W", lint_step=li.id),
                           deps=[li.id])
            steps.append(cr)
            tails.append(cr.id)
    pair_ids, critic_ids = _pair_chain(rt, job_id, psid), _critic_chain(rt, job_id, psid)
    sel = _new_step(rt, "plan.select", job_id, pid, SelectParams(project_id=pid, plan_set_id=psid, lint_steps=[me, *[s.id for s in steps if s.kind == "plan.lint"]],
                                                                critic_steps=[*critic_ids, *[s.id for s in steps if s.kind == "plan.critic"]],
                                                                pair_steps=pair_ids),
                    deps=[me, *tails, *pair_ids, *critic_ids])          # the selection waits for every score it ranks by
    ctx.spawn([*steps, sel])


def _chain(rt: Runtime, job_id: str, plan_set_id: str, kind: str) -> list[str]:
    return [s.id for s in rt.repo.list_steps(job_id=job_id) if s.kind == kind and s.params.get("plan_set_id") == plan_set_id]


def _lint_chain(rt: Runtime, job_id: str, plan_set_id: str) -> list[str]:
    return _chain(rt, job_id, plan_set_id, "plan.lint")


def _critic_chain(rt: Runtime, job_id: str, plan_set_id: str) -> list[str]:
    return _chain(rt, job_id, plan_set_id, "plan.critic")


def _pair_chain(rt: Runtime, job_id: str, plan_set_id: str) -> list[str]:
    return _chain(rt, job_id, plan_set_id, "plan.pairwise")


def _critic_step(rt: Runtime, job_id: str, pid: str, psid: str, recs: list[SpecRecord], dep: str) -> list[Step]:
    labels = "XYZUVW"
    return [_new_step(rt, "plan.critic", job_id, pid, CriticParams(project_id=pid, plan_set_id=psid, spec_id=r.id,
                                                                   label=labels[r.plan_index % len(labels)], lint_step=dep), deps=[dep])
            for r in recs]


def _pair_steps(rt: Runtime, job_id: str, pid: str, psid: str, recs: list[SpecRecord], dep: str) -> list[Step]:
    """L5 once per pair (both orders inside the step). Cheap mode (Settings): L5 only, on the two non-wildcard specs."""
    pool = recs
    if rt.effective_settings().checks.cheap_critic_mode:
        pool = [r for r in recs if not r.spec.get("is_wildcard")][:2]
    out = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            out.append(_new_step(rt, "plan.pairwise", job_id, pid, PairParams(project_id=pid, plan_set_id=psid, first_id=pool[i].id,
                                                                             second_id=pool[j].id, lint_step=dep), deps=[dep]))
    return out


# ---------------------------------------------------------------------------------------------------- L4 / L5
def _check_scores(scores: Sequence[Any], criteria: Sequence[str]) -> bool:
    """Gate A of L4 and L5: every criterion exactly once."""
    seen = [s.criterion for s in scores]
    return sorted(seen) == sorted(criteria)


def _resolves(spec: dict[str, Any], pointer: str) -> bool:
    from duoskin.engine.deps import resolve

    sentinel = object()
    return pointer.startswith("/") and resolve(spec, pointer, sentinel) is not sentinel


def run_critic(ctx: StepContext, p: CriticParams, inputs: list[Any]) -> StepResult:
    """L4: an independent critique of one spec in a fresh context (anonymised, canonical JSON, the story removed, code facts supplied)."""
    from duoskin.models.llm_io import CRITERIA

    rt = ctx.rt
    spec_id = p.spec_id or str(_step_result(rt, p.lint_step).get("replacement_spec") or "")
    if not spec_id:
        return StepResult(result={"spec_id": "", "levels": {}, "points": 0, "fixes": [], "complete": False}, message="no plan to critique")
    rec = rt.repo.get_spec(spec_id)
    project = rt.repo.get_project(p.project_id)
    facts = measured_facts(rec, rec.lint)
    wildcard = bool(rec.spec.get("is_wildcard"))
    inputs_ = {"spec_id": p.label, "spec_json": spec_for_critic(rec.spec), "measured_facts": canonical(facts), "brief_text": project.brief.strip() or BR.NONE,
               "wildcard": wildcard}
    taste_doc = _taste_for_critic(rt)
    if taste_doc and not wildcard:
        inputs_["taste_profile"] = taste_doc
    counter = CallCounter()
    out = llm_call(ctx, "L4.critic", inputs_, counter=counter, nonce=ctx.step.nonce)
    crit: Critique = out.parsed
    complete = _check_scores(crit.scores, CRITERIA)
    if not complete:                                    # one repeat, then take what is usable
        out = llm_call(ctx, "L4.critic", inputs_, counter=counter, nonce="retry")
        crit = out.parsed
        complete = _check_scores(crit.scores, CRITERIA)
    levels: dict[str, str] = {}
    for s in crit.scores:
        levels.setdefault(s.criterion, s.level)
    ranking_only = ("structure_readable", "accessory_pair_expresses")
    points = sum(LEVEL_POINTS[v] for k, v in levels.items() if not (wildcard and k == "taste_fit"))
    fixes = [f.model_dump() for f in crit.fixes if _resolves(rec.spec, f.path)]
    return StepResult(result={"spec_id": spec_id, "levels": levels, "points": points, "fixes": fixes, "complete": complete,
                              "ranking_only": list(ranking_only), "wildcard": wildcard, "cached": out.cached},
                      message=f"{len(levels)} criteria scored")


def _taste_for_critic(rt: Runtime) -> str:
    f = rt.paths.user_data_dir / "taste_profile.json"
    if not f.exists():
        return ""
    try:
        return canonical(json.loads(f.read_text(encoding="utf-8")).get("profile"))
    except (OSError, ValueError):
        return ""


def estimate_critic(p: CriticParams) -> float:
    return estimate_llm(None, "L4.critic", in_tokens=9000, out_tokens=3000, cached=6000)


def run_pairwise(ctx: StepContext, p: PairParams, inputs: list[Any]) -> StepResult:
    """L5: one pair in both orders. Two orders that agree give a win; a disagreement or a tie gives each plan half a win."""
    from duoskin.models.llm_io import CRITERIA

    rt = ctx.rt
    a, b = rt.repo.get_spec(p.first_id), rt.repo.get_spec(p.second_id)
    project = rt.repo.get_project(p.project_id)
    wildcard_involved = bool(a.spec.get("is_wildcard") or b.spec.get("is_wildcard"))
    counter = CallCounter()
    orders: list[dict[str, Any]] = []
    for first, second in ((a, b), (b, a)):
        facts = {"first": measured_facts(first, first.lint), "second": measured_facts(second, second.lint)}
        inputs_ = {"first_json": spec_for_critic(first.spec), "second_json": spec_for_critic(second.spec), "measured_facts": canonical(facts),
                   "brief_text": project.brief.strip() or BR.NONE, "wildcard_involved": wildcard_involved}
        taste = _taste_for_critic(rt)
        if taste and not wildcard_involved:
            inputs_["taste_profile"] = taste
        out = llm_call(ctx, "L5.pairwise_ranker", inputs_, counter=counter, nonce=ctx.step.nonce + f":{first.id[-6:]}")
        pj: PairJudgment = out.parsed
        overall = pj.overall
        orders.append({"first": first.id, "second": second.id, "overall": overall,
                       "complete": sorted(c.criterion for c in pj.per_criterion) == sorted(CRITERIA)})

    def winner(o: dict[str, Any]) -> str | None:
        return o["first"] if o["overall"] == "first" else o["second"] if o["overall"] == "second" else None

    w1, w2 = winner(orders[0]), winner(orders[1])
    if w1 is not None and w1 == w2:
        wins = {w1: 1.0, (p.second_id if w1 == p.first_id else p.first_id): 0.0}
    else:
        wins = {p.first_id: 0.5, p.second_id: 0.5}
    return StepResult(result={"pair": [p.first_id, p.second_id], "orders": orders, "wins": wins, "agree": w1 is not None and w1 == w2},
                      message="both orders judged")


def estimate_pairwise(p: PairParams) -> float:
    return estimate_llm(None, "L5.pairwise_ranker", calls=2, in_tokens=11000, out_tokens=3000, cached=6000)


# ---------------------------------------------------------------------------------------------------- L6: the reviser
def run_revise(ctx: StepContext, p: ReviseParams, inputs: list[Any]) -> StepResult:
    """L6: fix exactly the findings (HARD lint findings, and in round 1 the critic's high-severity fixes). The patch is checked here (a dry
    run, so the plan log can show what was rejected) and applied by the next ``plan.lint``."""
    rt = ctx.rt
    lint_res = _step_result(rt, p.lint_step)
    findings: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in (lint_res.get("findings") or {}).items()}
    if p.round == 1:
        for sid in p.critic_steps:
            cr = _step_result(rt, sid)
            if cr.get("spec_id"):
                extra = LI.critic_findings(cr.get("fixes", []), start=len(findings.get(cr["spec_id"], [])) + 1)
                findings.setdefault(cr["spec_id"], []).extend(extra)
    counter = CallCounter()
    revisions: dict[str, Any] = {}
    live = {r.id: r for r in plan_records(rt, p.project_id, p.plan_set_id)}
    todo = [(sid, fs) for sid, fs in findings.items() if fs and sid in live]
    for n, (sid, fs) in enumerate(todo):
        ctx.progress(0.1 + 0.8 * n / max(1, len(todo)), "improving a plan")
        rec = live[sid]
        out = llm_call(ctx, "L6.reviser", {"spec_json": canonical(rec.spec), "findings": LI.findings_text(fs)}, counter=counter,
                       nonce=f"r{p.round}")
        rev: Revision = out.parsed
        paths = LI.finding_paths(fs)
        dry = CH.apply_patch(rec.spec, [o.model_dump() for o in rev.patch], kind="reviser", finding_paths=paths, subject_sha=rec.sha256)
        revisions[sid] = {"patch": [o.model_dump() for o in rev.patch], "note": rev.note[:300], "finding_paths": paths, "findings": len(fs),
                          "dry_run_ok": dry.ok, "problems": dry.problems[:4]}
    envelope: dict[str, Any] | None = None
    env_findings = list(lint_res.get("envelope_findings") or [])
    if env_findings:
        env = get_envelope(rt, p.plan_set_id)
        doc = {"brief_constraints": env.get("brief_constraints", []), "how_they_differ": env.get("how_they_differ", "")}
        out = llm_call(ctx, "L6.reviser", {"spec_json": canonical(doc), "findings": LI.findings_text(env_findings)}, counter=counter, nonce=f"e{p.round}")
        rev = out.parsed
        paths = LI.finding_paths(env_findings)
        envelope = {"patch": [o.model_dump() for o in rev.patch], "note": rev.note[:300], "finding_paths": paths}
    return StepResult(result={"round": p.round, "revisions": revisions, "envelope": envelope, "revised": sorted(revisions)},
                      message=f"{len(revisions)} plan(s) revised" if revisions else "nothing to revise")


def estimate_revise(p: ReviseParams) -> float:
    return estimate_llm(None, "L6.reviser", calls=3, in_tokens=9000, out_tokens=2500, cached=6000)


# ---------------------------------------------------------------------------------------------------- selection
@dataclass
class Ranked:
    rec: SpecRecord
    wins: float = 0.0
    points: int = 0
    novelty: float = 0.0           # lower nearest-card share is better (a tie-break only)
    levels: dict[str, str] = field(default_factory=dict)

    def key(self) -> tuple[float, int, float, int]:
        return (-self.wins, -self.points, self.novelty, self.rec.plan_index)


def select_shown(ranked: Sequence[Ranked], *, limit: int = SHOWN_PLANS) -> list[Ranked]:
    """The plans Gate 1 shows: the best ``limit`` by wins, then critic points, then the novelty tie-break. **The wildcard is always among
    them** (the best-ranked wildcard takes a slot whatever its rank) and there is never more than one: a second wildcard is not shown."""
    order = sorted(ranked, key=Ranked.key)
    wild = next((r for r in order if r.rec.spec.get("is_wildcard")), None)
    rest = [r for r in order if not r.rec.spec.get("is_wildcard")]
    shown = rest[:limit - 1] if wild is not None else rest[:limit]
    if wild is not None:
        shown.append(wild)
    return sorted(shown, key=Ranked.key)


def lineage_value(rt: Runtime, rec: SpecRecord, table: dict[str, Any]) -> Any:
    """The entry of ``table`` (keyed by spec id) for this record or its nearest ancestor: a revised spec inherits the score its earlier
    version got (the critics and rankers ran before the revision)."""
    seen: set[str] = set()
    cur: SpecRecord | None = rec
    while cur is not None and cur.id not in seen:
        if cur.id in table:
            return table[cur.id]
        seen.add(cur.id)
        cur = rt.repo.get_spec(cur.parent_spec_id) if cur.parent_spec_id else None
    return None


def run_select(ctx: StepContext, p: SelectParams, inputs: list[Any]) -> StepResult:
    """The final order of the plans and the start of the concept previews. HARD-clean plans only; the wildcard is always shown, or the
    visible notice says "wildcard could not be built"."""
    from duoskin.checks import plan_rules as PR
    from duoskin.pipeline import concept

    rt = ctx.rt
    recs = [r for r in plan_records(rt, p.project_id, p.plan_set_id) if r.status in LIVE]
    clean = [r for r in recs if bool((rt.repo.kv_get(f"speclint:{r.id}") or {}).get("clean"))]
    # scores from the critic and pairwise steps
    wins: dict[str, float] = {}
    points: dict[str, int] = {}
    levels: dict[str, dict[str, str]] = {}
    for sid in p.pair_steps:
        for spec_id, w in (_step_result(rt, sid).get("wins") or {}).items():
            wins[spec_id] = wins.get(spec_id, 0.0) + float(w)
    for sid in p.critic_steps:
        r_ = _step_result(rt, sid)
        if r_.get("spec_id"):
            points[r_["spec_id"]] = int(r_.get("points", 0))
            levels[r_["spec_id"]] = dict(r_.get("levels", {}))
    cards = BR.recent_cards(rt, p.project_id)
    ranked = []
    for r in clean:
        share = PR.nearest_card_share(LI.parse_loose(r.spec), cards) or 0.0
        ranked.append(Ranked(r, float(lineage_value(rt, r, wins) or 0.0), int(lineage_value(rt, r, points) or 0), share,
                             dict(lineage_value(rt, r, levels) or {})))
    shown = select_shown(ranked)
    shown_ids = {x.rec.id for x in shown}
    env = get_envelope(rt, p.plan_set_id)
    dropped_wild = any(d.get("wildcard") for d in env.get("dropped", [])) and not any(x.rec.spec.get("is_wildcard") for x in shown)
    shape = LI.gate1_set_shape([x.rec.spec for x in shown], wildcard_dropped=dropped_wild,
                               dropped_reasons=[r for d in env.get("dropped", []) for r in d.get("reasons", [])[:1]])
    env["notice"] = shape.notice
    env["shown"] = [x.rec.id for x in shown]
    set_envelope(rt, p.plan_set_id, env)
    for slot, x in enumerate(shown):
        rec = x.rec.model_copy(update={"status": "shown", "critic_levels": x.levels, "pairwise_wins": x.wins, "rank": slot})
        save_spec(rt, rec)
        x.rec = rec
    for r in recs:
        if r.id not in shown_ids and r.status in ("candidate", "shown"):
            save_spec(rt, r.model_copy(update={"status": "dropped"}))
    if shown:
        rt.repo.mutate_project(p.project_id, lambda pr: setattr(pr, "current_spec_id", shown[0].rec.id), bump=False)
    if not shown:
        raise StepFailure("no plan passed the rules after the revisions", kind="bad_request", billed="no",
                          user_hint="None of the plans passed the required rules. Try again with a different brief.")
    steps = concept.start_previews(rt, ctx.step.job_id, rt.repo.get_project(p.project_id), p.plan_set_id, [x.rec for x in shown], deps=[ctx.step.id])
    ctx.spawn(steps)
    rt.bus.emit("spec.updated", {"project_id": p.project_id, "plan_set_id": p.plan_set_id, "shown": [x.rec.id for x in shown]}, p.project_id)
    return StepResult(result={"shown": [x.rec.id for x in shown], "ranks": {x.rec.id: i for i, x in enumerate(shown)}, "wins": wins,
                              "points": points, "notice": shape.notice, "dropped": env.get("dropped", [])},
                      message=f"{len(shown)} plan(s) go on to the concept pictures")


# ---------------------------------------------------------------------------------------------------- the hand-off to the part board
_PARTS_STARTERS: list[Callable[..., Any]] = []
_starters_lock = threading.Lock()


def register_parts_starter(fn: Callable[..., Any]) -> None:
    """Register what starts the part board after the concept lock: ``fn(rt, project_id) -> Job | None`` (the PARTS lane, APP_SPEC §8.2).
    ``start_parts`` calls every registered starter, in order, and returns the first job one of them made."""
    with _starters_lock:
        if fn not in _PARTS_STARTERS:
            _PARTS_STARTERS.append(fn)


def start_parts(project_id: str, rt: Runtime | None = None) -> Any:
    """The hand-off after Gate 1 and the concept lock: the project advances to the part-board stage (a ``project.stage`` event) and the
    PARTS job starts. The job is built by the part-board lane: a registered starter (``register_parts_starter``), else the lane's
    ``pipeline.parts.start_parts_job``, else any registered PARTS job factory. Without any of them only the stage changes (the lane starts
    the job when it registers). Idempotent: a PARTS job of this project that is still running is returned instead of a second one."""
    from duoskin import config
    from duoskin.engine.scheduler import has_job_factory

    rt = rt or config.active_runtime()
    if rt is None:
        raise RuntimeError("start_parts needs a running app (or pass rt)")
    project = rt.repo.get_project(project_id)
    if project.stage not in (Stage.PARTS, Stage.GATE2):
        rt.repo.set_project_stage(project_id, Stage.PARTS, bus=rt.bus)
    for j in rt.repo.list_jobs(project_id=project_id, limit=50):
        if j.kind == JobKind.PARTS and j.state.value in ("running", "waiting_user", "paused"):
            return j
    with _starters_lock:
        starters = list(_PARTS_STARTERS)
    for fn in starters:
        job = fn(rt, project_id)
        if job is not None:
            return job
    try:
        from duoskin.pipeline import parts as parts_lane

        starter = getattr(parts_lane, "start_parts_job", None)
    except ImportError:
        starter = None
    if starter is not None and has_job_factory(JobKind.PARTS):
        return starter(rt, project_id)
    if has_job_factory(JobKind.PARTS):
        return rt.scheduler.submit_job(JobKind.PARTS, project_id, {}, spec_id=rt.repo.get_project(project_id).approved_spec_id)
    return None


# ---------------------------------------------------------------------------------------------------- registration
def register_handlers() -> None:
    """Register the plan-loop step handlers (process-wide; safe to call twice)."""
    reg.register_handler("plan.reference", run_reference, version=1, pool="api", paid=True, provider="anthropic", Params=ReferenceParams,
                         estimate=estimate_reference, cacheable=False)
    reg.register_handler("plan.taste", run_taste, version=1, pool="api", paid=True, provider="anthropic", Params=TasteParams,
                         estimate=estimate_taste, cacheable=False)
    reg.register_handler("plan.planner", run_planner, version=1, pool="api", paid=True, provider="anthropic", Params=PlannerParams,
                         estimate=estimate_planner, cacheable=False)
    reg.register_handler("plan.lint", run_lint, version=1, pool="cpu", paid=False, Params=LintParams, cacheable=False)
    reg.register_handler("plan.critic", run_critic, version=1, pool="api", paid=True, provider="anthropic", Params=CriticParams,
                         estimate=estimate_critic, cacheable=False)
    reg.register_handler("plan.pairwise", run_pairwise, version=1, pool="api", paid=True, provider="anthropic", Params=PairParams,
                         estimate=estimate_pairwise, cacheable=False)
    reg.register_handler("plan.revise", run_revise, version=1, pool="api", paid=True, provider="anthropic", Params=ReviseParams,
                         estimate=estimate_revise, cacheable=False)
    reg.register_handler("plan.select", run_select, version=1, pool="cpu", paid=False, Params=SelectParams, cacheable=False)


def register(rt: Runtime | None = None) -> None:
    """Called by ``pipeline.register``: the plan handlers and the PLAN job factory. The mock roles of the plan routes are loaded by
    ``providers.mock.llm`` itself (``pipeline.mock_roles``), so mock mode needs no import order."""
    register_handlers()
    register_job_factory(JobKind.PLAN, plan_job_factory)
