"""The brief and what the planner is told about it (APP_SPEC §3.2, §3.3, §3.7, §10.2, §13; bible §9.3).

This module turns a ``Project`` (the brief form) and the app's memory into the inputs of L3, and reads the plan set back into the facts the
Gate 1 card shows:

* ``structure_choices()`` is the "Pair structure" dropdown (``auto`` or one named structure, §2 S18). A named structure is a HARD lint
  (all 3 specs use it); ``auto`` is only an instruction to the planner (the mix is a SOFT warning);
* ``planner_inputs(rt, project, ...)`` builds the ``<user_brief>``, ``<structure_request>``, ``<must_include>``, ``<combo>``,
  ``<reference_analysis>``, ``<taste_profile>``, ``<recent_cards>`` (the last 5 DNA cards), ``<recently_used>`` (a derived view over the last
  10 approved duos) and ``<avoid>`` (plans rejected in this session, with the person's reasons) slots. There is **no example spec** in any
  of them (PROPOSAL_DECISION): the recent cards and the recently-used hints are the only things that change between runs of one brief;
* the hints are never a lint: nothing here caps, rations or blocks a kit id, a structure or a palette family (``test_no_novelty_lints``);
* ``structure_log`` / ``structure_shares`` log the structure picks of approved duos; ``planner_structure_lru_hint`` (default off) adds
  "least used recently" to ``<recently_used>`` once one structure passes ``calib.structure_collapse_share`` after 10 duos;
* ``brief_read_as`` and ``must_include_coverage`` feed the Gate 1 summary ("Your brief was read as ..." and the must-include list);
* ``estimate_plan_loop`` is the estimate shown next to the start button.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from duoskin.models.common import sha256_of
from duoskin.models.dna import DnaCard, recent_cards_json
from duoskin.models.spec import PAIR_STRUCTURES, DuoSpec

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime
    from duoskin.models.project import Project

RECENT_CARDS = 5           # the planner sees the last 5 cards (APP_SPEC §3.2)
RECENT_DUOS = 10           # recently_used is a view over the last 10 approved duos (APP_SPEC §6.13)
STRUCTURE_LOG_MIN = 10     # the LRU hint is offered after this many duos (APP_SPEC §3.3)
AVOID_KEEP = 6             # rejected plans a session remembers (the most recent ones)
NONE = "none"

STRUCTURE_LABELS = {
    "complement": "Complement: two clearly different main colours",
    "leader_chaotic": "Leader and chaotic partner",
    "same_club": "Same club: a team look with a shared main colour",
    "mirror": "Mirror: swapped colour roles",
    "seasonal_twins": "Seasonal twins: one theme, different seasons",
    "object_mascot": "Object mascot: a linked mascot accessory",
    "other": "Other (the plan describes it)",
}


# ---------------------------------------------------------------------------------------------------- the form
def structure_choices() -> list[dict[str, str]]:
    """The "Pair structure" dropdown: ``auto`` first, then every structure with a plain label."""
    return [{"value": "auto", "label": "Let the planner choose"},
            *({"value": s, "label": STRUCTURE_LABELS.get(s, s)} for s in PAIR_STRUCTURES)]


def valid_structure_request(value: str) -> bool:
    return value == "auto" or value in PAIR_STRUCTURES


def combo_words(combo: str) -> str:
    names = {"b": "boy", "g": "girl"}
    return f"character A is a {names[combo[0]]}, character B is a {names[combo[1]]}"


# ---------------------------------------------------------------------------------------------------- memory: approved duos
def _json_or(value: Any) -> str:
    """Canonical JSON for a slot (``none`` when empty): the same value always gives the same text, so the cache key is stable."""
    if value in (None, "", [], {}):
        return NONE
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def approved_specs(rt: Runtime, *, exclude_project: str | None = None, limit: int = RECENT_DUOS) -> list[dict[str, Any]]:
    """The newest approved spec of each other project, most recent first (``[{"project_id", "spec_id", "version", "spec"}]``).

    A project counts once: its approved record is the one with the latest creation time. Plans that were only shown, dropped or
    superseded never count."""
    rows = rt.db.conn().execute(
        "SELECT s.id AS spec_id, s.project_id AS project_id, s.version AS version, s.json AS json FROM specs s "
        "WHERE s.status='approved' ORDER BY s.created_at DESC, s.rowid DESC").fetchall()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["project_id"] == exclude_project or r["project_id"] in seen:
            continue
        seen.add(r["project_id"])
        try:
            rec = json.loads(r["json"])
        except ValueError:
            continue
        out.append({"project_id": r["project_id"], "spec_id": r["spec_id"], "version": r["version"], "spec": rec.get("spec", {})})
        if len(out) >= limit:
            break
    return out


def _cards_for(rt: Runtime, specs: Sequence[Mapping[str, Any]]) -> list[DnaCard]:
    cards: list[DnaCard] = []
    for s in specs:
        raw = rt.repo.get_dna_card(s["spec_id"])
        if raw is None:
            continue
        try:
            cards.append(DnaCard.model_validate(raw))
        except ValueError:
            continue
    return cards


def recent_cards(rt: Runtime, project_id: str | None = None, n: int = RECENT_CARDS) -> list[DnaCard]:
    """The DNA cards of the last ``n`` approved duos (oldest of those first, the order ``recent_cards_json`` expects)."""
    specs = approved_specs(rt, exclude_project=project_id, limit=n)
    return list(reversed(_cards_for(rt, specs)))


def _ordered_unique(values: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        if v and v != NONE:
            seen.setdefault(v, None)
    return list(seen)


def recently_used(rt: Runtime, project_id: str | None = None, n: int = RECENT_DUOS) -> dict[str, list[str]]:
    """The derived view of APP_SPEC §6.13 over the last ``n`` approved duos: hair kit ids, eye shapes, mouth styles, palette families,
    fabric ids, pair structures and anchor kinds, most recent first. A **hint** for the planner; no lint reads it."""
    hair: list[str] = []
    eyes: list[str] = []
    mouths: list[str] = []
    families: list[str] = []
    fabrics: list[str] = []
    structures: list[str] = []
    anchors: list[str] = []
    for item in approved_specs(rt, exclude_project=project_id, limit=n):
        sp = item["spec"]
        try:
            spec = DuoSpec.model_validate(sp, context={"skip_rules": True})
        except ValueError:
            continue
        for c in (spec.a, spec.b):
            hair.append(c.hair.kit_style_id)
            eyes.append(c.face.eye_shape)
            mouths.append(c.face.mouth_style)
            fabrics += [c.top.fabric_id, c.bottom.fabric_id]
        families.append(spec.world.palette_family)
        structures.append(spec.world.pair_structure)
        anchors += [a.kind for a in spec.shared_anchors]
    return {"hair_kit_ids": _ordered_unique(hair), "eye_shapes": _ordered_unique(eyes), "mouth_styles": _ordered_unique(mouths),
            "palette_families": _ordered_unique(families), "fabric_ids": _ordered_unique(fabrics),
            "pair_structures": _ordered_unique(structures), "anchor_kinds": _ordered_unique(anchors)}


# ---------------------------------------------------------------------------------------------------- the structure log
def structure_log(rt: Runtime, *, limit: int = 100) -> list[str]:
    """The pair structure of every approved duo, newest first (a view over approved specs, APP_SPEC §3.3)."""
    out: list[str] = []
    for item in approved_specs(rt, limit=limit):
        out.append(str(item["spec"].get("world", {}).get("pair_structure", "")))
    return [s for s in out if s]


def structure_shares(rt: Runtime) -> dict[str, float]:
    """Share of each structure among the logged duos (the Learning page shows these)."""
    log = structure_log(rt)
    if not log:
        return {}
    total = len(log)
    return {s: c / total for s, c in Counter(log).most_common()}


def structure_collapse(rt: Runtime) -> str | None:
    """The structure that passes ``calib.structure_collapse_share`` after at least 10 duos (the page then suggests the LRU hint), else None."""
    from duoskin.prompts.limits import thr

    log = structure_log(rt)
    if len(log) < STRUCTURE_LOG_MIN:
        return None
    share = Counter(log).most_common(1)[0]
    return share[0] if share[1] / len(log) > float(thr("calib.structure_collapse_share")) else None


def least_used_structures(rt: Runtime) -> list[str]:
    """Structures ordered from the least to the most used among the logged duos (never-used ones first)."""
    counts = Counter(structure_log(rt))
    return sorted(PAIR_STRUCTURES, key=lambda s: (counts.get(s, 0), PAIR_STRUCTURES.index(s)))


# ---------------------------------------------------------------------------------------------------- the avoid list
def _session_key(project_id: str) -> str:
    return f"plan_session:{project_id}"


def avoid_entries(rt: Runtime, project_id: str) -> list[dict[str, Any]]:
    """Plans the person rejected in this session, each with their reason (empty on the first run)."""
    data = rt.repo.kv_get(_session_key(project_id)) or {}
    return list(data.get("rejected", []))[-AVOID_KEEP:]


def plan_summary(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The compact description of a rejected plan that goes into ``<avoid>``: enough to avoid repeating it, never the whole spec."""
    w = spec.get("world", {})
    return {"theme": w.get("theme", ""), "pair_structure": w.get("pair_structure", ""), "palette_family": w.get("palette_family", ""),
            "anchor_kinds": sorted({a.get("kind", "") for a in spec.get("shared_anchors", [])}),
            "a_motif": spec.get("a", {}).get("dna", {}).get("motif_object", ""), "b_motif": spec.get("b", {}).get("dna", {}).get("motif_object", "")}


def remember_rejected(rt: Runtime, project_id: str, specs: Sequence[Mapping[str, Any]], reason: str) -> int:
    """Record the shown plans as rejected with the person's reason (New plan). Returns how many entries the session holds now."""
    data = rt.repo.kv_get(_session_key(project_id)) or {"rejected": [], "rounds": 0}
    why = " ".join(reason.split())[:400]
    data["rejected"] = [*data.get("rejected", []), *({**plan_summary(s), "reason": why} for s in specs)][-AVOID_KEEP:]
    data["rounds"] = int(data.get("rounds", 0)) + 1
    rt.repo.kv_set(_session_key(project_id), data)
    return len(data["rejected"])


def plan_round(rt: Runtime, project_id: str) -> int:
    """How many times "New plan" was used (a new plan round changes the planner request, so the call is never served from the cache)."""
    return int((rt.repo.kv_get(_session_key(project_id)) or {}).get("rounds", 0))


# ---------------------------------------------------------------------------------------------------- planner inputs
def planner_inputs(rt: Runtime, project: Project, *, reference_analysis: Any = None, taste_profile: Any = None,
                   replacement: bool = False, wildcard: bool | None = None, dropped_reasons: Sequence[str] = ()) -> dict[str, Any]:
    """The ``inputs`` of ``prompts.llm.compile_llm("L3.planner", ...)`` for ``project``.

    Every value comes from the brief form, a model answer that passed its Gate A, or the app's memory. The brief text is data (it is
    neutralised inside its tag by the compiler), never an instruction. ``replacement=True`` asks for exactly one replacement spec that keeps
    the dropped spec's wildcard role (``wildcard``) and names why it was dropped."""
    cards = recent_cards(rt, project.id)
    used = recently_used(rt, project.id)
    if rt.effective_settings().planner_structure_lru_hint:
        used = {**used, "least_used_structures": least_used_structures(rt)[:3]}
    inputs: dict[str, Any] = {
        "brief_text": project.brief.strip() or NONE,
        "structure_request": project.structure_request or "auto",
        "must_include": "\n".join(project.must_include) if project.must_include else NONE,
        "combo": project.combo,
        "reference_analysis": _json_or(reference_analysis),
        "taste_profile": _json_or(taste_profile),
        "recent_cards": _json_or(recent_cards_json(cards, RECENT_CARDS)),
        "recently_used": _json_or({k: v for k, v in used.items() if v}),
        "avoid": _json_or(avoid_entries(rt, project.id)) if avoid_entries(rt, project.id) else "",
    }
    if replacement:
        inputs["replacement"] = True
        inputs["wildcard_flag"] = "true" if wildcard else "false"
        inputs["dropped_reasons"] = "; ".join(r.strip() for r in dropped_reasons if r.strip())[:600] or "a hard rule failed"
    return inputs


def inputs_sha(inputs: Mapping[str, Any]) -> str:
    return sha256_of(dict(inputs))


# ---------------------------------------------------------------------------------------------------- reading the plans back
def brief_read_as(specs: Sequence[Mapping[str, Any] | DuoSpec]) -> list[str]:
    """The pair structures the plans actually use, in plan order and without repeats ("Your brief was read as ...")."""
    out: dict[str, None] = {}
    for s in specs:
        w = s.world.pair_structure if isinstance(s, DuoSpec) else (s.get("world") or {}).get("pair_structure")
        if w:
            out.setdefault(str(w), None)
    return list(out)


def _resolves(doc: Any, pointer: str) -> bool:
    from duoskin.engine.deps import resolve

    sentinel = object()
    return resolve(doc, pointer, sentinel) is not sentinel


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).casefold()).split())


def must_include_coverage(lines: Sequence[str], constraints: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Which must-include lines this plan covers: a line is covered when ``brief_constraints`` echoes it and every path it names resolves
    in the spec (FAILURE_MODES PLN-09). ``[{"text", "covered", "paths"}]``, one row per line, in form order."""
    out = []
    for line in lines:
        want = _norm(line)
        hit = next((c for c in constraints if want and (want in _norm(c.get("text", "")) or _norm(c.get("text", "")) in want)), None)
        paths = [str(p) for p in (hit or {}).get("spec_paths", [])]
        covered = bool(hit) and bool(paths) and all(_resolves(spec, p) for p in paths)
        out.append({"text": line, "covered": covered, "paths": paths})
    return out


# ---------------------------------------------------------------------------------------------------- the estimate
def estimate_plan_loop(rt: Runtime, project: Project | None = None, *, n_plans: int = 3) -> dict[str, Any]:
    """The plan-loop estimate beside the start button: L1 (when references exist), L3, L4 x3, L5 x6, one revision round, then 6 concept
    drafts with their checks. A range (bible §20.2), priced from ``prices.json``; the ledger records the real numbers."""
    from duoskin.providers import pricing

    s = rt.effective_settings()
    planner, critic, checker = s.models.planner, s.models.critic, s.models.checker
    n_refs = len([r for r in (project.references if project else []) if r.role == "reference"])
    est = pricing.estimate_claude
    rows: list[dict[str, Any]] = []

    def add(label: str, usd: float, n: int = 1) -> None:
        rows.append({"label": label, "calls": n, "usd": round(usd * n, 4)})

    if n_refs:
        add("Reference analysis (L1)", est(planner, input_tokens=4000 + 1400 * n_refs, output_tokens=6000).usd)
    add("Planner (L3)", est(planner, input_tokens=14000, output_tokens=24000, cached_input_tokens=9000).usd)
    add("Critic (L4)", est(critic, input_tokens=9000, output_tokens=4000, cached_input_tokens=6000).usd, n_plans)
    pairs = n_plans * (n_plans - 1)
    add("Pairwise ranker (L5)", est(critic, input_tokens=11000, output_tokens=3000, cached_input_tokens=6000).usd, pairs)
    add("Reviser (L6), one round", est(planner, input_tokens=9000, output_tokens=2500, cached_input_tokens=6000).usd, 1)
    draft = pricing.estimate_openai_image(quality=(project.settings.concept_quality if project else "low"), n=4, n_input_images=2,
                                          prompt_chars=1900).usd
    add("Concept drafts (I1, front and back)", draft, n_plans * 2)
    judge = est(checker, input_tokens=2600, output_tokens=500).usd
    add("Concept checks (L11, L15)", judge, n_plans * 2 * 3)
    low = sum(r["usd"] for r in rows)
    high = low * 1.8       # a second revision round, a replacement spec and a Reimagine's worth of headroom [ESTIMATE]
    return {"usd_low": round(low, 2), "usd_high": round(high, 2), "breakdown": rows, "estimate": True,
            "note": "An estimate from the price table; the cost bar shows what was actually spent."}
