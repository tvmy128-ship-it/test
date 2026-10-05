"""Gate lifecycle, tile actions, decisions and the BUDGET gate (APP_SPEC §9.1, §9.5, §9.9, §9.10).

How a gate works
----------------
* A handler (or the engine) opens a gate with ``GateService.open_gate`` (``ctx.open_gate`` calls it). The gate's step
  becomes WAITING_USER and the UI receives ``gate.opened``.
* ``decide`` stores a ``GateDecision`` and applies it. Every decision is
  - **idempotent** by ``client_decision_id`` (a double click returns the stored decision, spawns nothing twice);
  - **optimistically locked** by the tile's ``expected_version`` (``ConflictError`` -> HTTP 409 with the current gate);
  - checked against the tile's ``allowed_actions``.
* **Warnings** (§9.9): SOFT warnings live in ``tile.facts["warnings"]`` (a list of ``{id, text, severity, catch_rate,
  visible}``). ``view`` withholds them until the gate's first choice; the first decision releases at most 2 for the whole
  gate (visible -> highest catch rate -> severity). An approving decision made while warnings were released is stored as
  *provisional* and its follow-up does not start; ``confirm`` ("approve anyway") applies it and logs each overridden
  warning as a ``warning_override`` label; ``withdraw`` ("go back") deletes it.
* **Appliers** do the work of a decision. ``register_applier(kind, fn)``: ``fn(ApplyContext) -> ApplyResult | None`` runs
  inside the decision's transaction (keep it short: write rows, spawn steps, never call providers). The foundation ships
  appliers for the BUDGET gate and a default one for the others (approve a tile, stamp the part's ``approval_hash``,
  decide the gate by the closing rules). Pipeline code registers richer appliers for CONCEPT, PART_BOARD, ...
* **Approvals**: approving a PART_BOARD tile whose part exists stamps the Gate 2 ``approval_hash`` (``engine.deps``). The
  second stamp, ``build_hash``, is written when the part is BUILT and confirmed by the Gate 3 pick (``FINAL_PICK`` + ``PICK``
  confirms every part's build stamp).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from duoskin.db.errors import ConflictError, NotFound
from duoskin.engine import deps
from duoskin.models.common import new_id, utcnow
from duoskin.models.cost import Estimate
from duoskin.models.gate import Gate, GateAction, GateDecision, GateDecisionIn, GateKind, GateTile, TileState
from duoskin.models.job import Step, StepError, StepState
from duoskin.models.part import ApprovalRecord, Part, PartKind, PartState

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.gates")

WITHHELD_FACT_KEYS = ("warnings", "soft_warnings", "soft_results")
MAX_RELEASED_WARNINGS = 2
PROVISIONAL_ACTIONS = (GateAction.APPROVE, GateAction.APPROVE_ALL, GateAction.PICK)
_SEVERITY = {"high": 3, "medium": 2, "low": 1}


class GateError(Exception):
    """A decision the API must refuse with a 4xx (``status`` 400, 404 or 422)."""

    def __init__(self, message: str, status: int = 422, code: str = "bad_decision") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------------------------------- tile actions
def allowed_actions_for(kind: GateKind, part_kind: PartKind | str | None = None, *, has_alternatives: bool = False,
                        can_manual: bool = False, mirrored: bool = False) -> list[GateAction]:
    """The action set of a tile (APP_SPEC §9.5/§9.10). Board-level actions (``approve_all``, ``back_to_concept``,
    ``new_plan``) are included on every tile of their gate and may be sent with any tile id. ``mirrored=True`` (the mesh
    pipeline found that the mirrored match wins, §10.9) adds ``flip_mirrored`` to a hair or accessory tile."""
    A = GateAction
    pk = part_kind.value if isinstance(part_kind, PartKind) else part_kind
    if kind == GateKind.CONCEPT:
        out = [A.APPROVE, A.REIMAGINE, A.CHANGE, A.NEW_PLAN]
        return [*out, A.SELECT_ALTERNATIVE]
    if kind == GateKind.PART_BOARD:
        out = [A.APPROVE]
        if pk != "colours":
            out.append(A.REIMAGINE)
        out.append(A.CHANGE)
        if can_manual and pk in ("hair", "accessory"):
            out.append(A.MAKE_MANUAL)
        if mirrored and pk in ("hair", "accessory"):
            out.append(A.FLIP_MIRRORED)      # "Flip left/right (I checked)": never applied automatically
        if has_alternatives or pk in ("face", "print"):
            out.append(A.SELECT_ALTERNATIVE)
        return out + [A.APPROVE_ALL, A.BACK_TO_CONCEPT]
    if kind == GateKind.FINAL_PICK:
        return [A.PICK, A.CHANGE, A.EXPORT]
    if kind == GateKind.BUDGET:
        return [A.CONTINUE, A.RAISE_CAP, A.STOP]
    if kind == GateKind.MANUAL_IMPORT:
        return [A.CANCEL]
    if kind == GateKind.CHANGE_CONFIRM:
        return [A.CONFIRM, A.CANCEL]
    if kind == GateKind.CLARIFY:
        return [A.CHANGE, A.CANCEL]
    if kind == GateKind.HUMAN_REVIEW:
        return [A.APPROVE, A.CHANGE, A.BACK_TO_CONCEPT]
    return [A.APPROVE, A.REIMAGINE]    # SETUP_APPROVAL


# Which decisions close a gate by themselves. PART_BOARD closes when every tile is APPROVED (or on back_to_concept).
CLOSING_ACTIONS: dict[GateKind, set[GateAction]] = {
    GateKind.CONCEPT: {GateAction.APPROVE, GateAction.NEW_PLAN},
    GateKind.PART_BOARD: {GateAction.BACK_TO_CONCEPT},
    GateKind.FINAL_PICK: {GateAction.EXPORT},
    GateKind.BUDGET: {GateAction.CONTINUE, GateAction.RAISE_CAP, GateAction.STOP},
    GateKind.MANUAL_IMPORT: {GateAction.CANCEL},
    GateKind.CHANGE_CONFIRM: {GateAction.CONFIRM, GateAction.CANCEL},
    GateKind.CLARIFY: {GateAction.CHANGE, GateAction.CANCEL},
    GateKind.HUMAN_REVIEW: {GateAction.APPROVE, GateAction.CHANGE, GateAction.BACK_TO_CONCEPT},
    GateKind.SETUP_APPROVAL: {GateAction.APPROVE},
}


# ---------------------------------------------------------------------------------------------------- appliers
@dataclass
class ApplyContext:
    rt: Runtime
    gate: Gate                      # mutable copy: tile changes made here are saved by the service
    tile: GateTile                  # the decided tile (also inside ``gate.tiles``)
    decision: GateDecision
    project_id: str


@dataclass
class ApplyResult:
    close_gate: bool | None = None  # None: use the closing rules above
    spawned_step_ids: list[str] = field(default_factory=list)
    resulting_spec_id: str | None = None
    change_request_id: str | None = None
    approval: ApprovalRecord | None = None
    step: Literal["auto", "keep", "succeed"] = "auto"   # what to do with the gate's step when the gate closes
    step_result: dict[str, Any] = field(default_factory=dict)


Applier = Callable[[ApplyContext], "ApplyResult | None"]


@dataclass
class DecisionOutcome:
    decision: GateDecision
    released_warnings: list[dict[str, Any]]
    replay: bool = False


def _rank_warning(w: dict[str, Any]) -> tuple[float, float, str]:
    sev = w.get("severity", 1)
    sev_n = _SEVERITY.get(sev, 1) if isinstance(sev, str) else float(sev or 1)
    return (-float(w.get("catch_rate", 0.0) or 0.0), -sev_n, str(w.get("id", "")))


class GateService:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.repo = rt.repo
        self.db = rt.db
        self.bus = rt.bus
        self._appliers: dict[str, Applier] = {GateKind.BUDGET.value: self._apply_budget}

    def register_applier(self, kind: GateKind | str, fn: Applier) -> None:
        self._appliers[GateKind(kind).value] = fn

    # ------------------------------------------------------------------------------------------------ open
    def open_gate(self, gate: Gate, *, step: Step | None = None) -> Gate:
        """Insert ``gate`` and park ``step`` (if any) in WAITING_USER. Fills id, project, job, spend and time."""
        updates: dict[str, Any] = {}
        if not gate.id:
            updates["id"] = new_id("gat")
        if step is not None:
            updates["step_id"] = step.id
            if not gate.job_id:
                updates["job_id"] = step.job_id
            if not gate.project_id:
                updates["project_id"] = step.project_id or ""
        gate = gate.model_copy(update=updates) if updates else gate
        gate = gate.model_copy(update={"spent_usd_at_open": self.rt.budget.spent(gate.project_id or None),
                                       "opened_at": utcnow()})
        with self.db.tx():
            self.repo.insert_gate(gate)
            if step is not None:
                moved = self.rt.ops.transition(
                    step.id, StepState.WAITING_USER, expect=StepState.RUNNING, attempt=step.attempt,
                    update=lambda s: (setattr(s, "gate_id", gate.id), setattr(s, "message", f"waiting for you ({gate.kind.value})")))
                if moved is None:
                    from duoskin.engine.errors import Cancelled

                    raise Cancelled(step.id)
            self._emit_opened(gate)
        return gate

    def _emit_opened(self, gate: Gate) -> None:
        self.bus.emit("gate.opened", {"gate_id": gate.id, "project_id": gate.project_id, "kind": gate.kind.value,
                                      "tiles": len(gate.tiles), "step_id": gate.step_id}, gate.project_id or None)

    def reannounce_open_gates(self) -> int:
        """Recovery: tell the UI about every gate that is still open."""
        gates = self.repo.list_gates(state="open")
        for g in gates:
            self._emit_opened(g)
        return len(gates)

    # ------------------------------------------------------------------------------------------------ view
    def _shown_ids(self, gate: Gate) -> set[str]:
        ids: set[str] = set()
        for d in self.repo.list_decisions(gate.id):
            ids.update(d.warnings_shown)
        return ids

    def view(self, gate: Gate | str) -> Gate:
        """The gate as the API shows it: SOFT warnings are withheld until the first choice, then only the (<= 2) released
        ones are visible (§9.9)."""
        if isinstance(gate, str):
            gate = self.repo.get_gate(gate)
        shown = self._shown_ids(gate) if gate.first_choice_at is not None else set()
        tiles: list[GateTile] = []
        for t in gate.tiles:
            facts = dict(t.facts)
            for key in WITHHELD_FACT_KEYS:
                if key not in facts:
                    continue
                if key == "warnings" and shown:
                    facts[key] = [w for w in facts[key] if isinstance(w, dict) and w.get("id") in shown]
                else:
                    facts.pop(key)
            tiles.append(t.model_copy(update={"facts": facts}))
        return gate.model_copy(update={"tiles": tiles})

    def _pick_warnings(self, gate: Gate, only_tile: str | None = None, *, fresh_only: bool = False,
                       exclude: set[str] | None = None) -> list[dict[str, Any]]:
        exclude = exclude or set()
        pool: list[dict[str, Any]] = []
        for t in gate.tiles:
            if only_tile is not None and t.tile_id != only_tile:
                continue
            for w in t.facts.get("warnings", []) or []:
                if not isinstance(w, dict) or "id" not in w or w["id"] in exclude:
                    continue
                if w.get("visible", True) is False:         # auto-hidden by calibration (§3.1)
                    continue
                if fresh_only and not w.get("fresh"):
                    continue
                pool.append({**w, "tile_id": t.tile_id})
        pool.sort(key=_rank_warning)
        return pool[:MAX_RELEASED_WARNINGS]

    # ------------------------------------------------------------------------------------------------ decide
    def decide(self, gate_id: str, body: GateDecisionIn) -> DecisionOutcome:
        existing = self.repo.find_decision_by_client_id(body.client_decision_id)
        if existing is not None:
            return self._replay(existing)
        with self.db.tx():
            existing = self.repo.find_decision_by_client_id(body.client_decision_id)
            if existing is not None:
                return self._replay(existing)
            gate = self.repo.find_gate(gate_id)
            if gate is None:
                raise NotFound("gate", gate_id)
            if gate.state != "open":
                raise ConflictError("this gate is no longer open", self.view(gate), code="gate_closed")
            tile = next((t for t in gate.tiles if t.tile_id == body.tile_id), None)
            if tile is None:
                raise GateError(f"unknown tile '{body.tile_id}'", 404, "unknown_tile")
            if body.expected_version != tile.version:
                raise ConflictError("the tile changed since you loaded it", self.view(gate), code="version_conflict")
            if body.action not in tile.allowed_actions:
                raise GateError(f"'{body.action.value}' is not allowed on this tile", 422, "action_not_allowed")
            if any(d.provisional and d.tile_id == tile.tile_id for d in self.repo.list_decisions(gate.id)):
                raise ConflictError("finish or withdraw the earlier choice on this tile first", self.view(gate),
                                    code="provisional_pending")
            now = utcnow()
            released: list[dict[str, Any]] = []
            if gate.first_choice_at is None:
                gate = gate.model_copy(update={"first_choice_at": now})
                released = self._pick_warnings(gate)
            else:
                released = self._pick_warnings(gate, only_tile=tile.tile_id, fresh_only=True, exclude=self._shown_ids(gate))
            provisional = body.action in PROVISIONAL_ACTIONS and bool(released)
            decision = GateDecision(
                id=new_id("dec"), gate_id=gate.id, tile_id=tile.tile_id, action=body.action, text=body.text,
                mask_sha=body.mask_sha, choice=body.choice, decided_at=now, warnings_shown=[w["id"] for w in released],
                provisional=provisional, client_decision_id=body.client_decision_id)
            self.repo.insert_decision(decision)
            if provisional:
                self.repo.save_gate(gate)
                self.bus.emit("warning.released", {"gate_id": gate.id, "tile_id": tile.tile_id, "decision_id": decision.id,
                                                   "warnings": [{k: v for k, v in w.items()} for w in released]},
                              gate.project_id or None)
                return DecisionOutcome(decision, released)
            decision = self._apply(gate, decision)
        return DecisionOutcome(decision, released)

    def _replay(self, d: GateDecision) -> DecisionOutcome:
        gate = self.repo.find_gate(d.gate_id)
        released: list[dict[str, Any]] = []
        if gate is not None and d.warnings_shown:
            ids = set(d.warnings_shown)
            for t in gate.tiles:
                released += [{**w, "tile_id": t.tile_id} for w in t.facts.get("warnings", []) or []
                             if isinstance(w, dict) and w.get("id") in ids]
        return DecisionOutcome(d, released, replay=True)

    def confirm(self, gate_id: str, decision_id: str, override_warnings: list[str]) -> GateDecision:
        """"Approve anyway": make a provisional decision final, log the overridden warnings, start the follow-up."""
        with self.db.tx():
            gate = self.repo.get_gate(gate_id)
            d = self.repo.find_decision(decision_id)
            if d is None or d.gate_id != gate_id:
                raise NotFound("decision", decision_id)
            if not d.provisional:
                raise ConflictError("this decision is already final", None, code="not_provisional")
            if gate.state != "open":
                raise ConflictError("this gate is no longer open", self.view(gate), code="gate_closed")
            unknown = [w for w in override_warnings if w not in d.warnings_shown]
            if unknown:
                raise GateError(f"warnings not shown for this decision: {', '.join(unknown)}")
            tile = next((t for t in gate.tiles if t.tile_id == d.tile_id), None)
            if tile is None:
                raise GateError("the tile no longer exists", 404, "unknown_tile")
            d = d.model_copy(update={"provisional": False, "warnings_overridden": list(override_warnings)})
            for wid in override_warnings:
                self.repo.insert_label("warning_override", "gate", [gate.id, d.tile_id, wid], True)
            d = self._apply(gate, d)
        return d

    def withdraw(self, gate_id: str, decision_id: str) -> None:
        """"Go back": delete a provisional decision. The tile is unchanged (still READY)."""
        with self.db.tx():
            d = self.repo.find_decision(decision_id)
            if d is None or d.gate_id != gate_id:
                raise NotFound("decision", decision_id)
            if not d.provisional:
                raise ConflictError("only a provisional decision can be withdrawn", None, code="not_provisional")
            self.repo.delete_decision(decision_id)

    # ------------------------------------------------------------------------------------------------ apply
    def _apply(self, gate: Gate, decision: GateDecision) -> GateDecision:
        project_id = gate.project_id
        tile = next(t for t in gate.tiles if t.tile_id == decision.tile_id)
        before = {t.tile_id: t.model_dump_json() for t in gate.tiles}
        applier = self._appliers.get(gate.kind.value, self._apply_default)
        ac = ApplyContext(self.rt, gate, tile, decision, project_id)
        result = applier(ac) or ApplyResult()
        gate = ac.gate
        changed: list[GateTile] = []
        new_tiles: list[GateTile] = []
        for t in gate.tiles:
            if t.model_dump_json() != before.get(t.tile_id) or t.tile_id == decision.tile_id:
                bumped = t.model_copy(update={"version": t.version + 1})
                new_tiles.append(bumped)
                changed.append(bumped)
            else:
                new_tiles.append(t)
        gate = gate.model_copy(update={"tiles": new_tiles})
        decision = decision.model_copy(update={
            "spawned_step_ids": result.spawned_step_ids, "resulting_spec_id": result.resulting_spec_id,
            "change_request_id": result.change_request_id, "approval": result.approval or decision.approval,
            "provisional": False})
        close = result.close_gate
        if close is None:
            close = (decision.action in CLOSING_ACTIONS.get(gate.kind, set())
                     or (gate.kind == GateKind.PART_BOARD and all(t.state == TileState.APPROVED for t in gate.tiles)))
        if close:
            gate = gate.model_copy(update={"state": "decided", "decided_at": utcnow()})
        self.repo.save_decision(decision)
        self.repo.save_gate(gate)
        for t in changed:
            self.bus.emit("tile.updated", {"gate_id": gate.id, "tile_id": t.tile_id, "state": t.state.value,
                                           "version": t.version}, gate.project_id or None)
        self.bus.emit("gate.updated", {"gate_id": gate.id, "state": gate.state, "kind": gate.kind.value,
                                       "decision_id": decision.id}, gate.project_id or None)
        if close and gate.step_id and result.step in ("auto", "succeed"):
            self.complete_step(gate.step_id, result={"gate_id": gate.id, "decision_id": decision.id,
                                                     "action": decision.action.value, **result.step_result})
        return decision

    def close_gate(self, gate_id: str, state: Literal["decided", "superseded"] = "decided") -> Gate:
        """Close a gate from outside a decision (an import finished a MANUAL_IMPORT gate, a new gate replaced it)."""
        with self.db.tx():
            gate = self.repo.get_gate(gate_id)
            if gate.state != "open":
                return gate
            gate = gate.model_copy(update={"state": state, "decided_at": utcnow()})
            self.repo.save_gate(gate)
            self.bus.emit("gate.updated", {"gate_id": gate.id, "state": state, "kind": gate.kind.value},
                          gate.project_id or None)
        return gate

    def complete_step(self, step_id: str, *, result: dict[str, Any] | None = None, outputs: list[str] | None = None) -> Step | None:
        """WAITING_USER -> SUCCEEDED (the decision resolved what the step was waiting for)."""
        def done(s: Step) -> None:
            s.progress = 1.0
            s.message = "decided"
            s.result = {**s.result, **(result or {})}
            if outputs is not None:
                s.outputs = outputs

        return self.rt.ops.transition(step_id, StepState.SUCCEEDED, expect=StepState.WAITING_USER, update=done)

    # ------------------------------------------------------------------------------------------------ default applier
    def _stamp(self, ac: ApplyContext, part_id: str, decision_id: str) -> ApprovalRecord | None:
        part = self.repo.find_part(ac.project_id, part_id)
        if part is None:
            return None
        project = self.repo.find_project(ac.project_id)
        spec_id = (project.approved_spec_id or project.current_spec_id) if project else None
        spec_doc: dict[str, Any] = {}
        spec_version = 0
        if spec_id:
            try:
                rec = self.repo.get_spec(spec_id)
                spec_doc, spec_version = rec.spec, rec.version
            except NotFound:
                spec_id = None
        facts = deps.collect_facts(self.repo, part)
        approval = deps.make_approval(part, spec_doc, spec_id=spec_id or "", spec_version=spec_version,
                                      pins=project.pins if project else None, decision_id=decision_id, facts=facts)

        def apply(p: Part) -> None:
            p.approval = approval
            p.state = PartState.APPROVED

        self.repo.mutate_part(ac.project_id, part_id, apply)
        self.repo.upsert_approval(ac.project_id, approval, valid=True)
        self.bus.emit("part.state", {"project_id": ac.project_id, "part_id": part_id, "state": "approved"}, ac.project_id)
        return approval

    def _apply_default(self, ac: ApplyContext) -> ApplyResult | None:
        a, gate, tile = ac.decision.action, ac.gate, ac.tile
        res = ApplyResult()
        if a == GateAction.APPROVE:
            tile.state = TileState.APPROVED
            if gate.kind == GateKind.PART_BOARD and tile.part_id:
                res.approval = self._stamp(ac, tile.part_id, ac.decision.id)
        elif a == GateAction.APPROVE_ALL:
            for t in gate.tiles:
                if t.state == TileState.READY and not t.facts.get("hard_failures"):
                    t.state = TileState.APPROVED
                    if gate.kind == GateKind.PART_BOARD and t.part_id:
                        self._stamp(ac, t.part_id, ac.decision.id)
        elif a == GateAction.PICK and gate.kind == GateKind.FINAL_PICK:
            tile.state = TileState.APPROVED
            for part in self.repo.list_parts(ac.project_id):
                if part.build_stamp is not None and part.character in ("a", "b"):
                    deps.confirm_build(self.repo, ac.project_id, part.id, ac.decision.id)
        return res

    # ------------------------------------------------------------------------------------------------ BUDGET gate
    def open_budget_gate(self, step: Step, facts: dict[str, Any]) -> Gate:
        """The BUDGET gate for a paid step whose estimate passes the cap or the ask threshold (§8.8)."""
        tile = GateTile(tile_id=step.id, part_id=step.part_id, label=f"{step.kind} (about ${step.cost_estimate_usd:.2f})",
                        state=TileState.READY, facts={"step_id": step.id, "step_kind": step.kind, **facts},
                        badges=["Budget"], allowed_actions=allowed_actions_for(GateKind.BUDGET))
        gate = Gate(id="", project_id=step.project_id or "", job_id=step.job_id, kind=GateKind.BUDGET, tiles=[tile],
                    opened_at=utcnow())
        return self.open_gate(gate, step=step)

    def _apply_budget(self, ac: ApplyContext) -> ApplyResult:
        a, tile = ac.decision.action, ac.tile
        step_id = tile.tile_id
        ops = self.rt.ops
        if a == GateAction.STOP:
            def fail(s: Step) -> None:
                s.error = StepError(kind="other", code="budget", message="budget", retryable=False, billed="no",
                                    user_hint="Stopped at the budget gate.")
                s.message = "budget"
                s.gate_id = None

            ops.transition(step_id, StepState.FAILED, expect=StepState.WAITING_USER, update=fail)
            tile.state = TileState.FAILED
            return ApplyResult(close_gate=True, step="keep")
        if a == GateAction.RAISE_CAP:
            try:
                new_cap = float(ac.decision.choice or "")
            except ValueError:
                raise GateError("raise_cap needs the new cap in 'choice'") from None
            if new_cap <= 0:
                raise GateError("the new cap must be above zero")
            if ac.project_id:
                self.repo.mutate_project(ac.project_id, lambda p: setattr(p.settings, "budget_usd", max(p.settings.budget_usd, new_cap)))
                self.bus.emit("toast", {"message": f"Cap raised to ${new_cap:.2f}", "level": "info"}, ac.project_id)
        # CONTINUE and RAISE_CAP: the user accepted this spend; the step runs without asking again
        ops.transition(step_id, StepState.READY, expect=StepState.WAITING_USER,
                       update=lambda s: (setattr(s, "budget_ok", True), setattr(s, "gate_id", None),
                                         setattr(s, "message", "approved by you")))
        tile.state = TileState.APPROVED
        return ApplyResult(close_gate=True, step="keep")


def estimate_of(usd: float, provider: str = "mock", operation: str = "step") -> Estimate:
    return Estimate(usd=usd, provider=provider, operation=operation)   # type: ignore[arg-type]
