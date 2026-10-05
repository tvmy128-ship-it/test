"""The part board: plan the parts of an approved spec, run every lane, open Gate 2 and apply its decisions (APP_SPEC §6.5, §9.3, §9.5, §9.7, §9.8).

Public interface::

    plan_parts(spec, project_id="") -> list[Part]            # §6.5: colours, face, hair, one tile per accessory and print, shirt, pants, duo
    ensure_parts(rt, project_id, spec) -> list[Part]         # create the rows (idempotent; labels and routes follow the spec)
    start_parts_job(rt, project_id, *, only=None, nonce="", reason="") -> Job   # the PARTS job (every part, or ``only``)
    reimagine_part(rt, project_id, part_id, *, target=None) -> str               # a new nonce for one part (or one face part)
    apply_invalidation(rt, project_id, report, *, new_spec_id) -> list[str]      # §9.8: regenerate / recompose / recheck what a change touched
    reconcile_approval(rt, project_id, part_id) -> bool      # keep or drop an approval after a recompute (cache hit => unchanged hash)
    refresh_tile(rt, project_id, part_id)                    # push the part's state into the open Gate 2

How the pieces fit
------------------
* A **lane** (``register_lane``) knows one part kind: ``start`` spawns its steps (asset loops, code steps) and ``waits_for`` names the
  parts it needs first (a shirt waits for its prints). The generic ``part.start`` step calls it.
* A lane ends by calling ``mark_ready`` / ``mark_needs_human`` / ``mark_failed``. Those update the part and the open gate's tile and call
  ``on_part_settled``, which starts the parts that were waiting for it and opens **Gate 2** once every part is settled
  (``READY``, ``NEEDS_HUMAN``, ``FAILED`` or already ``APPROVED``).
* **Gate 2** (``PART_BOARD``) has one tile per part. Its applier handles approve / approve all, reimagine, select alternative,
  make-it-myself, flip, change and back to concept. When every tile is APPROVED the BUILD job starts (§2 S10).
* **Invalidation** (§9.8) never edits history: a REGENERATE makes the part STALE and regenerates it; a RECOMPOSE recomputes it for $0 and
  the approval survives when the new ``approval_hash`` equals the old one; a RECHECK re-runs checks only. The partner character's parts are
  only re-checked on the pair-dependent rules and keep their approval (§2 S21).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from duoskin.db.errors import NotFound
from duoskin.engine import deps
from duoskin.engine import registry as eng_registry
from duoskin.engine import scheduler as sched
from duoskin.engine.gates import ApplyContext, ApplyResult, GateError, allowed_actions_for
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict, utcnow
from duoskin.models.gate import Gate, GateAction, GateKind, GateTile, TileState
from duoskin.models.job import Job, JobKind, Step
from duoskin.models.part import DepEffect, Part, PartKind, PartState
from duoskin.models.project import Stage
from duoskin.pipeline import common

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.parts")

CHAR_NAMES = {"a": "A", "b": "B"}
SETTLED = (PartState.READY, PartState.NEEDS_HUMAN, PartState.FAILED, PartState.APPROVED, PartState.BUILDING, PartState.BUILT)
BOARD_PARTS_ORDER = ("colours", "face", "hair", "acc", "print", "shirt", "pants")


# ---------------------------------------------------------------------------------------------------- planning
def _label(character: str, kind: str, spec: dict[str, Any], index: int | None = None, slot: str | None = None) -> str:
    c = CHAR_NAMES[character]
    ch = spec.get(character, {})
    if kind == "acc":
        a = (ch.get("accessories") or [])[index or 0]
        words = " ".join(str(a.get("description", "")).split()[:4])
        return f"{c} · {words} ({a.get('attachment', '').replace('_', ' ')})"
    if kind == "print":
        if slot == "shoes":
            return f"{c} · shoe motif ({ch.get('bottom', {}).get('shoes', {}).get('motif', '')[:30]})"
        prints = ch.get("top" if slot == "top" else "bottom", {}).get("prints") or []
        motif = prints[index or 0].get("motif", "") if index is not None and index < len(prints) else ""
        return f"{c} · {slot} print ({' '.join(motif.split()[:4])})"
    names = {"colours": "Colours & body", "face": "Face", "hair": "Hair", "shirt": "Shirt", "pants": "Pants"}
    return f"{c} · {names.get(kind, kind)}"


def _route_of(kind: str, spec: dict[str, Any], character: str, index: int | None, available: set[str] | None = None) -> str:
    avail = available if available is not None else {"openai", "recraft", "tripo"}
    ch = spec.get(character, {})
    if kind == "face":
        return "R1" if "recraft" in avail else "I3"
    if kind == "hair":
        custom = ch.get("hair", {}).get("kit_style_id") == "hair_custom"
        return ("I4+T1" if "tripo" in avail else "I4+I10") if custom else "kit"
    if kind == "acc":
        build = (ch.get("accessories") or [{}])[index or 0].get("build", "tripo")
        return {"tripo": "I5+T1" if "tripo" in avail else "I5+I10", "sticker_slab": "I6+slab", "code_primitive": "primitive"}.get(build, "I5+T1")
    if kind == "print":
        return "I2"
    return "compositor"


def plan_parts(spec: Any, project_id: str = "", *, available: set[str] | None = None) -> list[Part]:
    """The parts of an approved spec (APP_SPEC §6.5): ``c.colours``, ``c.face``, ``c.hair``, one ``c.acc.<i>`` per accessory, one
    ``c.print.<slot>.<i>`` per print and per shoe motif, ``c.shirt``, ``c.pants`` for both characters, and ``duo``."""
    from duoskin.models.spec import DuoSpec

    sd = spec.model_dump(mode="json") if isinstance(spec, DuoSpec) else dict(spec)
    parts: list[Part] = []

    def add(pid: str, character: str, kind: PartKind, label: str, route: str, rules: list, part_deps: list[str] | None = None) -> None:
        parts.append(Part(id=pid, project_id=project_id, character=character, kind=kind, label=label, deps=rules,   # type: ignore[arg-type]
                          part_deps=part_deps or [], route=route))

    for c in ("a", "b"):
        ch = sd.get(c)
        if not isinstance(ch, dict):
            continue
        add(f"{c}.colours", c, PartKind.COLOURS, _label(c, "colours", sd), "compositor", deps.default_dep_rules("colours", c))
        add(f"{c}.face", c, PartKind.FACE, _label(c, "face", sd), _route_of("face", sd, c, None, available), deps.default_dep_rules("face", c))
        add(f"{c}.hair", c, PartKind.HAIR, _label(c, "hair", sd), _route_of("hair", sd, c, None, available), deps.default_dep_rules("hair", c))
        for i, acc in enumerate(ch.get("accessories") or []):
            head_item = acc.get("category") in ("hat", "hair", "face")        # judged against the approved hair (APP_SPEC §10.8): it goes first
            add(f"{c}.acc.{i}", c, PartKind.ACCESSORY, _label(c, "acc", sd, i), _route_of("acc", sd, c, i, available),
                deps.default_dep_rules("accessory", c, i), [f"{c}.hair"] if head_item else [])
        top_prints = [f"{c}.print.top.{i}" for i in range(len(ch.get("top", {}).get("prints") or []))]
        bottom_prints = [f"{c}.print.bottom.{i}" for i in range(len(ch.get("bottom", {}).get("prints") or []))]
        shoes = ch.get("bottom", {}).get("shoes", {})
        shoe_print = [f"{c}.print.shoes.0"] if shoes.get("motif") not in (None, "", "none") else []
        for pid in top_prints:
            i = int(pid.rsplit(".", 1)[1])
            add(pid, c, PartKind.PRINT, _label(c, "print", sd, i, "top"), "I2", deps.default_dep_rules("print", c, i))
        for pid in bottom_prints:
            i = int(pid.rsplit(".", 1)[1])
            add(pid, c, PartKind.PRINT, _label(c, "print", sd, i, "bottom"), "I2",
                [deps.rule(f"/{c}/bottom/prints/{i}", DepEffect.REGENERATE), deps.rule(f"/{c}/dna/shape_language", DepEffect.REGENERATE),
                 deps.rule("/world/detail_level", DepEffect.REGENERATE)])
        for pid in shoe_print:
            add(pid, c, PartKind.PRINT, _label(c, "print", sd, 0, "shoes"), "I2",
                [deps.rule(f"/{c}/bottom/shoes/motif", DepEffect.REGENERATE), deps.rule(f"/{c}/dna/shape_language", DepEffect.REGENERATE)])
        add(f"{c}.shirt", c, PartKind.SHIRT, _label(c, "shirt", sd), "compositor", deps.default_dep_rules("shirt", c), top_prints)
        add(f"{c}.pants", c, PartKind.PANTS, _label(c, "pants", sd), "compositor", deps.default_dep_rules("pants", c),
            bottom_prints + shoe_print)
    parts.append(Part(id="duo", project_id=project_id, character="duo", kind=PartKind.DUO, label="Duo", route="duo",   # type: ignore[arg-type]
                      deps=[deps.rule("/combo", DepEffect.RECHECK)]))
    return parts


def ensure_parts(rt: Runtime, project_id: str, spec: dict[str, Any]) -> list[Part]:
    """Create the part rows of the project (idempotent). Existing rows keep their state; labels, routes and deps follow the spec."""
    avail = common.available_providers(rt)
    planned = plan_parts(spec, project_id, available=avail)
    existing = {p.id: p for p in rt.repo.list_parts(project_id)}
    out: list[Part] = []
    for p in planned:
        cur = existing.get(p.id)
        if cur is None:
            out.append(rt.repo.save_part(p))
        else:
            def upd(part: Part, p=p) -> None:
                part.label, part.deps, part.part_deps = p.label, p.deps, p.part_deps
                if part.state in (PartState.PLANNED, PartState.STALE, PartState.GENERATING):
                    part.route = p.route

            out.append(rt.repo.mutate_part(project_id, p.id, upd))
    return out


# ---------------------------------------------------------------------------------------------------- lanes
class Lane:
    """One part kind. ``start`` spawns the steps that make the part's board assets; it returns False when it has to wait."""

    kind: PartKind

    def waits_for(self, part: Part, parts: dict[str, Part]) -> list[str]:
        return list(part.part_deps)

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:   # pragma: no cover
        raise NotImplementedError

    def recheck(self, ctx: StepContext, part: Part, spec: dict[str, Any]) -> None:
        """RECHECK: nothing the lane drew changes; the existing board assets stand (the approval survives when its hash is unchanged)."""
        mark_ready(ctx, ctx.step.project_id or "", part.id)

    def recompose(self, ctx: StepContext, part: Part, spec: dict[str, Any]) -> None:
        """RECOMPOSE: recompute from the existing parts for $0 (default: the lane's own ``start``, which is code for the compositor lanes)."""
        self.start(ctx, part, spec, nonce="", target=None)


_LANES: dict[PartKind, Lane] = {}


def register_lane(lane: Lane) -> None:
    _LANES[lane.kind] = lane


def lane_of(kind: PartKind) -> Lane:
    try:
        return _LANES[kind]
    except KeyError:
        raise NotFound("lane", kind.value) from None


# ---------------------------------------------------------------------------------------------------- state and tiles
def _state_to_tile(s: PartState) -> TileState:
    return {PartState.PLANNED: TileState.GENERATING, PartState.GENERATING: TileState.GENERATING, PartState.READY: TileState.READY,
            PartState.NEEDS_HUMAN: TileState.NEEDS_HUMAN, PartState.APPROVED: TileState.APPROVED, PartState.BUILDING: TileState.APPROVED,
            PartState.BUILT: TileState.APPROVED, PartState.STALE: TileState.STALE, PartState.RECHECK: TileState.RECHECK,
            PartState.WAITING_MANUAL: TileState.WAITING_MANUAL, PartState.FAILED: TileState.FAILED}[s]


BADGES = {"no_head_base": "2D preview — no head base", "views_from_gpt": "Views from GPT (lower reliability)",
          "procedural_folds": "procedural folds", "hair_custom_no_kit": "custom hair: no kit", "no_body_base": "standard Block body",
          "no_concept_crop": "no concept crop (placeholder)", "no_style_sheet": "no style sheet (placeholder)",
          "needs_human": "needs your look", "flip_applied": "flipped left/right", "mirrored": "mirrored match: check Left/Right",
          "finalize_drift": "final differs from draft: draft shown", "mock": "DEMO", "kit_hair": "kit hair",
          "hair_registered": "hair registered to the head", "changed_since_concept": "changed since the concept",
          "build_changed": "rebuilt since you last looked", "slab": "sticker slab", "code_primitive": "built by code",
          "mock_face_by_code": "demo: face parts drawn by code", "licence_free_plan": "Tripo free plan: public, not for sale",
          "hair_not_approved": "hair not approved yet", "build_failed": "build needs a look", "waiting_manual": "waiting for your model",
          "kit_mismatch": "kit match is low"}


def facts_key(project_id: str, part_id: str) -> str:
    return f"tile:{project_id}:{part_id}"


def part_cost_usd(rt: Runtime, project_id: str, part_id: str) -> float:
    row = rt.db.conn().execute(
        "SELECT COALESCE(SUM(usd),0) AS s FROM cost_ledger WHERE project_id=? AND json_extract(json,'$.part_id')=? "
        "AND state IN ('committed','orphan')", (project_id, part_id)).fetchone()
    return round(float(row["s"]), 6)


def set_tile_facts(rt: Runtime, project_id: str, part_id: str, **facts: Any) -> dict[str, Any]:
    cur = dict(rt.repo.kv_get(facts_key(project_id, part_id)) or {})
    cur.update(facts)
    rt.repo.kv_set(facts_key(project_id, part_id), cur)
    return cur


def tile_for(rt: Runtime, project_id: str, part: Part, *, version: int = 0) -> GateTile:
    facts = dict(rt.repo.kv_get(facts_key(project_id, part.id)) or {})
    facts["cost_usd"] = part_cost_usd(rt, project_id, part.id)
    facts["route"] = part.route
    facts["part_state"] = part.state.value
    if part.flags:
        facts["flags"] = list(part.flags)
    state = _state_to_tile(part.state)
    can_manual = part.kind in (PartKind.HAIR, PartKind.ACCESSORY) and part.route not in ("kit", "primitive", "I6+slab") and \
        bool(part.board_assets)
    allowed = allowed_actions_for(GateKind.PART_BOARD, part.kind, has_alternatives=bool(part.alternatives), can_manual=can_manual,
                                  mirrored="mirrored" in part.flags)
    if state == TileState.GENERATING:
        allowed = [a for a in allowed if a in (GateAction.REIMAGINE, GateAction.CHANGE, GateAction.APPROVE_ALL, GateAction.BACK_TO_CONCEPT)]
    badges = [BADGES[f] for f in part.flags if f in BADGES]
    if part.character == "b" and False:
        badges.append("")
    return GateTile(tile_id=part.id, part_id=part.id, label=part.label, state=state, assets=dict(part.board_assets),
                    alternatives=[dict(a) for a in part.alternatives], facts=facts, badges=badges, allowed_actions=allowed,   # type: ignore[arg-type]
                    version=version)


def open_board_gate(rt: Runtime, project_id: str) -> Gate | None:
    for g in rt.repo.list_gates(project_id, "open"):
        if g.kind == GateKind.PART_BOARD:
            return g
    return None


def refresh_tile(rt: Runtime, project_id: str, part_id: str) -> None:
    """Push the part's current state into the open Gate 2 (one tile; its version is bumped so stale clients get a 409)."""
    with rt.db.tx():
        gate = open_board_gate(rt, project_id)
        if gate is None:
            return
        part = rt.repo.find_part(project_id, part_id)
        if part is None or part.kind == PartKind.DUO:
            return
        tiles = []
        changed = False
        for t in gate.tiles:
            if t.tile_id == part_id:
                new = tile_for(rt, project_id, part, version=t.version + 1)
                tiles.append(new)
                changed = True
            else:
                tiles.append(t)
        if not changed:
            tiles.append(tile_for(rt, project_id, part, version=0))
        gate = gate.model_copy(update={"tiles": tiles})
        rt.repo.save_gate(gate)
        rt.bus.emit("tile.updated", {"gate_id": gate.id, "tile_id": part_id, "state": _state_to_tile(part.state).value}, project_id)


def set_part_state(rt: Runtime, project_id: str, part_id: str, state: PartState, *, flags_add: list[str] | None = None,
                   flags_remove: list[str] | None = None, **fields: Any) -> Part:
    def apply(p: Part) -> None:
        p.state = state
        for f in flags_remove or []:
            if f in p.flags:
                p.flags.remove(f)
        for f in flags_add or []:
            if f not in p.flags:
                p.flags.append(f)
        for k, v in fields.items():
            setattr(p, k, v)

    part = rt.repo.mutate_part(project_id, part_id, apply)
    rt.bus.emit("part.state", {"project_id": project_id, "part_id": part_id, "state": state.value}, project_id)
    return part


def set_board_assets(rt: Runtime, project_id: str, part_id: str, assets: dict[str, str], *, replace: bool = False,
                     alternatives: list[dict[str, str]] | None = None) -> Part:
    def apply(p: Part) -> None:
        p.board_assets = dict(assets) if replace else {**p.board_assets, **assets}
        if alternatives is not None:
            p.alternatives = [dict(a) for a in alternatives]

    return rt.repo.mutate_part(project_id, part_id, apply)


def _check_facts(part_id: str, results: list[Any]) -> dict[str, Any]:
    """Tile facts from check results. The SOFT warnings live at the top level (``facts["warnings"]``) because the gate service withholds exactly that key
    until the user's first choice (APP_SPEC §9.9); nothing soft stays inside ``facts["checks"]``. Ids are unique per gate: ``<part>:<check>``."""
    summary = common.summarize_checks(results)
    warnings = [{**w, "id": f"{part_id}:{w['id']}"} for w in summary.pop("warnings")]
    return {"checks": summary, "warnings": warnings}


def soft_warning(part_id: str, check_id: str, text: str, *, severity: str = "low", catch_rate: float = 0.0) -> dict[str, Any]:
    """A SOFT warning a lane adds to its tile by hand (a kit match below the threshold, a hair that is not approved yet)."""
    return {"id": f"{part_id}:{check_id}", "text": text[:200], "severity": severity, "catch_rate": catch_rate, "visible": True, "fresh": True}


def mark_ready(ctx_or_rt: Any, project_id: str, part_id: str, *, assets: dict[str, str] | None = None, facts: dict[str, Any] | None = None,
               flags_add: list[str] | None = None, flags_remove: list[str] | None = None, results: list[Any] | None = None,
               warnings: list[dict[str, Any]] | None = None) -> Part:
    """The lane's board assets are final and passed their HARD checks: the tile becomes READY (or the part keeps its approval)."""
    rt: Runtime = ctx_or_rt.rt if hasattr(ctx_or_rt, "rt") else ctx_or_rt
    if assets:
        set_board_assets(rt, project_id, part_id, assets)
    if results is not None:
        facts = {**(facts or {}), **_check_facts(part_id, results)}
        facts["warnings"] = [*facts["warnings"], *(warnings or [])]
    elif warnings:
        facts = {**(facts or {}), "warnings": warnings}
    if facts:
        set_tile_facts(rt, project_id, part_id, **facts)
    part = set_part_state(rt, project_id, part_id, PartState.READY, flags_add=flags_add, flags_remove=(flags_remove or []) + ["waiting_deps"])
    kept = reconcile_approval(rt, project_id, part_id)
    refresh_tile(rt, project_id, part_id)
    on_part_settled(rt, project_id, part_id, ctx=ctx_or_rt if hasattr(ctx_or_rt, "spawn") else None)
    return rt.repo.get_part(project_id, part_id) if kept else part


def mark_needs_human(ctx_or_rt: Any, project_id: str, part_id: str, *, report: str = "", best: str | None = None,
                     assets: dict[str, str] | None = None, results: list[Any] | None = None) -> Part:
    rt: Runtime = ctx_or_rt.rt if hasattr(ctx_or_rt, "rt") else ctx_or_rt
    if assets:
        set_board_assets(rt, project_id, part_id, assets)
    facts: dict[str, Any] = {"report": report, "best": best}
    if results is not None:
        facts.update(_check_facts(part_id, results))
    set_tile_facts(rt, project_id, part_id, **facts)
    part = set_part_state(rt, project_id, part_id, PartState.NEEDS_HUMAN, flags_add=["needs_human"], flags_remove=["waiting_deps"])
    refresh_tile(rt, project_id, part_id)
    on_part_settled(rt, project_id, part_id, ctx=ctx_or_rt if hasattr(ctx_or_rt, "spawn") else None)
    return part


def mark_failed(ctx_or_rt: Any, project_id: str, part_id: str, reason: str) -> Part:
    rt: Runtime = ctx_or_rt.rt if hasattr(ctx_or_rt, "rt") else ctx_or_rt
    set_tile_facts(rt, project_id, part_id, report=reason)
    part = set_part_state(rt, project_id, part_id, PartState.FAILED)
    refresh_tile(rt, project_id, part_id)
    on_part_settled(rt, project_id, part_id, ctx=ctx_or_rt if hasattr(ctx_or_rt, "spawn") else None)
    return part


def mark_generating(rt: Runtime, project_id: str, part_id: str) -> None:
    set_part_state(rt, project_id, part_id, PartState.GENERATING, flags_remove=["needs_human"])
    refresh_tile(rt, project_id, part_id)


# ---------------------------------------------------------------------------------------------------- continuation
def _job_for_new_steps(rt: Runtime, project_id: str) -> str | None:
    """The newest PARTS job of the project that may still take steps (any state except cancelled)."""
    for j in reversed(rt.repo.list_jobs(project_id=project_id, limit=50)):
        if j.kind == JobKind.PARTS and j.state.value != "cancelled":
            return j.id
    return None


def _spawn_start(rt: Runtime, project_id: str, part_id: str, *, ctx: Any = None, nonce: str = "", target: str | None = None,
                 reason: str = "") -> Step | None:
    job_id = ctx.step.job_id if ctx is not None else _job_for_new_steps(rt, project_id)
    if job_id is None:
        job = rt.scheduler.submit_job(JobKind.PARTS, project_id, {"only": [part_id], "reason": reason}, steps=[])
        job_id = job.id
    st = rt.ops.new_step("part.start", job_id=job_id, project_id=project_id, part_id=part_id,
                         params={"part_id": part_id, "nonce": nonce, "target": target, "reason": reason}, nonce=nonce)
    rt.scheduler.spawn(job_id, [st])
    return st


def on_part_settled(rt: Runtime, project_id: str, part_id: str, *, ctx: Any = None) -> None:
    """A part reached READY / NEEDS_HUMAN / FAILED: start the parts that waited for it, then open Gate 2 when everything settled."""
    with rt.db.tx():
        parts = {p.id: p for p in rt.repo.list_parts(project_id)}
        for p in parts.values():
            if "waiting_deps" in p.flags and part_id in p.part_deps and all(
                    parts[d].state in SETTLED for d in p.part_deps if d in parts):
                rt.repo.mutate_part(project_id, p.id, lambda x: x.flags.remove("waiting_deps") if "waiting_deps" in x.flags else None)
                _spawn_start(rt, project_id, p.id, ctx=ctx, reason="dependencies ready")
        maybe_open_gate2(rt, project_id, ctx=ctx)


def maybe_open_gate2(rt: Runtime, project_id: str, *, ctx: Any = None) -> bool:
    """Open Gate 2 (or add the settled parts to the open one) once every character part is settled. Claimed atomically."""
    with rt.db.tx():
        parts = [p for p in rt.repo.list_parts(project_id) if p.kind != PartKind.DUO]
        if not parts or any(p.state not in SETTLED for p in parts):
            return False
        project = rt.repo.get_project(project_id)
        if project.stage in (Stage.BUILDING, Stage.DUO, Stage.GATE3, Stage.EXPORTING, Stage.EXPORTED) and \
                all(p.state in (PartState.APPROVED, PartState.BUILDING, PartState.BUILT) for p in parts):
            return False
        if open_board_gate(rt, project_id) is not None:
            return False
        if rt.repo.kv_get(f"gate2_pending:{project_id}"):
            return False
        rt.repo.kv_set(f"gate2_pending:{project_id}", True)
        job_id = ctx.step.job_id if ctx is not None else (_job_for_new_steps(rt, project_id) or "")
        if not job_id:
            job_id = rt.scheduler.submit_job(JobKind.PARTS, project_id, {"reason": "reopen the part board"}, steps=[]).id
        st = rt.ops.new_step("gate2.open", job_id=job_id, project_id=project_id, params={"project_id": project_id})
        rt.scheduler.spawn(job_id, [st])
        return True


# ---------------------------------------------------------------------------------------------------- approvals
def reconcile_approval(rt: Runtime, project_id: str, part_id: str) -> bool:
    """After a recompute: the approval survives when its ``approval_hash`` is unchanged (a cache hit changes nothing, §9.8)."""
    part = rt.repo.get_part(project_id, part_id)
    if part.approval is None:
        return False
    rec, spec = common.load_spec(rt, project_id)
    project = rt.repo.get_project(project_id)
    chk = deps.check_approval(part, spec, project.pins, deps.collect_facts(rt.repo, part))
    if chk.ok:
        set_part_state(rt, project_id, part_id, PartState.APPROVED)
        return True
    rt.repo.invalidate_approvals(project_id, part_id)
    rt.repo.mutate_part(project_id, part_id, lambda p: (setattr(p, "approval", None), setattr(p, "build_stamp", None),
                                                        setattr(p, "build_assets", {})))
    return False


# ---------------------------------------------------------------------------------------------------- handlers
class StartParams(Strict):
    part_id: str
    nonce: str = ""
    target: str | None = None
    reason: str = ""


class OpenParams(Strict):
    project_id: str


def run_part_start(ctx: StepContext, p: StartParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    lane = lane_of(part.kind)
    parts = {x.id: x for x in rt.repo.list_parts(project_id)}
    waiting = [d for d in lane.waits_for(part, parts) if d in parts and parts[d].state not in SETTLED]
    if waiting:
        set_part_state(rt, project_id, p.part_id, PartState.PLANNED, flags_add=["waiting_deps"])
        refresh_tile(rt, project_id, p.part_id)
        return StepResult(result={"waiting_for": waiting}, message="waiting for " + ", ".join(waiting))
    if p.reason == "recheck":
        lane.recheck(ctx, part, spec)
        return StepResult(result={"lane": part.kind.value, "mode": "recheck"}, message=f"{p.part_id} re-checked")
    if p.reason == "recompose":
        lane.recompose(ctx, part, spec)
        return StepResult(result={"lane": part.kind.value, "mode": "recompose"}, message=f"{p.part_id} recomposed")
    mark_generating(rt, project_id, p.part_id)
    lane.start(ctx, part, spec, nonce=p.nonce, target=p.target)
    return StepResult(result={"lane": part.kind.value}, message=f"{p.part_id} started")


def run_gate2_open(ctx: StepContext, p: OpenParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = p.project_id
    rt.repo.kv_set(f"gate2_pending:{project_id}", False)
    parts = [x for x in rt.repo.list_parts(project_id) if x.kind != PartKind.DUO]
    order = {k: i for i, k in enumerate(BOARD_PARTS_ORDER)}
    parts.sort(key=lambda x: (x.character, order.get({"accessory": "acc", "print": "print"}.get(x.kind.value, x.kind.value), 9), x.id))
    tiles = [tile_for(rt, project_id, x) for x in parts]
    gate = Gate(id="", project_id=project_id, job_id=ctx.step.job_id, kind=GateKind.PART_BOARD, tiles=tiles, opened_at=utcnow())   # type: ignore[arg-type]
    rt.repo.set_project_stage(project_id, Stage.GATE2, bus=rt.bus)
    ctx.open_gate(gate)
    return StepResult(message="Gate 2 is open")


# ---------------------------------------------------------------------------------------------------- jobs
def _parts_job_steps(rt: Runtime, job: Job, project: Any) -> list[Step]:
    """The job factory of ``parts``: ensure the rows, then one ``part.start`` step per part (``params.only`` limits them)."""
    project_id = job.project_id or ""
    _, spec = common.load_spec(rt, project_id)
    parts = ensure_parts(rt, project_id, spec)
    only = set(job.params.get("only") or [p.id for p in parts])
    nonce = str(job.params.get("nonce") or "")
    steps = []
    for p in parts:
        if p.kind == PartKind.DUO or p.id not in only:
            continue
        steps.append(rt.ops.new_step("part.start", job_id=job.id, project_id=project_id, part_id=p.id,
                                     params={"part_id": p.id, "nonce": nonce, "target": None, "reason": str(job.params.get("reason", ""))},
                                     nonce=nonce))
    return steps


def start_parts_job(rt: Runtime, project_id: str, *, only: list[str] | None = None, nonce: str = "", reason: str = "") -> Job:
    """Start the PARTS job (Gate 1 approval calls this after the concept lock): every part, or only ``only``."""
    rt.repo.set_project_stage(project_id, Stage.PARTS, bus=rt.bus)
    return rt.scheduler.submit_job(JobKind.PARTS, project_id, {"only": only, "nonce": nonce, "reason": reason},
                                   spec_id=rt.repo.get_project(project_id).approved_spec_id)


def reimagine_part(rt: Runtime, project_id: str, part_id: str, *, target: str | None = None) -> str:
    """Reimagine = a new nonce (APP_SPEC §8.5): the part's lane runs again with fresh cache keys. Returns the nonce."""
    nonce = common.new_nonce()
    part = rt.repo.get_part(project_id, part_id)
    rt.repo.invalidate_approvals(project_id, part_id)
    rt.repo.mutate_part(project_id, part_id, lambda p: (setattr(p, "approval", None), setattr(p, "build_stamp", None),
                                                        setattr(p, "build_assets", {}), setattr(p, "state", PartState.GENERATING),
                                                        p.ladder.__class__()))
    rt.repo.mutate_part(project_id, part_id, lambda p: setattr(p, "ladder", p.ladder.__class__()))
    refresh_tile(rt, project_id, part_id)
    _spawn_start(rt, project_id, part_id, nonce=nonce, target=target, reason="reimagine")
    return nonce


# ---------------------------------------------------------------------------------------------------- invalidation (§9.8)
def apply_invalidation(rt: Runtime, project_id: str, report: Any, *, new_spec_id: str | None = None) -> list[str]:
    """Act on an ``InvalidationReport`` after the user confirmed a change: REGENERATE makes the part STALE and runs its lane with a new
    nonce; RECOMPOSE recomputes it for $0 (the approval survives when the hash is unchanged); RECHECK re-runs checks only. The partner's
    parts are re-checked on the pair-dependent rules and keep their approval (§2 S21). Returns the part ids touched."""
    touched: list[str] = []
    nonce = common.new_nonce()
    for eff in report.parts:
        try:
            part = rt.repo.get_part(project_id, eff.part_id)
        except NotFound:
            continue
        touched.append(part.id)
        flags = ["changed_since_concept"] if eff.effect != DepEffect.RECHECK else []
        if eff.effect == DepEffect.REGENERATE:
            rt.repo.invalidate_approvals(project_id, part.id)
            rt.repo.mutate_part(project_id, part.id, lambda p: (setattr(p, "approval", None), setattr(p, "build_stamp", None),
                                                                setattr(p, "build_assets", {}), setattr(p, "state", PartState.STALE)))
            set_part_state(rt, project_id, part.id, PartState.STALE, flags_add=flags)
            refresh_tile(rt, project_id, part.id)
            _spawn_start(rt, project_id, part.id, nonce=nonce, target=(eff.targets[0] if len(eff.targets) == 1 else None), reason="regenerate")
        elif eff.effect == DepEffect.RECOMPOSE:
            set_part_state(rt, project_id, part.id, PartState.RECHECK, flags_add=flags)
            refresh_tile(rt, project_id, part.id)
            _spawn_start(rt, project_id, part.id, reason="recompose")
        else:
            set_part_state(rt, project_id, part.id, PartState.RECHECK)
            refresh_tile(rt, project_id, part.id)
            _spawn_start(rt, project_id, part.id, reason="recheck")
    for pid in report.pair_rechecks:
        part = rt.repo.find_part(project_id, pid)
        if part is not None and part.state in (PartState.APPROVED, PartState.READY):
            touched.append(pid)
            rt.repo.mutate_part(project_id, pid, lambda p: p.flags.append("pair_rechecked") if "pair_rechecked" not in p.flags else None)
    if report.duo_stale:
        duo = rt.repo.find_part(project_id, "duo")
        if duo is not None:
            set_part_state(rt, project_id, "duo", PartState.STALE)
    return touched


# ---------------------------------------------------------------------------------------------------- Gate 2 applier
def apply_part_board(ac: ApplyContext) -> ApplyResult | None:
    rt, gate, tile, decision = ac.rt, ac.gate, ac.tile, ac.decision
    a = decision.action
    project_id = ac.project_id
    part = rt.repo.find_part(project_id, tile.part_id or tile.tile_id)
    if part is None:
        raise GateError(f"the part {tile.tile_id} no longer exists", 404, "unknown_tile")
    res = ApplyResult()
    if a in (GateAction.APPROVE, GateAction.APPROVE_ALL):
        _check_approvable(rt, gate, tile, a)
        r = rt.gates._apply_default(ac) or res
        _after_approval(ac, r)
        return r
    if a == GateAction.REIMAGINE:
        target = decision.target if decision.target not in (None, "all") else None
        reimagine_part(rt, project_id, part.id, target=target)
        _sync_tiles(rt, gate, project_id)
        return res
    if a == GateAction.SELECT_ALTERNATIVE:
        select_alternative(rt, project_id, part.id, decision.choice)
        _sync_tiles(rt, gate, project_id)
        return res
    if a == GateAction.MAKE_MANUAL:
        from duoskin.pipeline import manual_mesh

        manual_mesh.start_manual(rt, project_id, part.id, job_id=gate.job_id)
        _sync_tiles(rt, gate, project_id)
        return res
    if a == GateAction.FLIP_MIRRORED:
        from duoskin.pipeline import build

        build.flip_mirrored(rt, project_id, part.id, decision.id)
        _sync_tiles(rt, gate, project_id)
        return res
    if a == GateAction.CHANGE:
        from duoskin.pipeline import partchange

        partchange.begin_change(rt, gate, tile, decision)
        return res
    if a == GateAction.BACK_TO_CONCEPT:
        rt.repo.set_project_stage(project_id, Stage.GATE1, bus=rt.bus)
        try:
            from duoskin.pipeline import concept       # type: ignore[attr-defined]

            reopen = getattr(concept, "reopen_gate1", None)
            if callable(reopen):
                reopen(rt, project_id)
        except ImportError:
            pass
        return ApplyResult(close_gate=True)
    raise GateError(f"'{a.value}' is not handled on the part board", 422, "action_not_allowed")


def _sync_tiles(rt: Runtime, gate: Gate, project_id: str) -> None:
    """Refresh every tile of ``gate`` (the decision's own transaction saves it)."""
    parts = {p.id: p for p in rt.repo.list_parts(project_id)}
    for i, t in enumerate(gate.tiles):
        if t.part_id in parts:
            gate.tiles[i] = tile_for(rt, project_id, parts[t.part_id], version=t.version)


def _check_approvable(rt: Runtime, gate: Gate, tile: GateTile, action: GateAction) -> None:
    from duoskin.checks import policy

    def refuse(t: GateTile) -> None:
        raise GateError(f"{t.label} is not ready to approve ({t.state.value})", 422, "not_ready")

    tiles = [tile] if action == GateAction.APPROVE else []
    for t in tiles:
        if t.state in (TileState.GENERATING, TileState.WAITING_MANUAL, TileState.FAILED, TileState.RECHECK):
            refuse(t)
        if t.state == TileState.NEEDS_HUMAN:
            fails = {f["id"] for f in (t.facts.get("checks", {}).get("hard_failures") or [])}
            blocked = [f for f in fails if policy.meta(f).policy_class in ("roblox", "ip", "stray_text")] if fails else []
            if blocked:
                raise GateError(f"{t.label} still breaks a Roblox, IP or stray-text rule ({', '.join(blocked)}): change it or reimagine it",
                                422, "hard_failure_open")


def _after_approval(ac: ApplyContext, r: ApplyResult) -> None:
    """Every tile approved: the BUILD job starts (§2 S10). ``build.start_per_tile`` starts that tile's build work right away."""
    rt, gate, project_id = ac.rt, ac.gate, ac.project_id
    _sync_tiles(rt, gate, project_id)
    parts = [p for p in rt.repo.list_parts(project_id) if p.kind != PartKind.DUO]
    if parts and all(p.state in (PartState.APPROVED, PartState.BUILT) for p in parts) and any(p.state == PartState.APPROVED for p in parts):
        from duoskin.pipeline import build

        build.start_build(rt, project_id)


def select_alternative(rt: Runtime, project_id: str, part_id: str, choice: str | None) -> None:
    """Swap the board assets for one of the alternatives (another passing face, an alternative print final). The old ones become an
    alternative themselves, and the part needs a new approval."""
    try:
        idx = int(choice or "")
    except ValueError:
        raise GateError("select_alternative needs the alternative's index in 'choice'", 422, "bad_choice") from None
    part = rt.repo.get_part(project_id, part_id)
    if not 0 <= idx < len(part.alternatives):
        raise GateError("there is no such alternative", 422, "bad_choice")
    chosen = part.alternatives[idx]
    rest = [a for i, a in enumerate(part.alternatives) if i != idx]
    rt.repo.invalidate_approvals(project_id, part_id)
    rt.repo.mutate_part(project_id, part_id, lambda p: (setattr(p, "board_assets", {**p.board_assets, **chosen}),
                                                        setattr(p, "alternatives", [dict(p.board_assets)] + rest),
                                                        setattr(p, "approval", None), setattr(p, "state", PartState.READY)))
    rt.repo.mutate_part(project_id, part_id, lambda p: setattr(p, "alternatives", [{k: v for k, v in a.items() if k in chosen} or a
                                                                                   for a in p.alternatives]))
    rt.bus.emit("part.state", {"project_id": project_id, "part_id": part_id, "state": "ready"}, project_id)


# ---------------------------------------------------------------------------------------------------- registration
def register(rt: Runtime | None = None) -> None:
    eng_registry.register_handler("part.start", run_part_start, version=1, pool="cpu", paid=False, Params=StartParams, cacheable=False)
    eng_registry.register_handler("gate2.open", run_gate2_open, version=1, pool="cpu", paid=False, Params=OpenParams, cacheable=False)
    sched.register_job_factory(JobKind.PARTS, _parts_job_steps)
    if rt is not None:
        rt.gates.register_applier(GateKind.PART_BOARD, apply_part_board)


_ = Callable
_ = dataclass
