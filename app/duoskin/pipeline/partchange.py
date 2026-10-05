"""The change flow at Gate 2 and Gate 3 ("Change..." on a tile; APP_SPEC §9.6, §9.8): L7 -> patch -> lint -> invalidation -> CHANGE_CONFIRM -> apply.

Gate 1 has its own flow (``pipeline.change``: I1e on the chosen concept draft). This module is the same state machine for the part board and the final
pick, where the redo list is the §9.8 table:

1. ``begin_change`` (called by the gate appliers inside the decision's transaction) stores a ``ChangeRequest(status=interpreting)`` and spawns
   ``partchange.interpret``;
2. ``partchange.interpret`` asks L7 (``ChangePlan``). A question (``needs_clarification``) opens a CLARIFY gate and nothing else happens until it is
   answered; otherwise the patch is applied to a **copy** of the spec (``change.apply_patch``: allowed paths, Pydantic), linted (C1: a HARD failure
   rejects the change and tells the person why) and diffed (``deps.affected_parts``: REGENERATE / RECOMPOSE / RECHECK per part, the partner's pair
   rechecks, the estimate);
3. a CHANGE_CONFIRM gate shows the spec diff, the parts to redo with their effect, the estimate and the SOFT lint warnings;
4. **Confirm** stores a new ``SpecRecord`` (version + 1; the approved spec becomes this one) and ``parts.apply_invalidation`` does the rest (only the
   affected tiles change; an approval survives where the new ``approval_hash`` equals the old one); **Cancel** changes nothing.

The CHANGE_CONFIRM and CLARIFY applier of this module is installed *around* any applier already registered for those kinds (Gate 1's), and only acts on
gates whose tile says ``origin`` is ``part_board`` or ``final_pick``.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from duoskin.engine import deps, registry
from duoskin.engine.gates import ApplyContext, ApplyResult, GateError
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict, new_id, sha256_of, utcnow
from duoskin.models.gate import ChangeRequest, Gate, GateAction, GateKind, GateTile, TileState
from duoskin.models.part import DepEffect, PartKind
from duoskin.models.spec_record import SpecRecord
from duoskin.pipeline import common, llmcall, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.partchange")

CODE_KINDS = (PartKind.SHIRT, PartKind.PANTS, PartKind.COLOURS)
ORIGINS = ("part_board", "final_pick")


class InterpretParams(Strict):
    change_id: str
    answer: str = ""


# ---------------------------------------------------------------------------------------------------- start
def begin_change(rt: Runtime, gate: Gate, tile: GateTile, decision: Any) -> ChangeRequest:
    """A "Change..." decision: remember the request and start L7 (runs inside the decision's transaction: rows and steps only)."""
    text = (decision.text or "").strip()
    if not text:
        raise GateError("write what you want to change", 422, "empty_change")
    origin = "final_pick" if gate.kind == GateKind.FINAL_PICK else "part_board"
    ch = ChangeRequest(id=new_id("chg"), project_id=gate.project_id, gate_id=gate.id, tile_id=tile.part_id or tile.tile_id, text=text,
                       mask_sha=decision.mask_sha, status="interpreting")   # type: ignore[arg-type]
    rt.repo.save_change(ch)
    rt.repo.kv_set(f"change:{ch.id}", {"origin": origin, "target": decision.target, "answers": [], "gate_job": gate.job_id})
    step = rt.ops.new_step("partchange.interpret", job_id=gate.job_id, project_id=gate.project_id, params=InterpretParams(change_id=ch.id).model_dump(mode="json"))
    rt.scheduler.spawn(gate.job_id, [step])
    return ch


# ---------------------------------------------------------------------------------------------------- L7
def parts_text(rt: Runtime, project_id: str, spec: dict[str, Any]) -> str:
    from duoskin.pipeline import change

    ps = [p for p in rt.repo.list_parts(project_id) if p.kind != PartKind.DUO]
    return json.dumps(change.parts_for_l7(gate="part_board", spec=spec, parts=ps), sort_keys=True)


def reject(ctx: StepContext, ch: ChangeRequest, reason: str, *, plan: dict[str, Any] | None = None) -> StepResult:
    rt = ctx.rt
    rt.repo.save_change(ch.model_copy(update={"status": "rejected", "plan": plan or ch.plan}))
    rt.repo.kv_set(f"change:{ch.id}:reason", reason)
    rt.bus.emit("toast", {"message": f"That change was not applied: {reason}", "level": "warning", "change_id": ch.id}, ch.project_id)
    return StepResult(result={"status": "rejected", "reason": reason}, message="the change was not applied")


def run_interpret(ctx: StepContext, p: InterpretParams, inputs: list[Any]) -> StepResult:
    from duoskin.models.llm_io import ChangePlan
    from duoskin.pipeline import change, lint

    rt = ctx.rt
    ch = rt.repo.get_change(p.change_id)
    project_id = ch.project_id
    meta = dict(rt.repo.kv_get(f"change:{ch.id}") or {})
    if p.answer:
        meta["answers"] = [*meta.get("answers", []), p.answer]
        rt.repo.kv_set(f"change:{ch.id}", meta)
    rec, spec = common.load_spec(rt, project_id)
    project = rt.repo.get_project(project_id)
    request = " ".join([ch.text, *meta.get("answers", [])])
    ctx.progress(0.2, "reading your change")
    plan: ChangePlan = llmcall.ask_llm(ctx, "L7.change_interpreter", {"spec_json": json.dumps(spec, sort_keys=True), "parts": parts_text(rt, project_id, spec),
                                                                      "clicked_tile": ch.tile_id or "none", "user_change_request": request}, out=ChangePlan)
    plan_json = plan.model_dump(mode="json")
    problems = change.validate_plan(plan, spec)
    if problems:
        return reject(ctx, ch, "; ".join(problems[:2]), plan=plan_json)
    if plan.needs_clarification:
        rt.repo.save_change(ch.model_copy(update={"status": "needs_clarification", "plan": plan_json}))
        tile = GateTile(tile_id=ch.id, part_id=None, label=plan.needs_clarification, state=TileState.READY,
                        facts={"origin": meta.get("origin"), "change_id": ch.id, "question": plan.needs_clarification}, badges=["Question"],
                        allowed_actions=[GateAction.CHANGE, GateAction.CANCEL])   # type: ignore[arg-type]
        gate = Gate(id="", project_id=project_id, job_id=ctx.step.job_id, kind=GateKind.CLARIFY, tiles=[tile], opened_at=utcnow())   # type: ignore[arg-type]
        ctx.open_gate(gate)
        return StepResult(result={"status": "needs_clarification"}, message="a question for you")
    pr = change.apply_patch(spec, plan.patch, kind="change", strict=True, subject_sha=rec.sha256)
    if not pr.ok or pr.spec is None:
        return reject(ctx, ch, pr.reason or "the change does not fit the spec", plan=plan_json)
    bundle = lint.lint_candidates([("change", pr.spec)], lint.lint_context(rt, project), check_set=False)
    if not bundle.clean("change"):
        why = "; ".join(f"{f.message}" for f in bundle.hard_findings("change")[:2]) or "a required rule would break"
        return reject(ctx, ch, why, plan=plan_json)
    existing = [x.id for x in rt.repo.list_parts(project_id)]
    approved = [x.id for x in rt.repo.list_parts(project_id) if x.approval is not None]
    kinds = {x.id: x.kind for x in rt.repo.list_parts(project_id)}
    redo = [{"part_id": r.part_id, "effect": (DepEffect.RECOMPOSE.value if kinds.get(r.part_id) in CODE_KINDS else DepEffect.REGENERATE.value)}
            for r in plan.redo_parts if r.part_id in kinds]
    report = deps.affected_parts(spec, pr.spec, redo, existing_parts=existing, approved_parts=approved, gate3_open=meta.get("origin") == "final_pick",
                                 estimator=lambda e: estimate_effect(rt, e))
    rt.repo.save_change(ch.model_copy(update={"status": "awaiting_confirm", "plan": plan_json, "invalidation": report.model_dump(mode="json"),
                                              "estimate_usd": report.estimate_usd}))
    new_sha = sha256_of(pr.spec)
    rt.repo.kv_set(f"change:{ch.id}:spec", {"spec": pr.spec, "sha": new_sha, "ops": [o.model_dump(mode="json") for o in pr.ops]})
    diff = change.diff_specs(spec, pr.spec)
    facts = {"origin": meta.get("origin"), "change_id": ch.id, "understood_as": plan.understood_as, "diff": diff, "invalidation": report.model_dump(mode="json"),
             "estimate_usd": report.estimate_usd, "risks": list(plan.duo_contract_risks),
             "redo": [{"part_id": e.part_id, "effect": e.effect.value, "reasons": e.reasons[:3]} for e in report.parts], "pair_rechecks": report.pair_rechecks,
             "warnings": [{"id": f"change:{w.check_id}", "text": (w.evidence or w.metric)[:200], "severity": "low", "catch_rate": 0.0, "visible": True, "fresh": True}
                          for w in bundle.warnings("change")[:3]]}
    tile = GateTile(tile_id=ch.id, part_id=None, label=plan.understood_as or "Your change", state=TileState.READY, facts=facts, badges=["Change"],
                    allowed_actions=[GateAction.CONFIRM, GateAction.CANCEL])   # type: ignore[arg-type]
    gate = Gate(id="", project_id=project_id, job_id=ctx.step.job_id, kind=GateKind.CHANGE_CONFIRM, tiles=[tile], opened_at=utcnow())   # type: ignore[arg-type]
    ctx.open_gate(gate)
    return StepResult(result={"status": "awaiting_confirm", "parts": [e.part_id for e in report.parts]}, message="please confirm the change")


def estimate_effect(rt: Runtime, e: Any) -> float:
    """USD to redo one part: nothing for a RECOMPOSE or RECHECK, an asset loop (drafts + judge + final) for a REGENERATE."""
    if e.effect != DepEffect.REGENERATE:
        return 0.0
    from duoskin.providers import pricing

    one = pricing.estimate_openai_image(quality="low", n=4, n_input_images=2, prompt_chars=1500).usd
    fin = pricing.estimate_openai_image(quality="high", n=1, n_input_images=1, prompt_chars=600).usd
    judge = pricing.estimate_claude(rt.effective_settings().models.checker, input_tokens=2600, output_tokens=500).usd * 4
    return round(float(one + fin + judge), 4)


# ---------------------------------------------------------------------------------------------------- the gates' applier
def apply_change_gate(ac: ApplyContext, prev: Any) -> ApplyResult | None:
    rt, tile, d = ac.rt, ac.tile, ac.decision
    origin = tile.facts.get("origin")
    if origin not in ORIGINS:
        return prev(ac) if prev is not None else rt.gates._apply_default(ac)
    ch = rt.repo.get_change(str(tile.facts["change_id"]))
    if ac.gate.kind == GateKind.CLARIFY:
        if d.action == GateAction.CANCEL:
            rt.repo.save_change(ch.model_copy(update={"status": "cancelled"}))
            return ApplyResult(close_gate=True)
        step = rt.ops.new_step("partchange.interpret", job_id=ac.gate.job_id, project_id=ac.project_id,
                               params=InterpretParams(change_id=ch.id, answer=d.text).model_dump(mode="json"))
        rt.scheduler.spawn(ac.gate.job_id, [step])
        return ApplyResult(close_gate=True, spawned_step_ids=[step.id])
    # CHANGE_CONFIRM
    if d.action == GateAction.CANCEL:
        rt.repo.save_change(ch.model_copy(update={"status": "cancelled"}))
        tile.state = TileState.READY
        return ApplyResult(close_gate=True)
    if d.action != GateAction.CONFIRM:
        raise GateError(f"'{d.action.value}' is not handled on a change confirmation", 422, "action_not_allowed")
    return confirm_change(rt, ac, ch, origin)


def confirm_change(rt: Runtime, ac: ApplyContext, ch: ChangeRequest, origin: str) -> ApplyResult:
    project_id = ac.project_id
    stored = rt.repo.kv_get(f"change:{ch.id}:spec")
    if not stored:
        raise GateError("the change is no longer available: ask again", 409, "change_gone")
    project = rt.repo.get_project(project_id)
    old = rt.repo.get_spec(project.approved_spec_id or project.current_spec_id or "")
    ops = [common_op(o) for o in stored["ops"]]
    new = SpecRecord(id=new_id("spc"), project_id=project_id, plan_set_id=old.plan_set_id, plan_index=old.plan_index, parent_spec_id=old.id, version=old.version + 1,
                     created_by="change", patch_from_parent=ops, spec=stored["spec"], palette_source=old.palette_source, status="approved",   # type: ignore[arg-type]
                     sha256=stored["sha"])
    rt.repo.add_spec(new)
    rt.repo.mutate_project(project_id, lambda x: (setattr(x, "approved_spec_id", new.id), setattr(x, "current_spec_id", new.id)))
    report = deps.InvalidationReport.model_validate(ch.invalidation or {})
    touched = parts.apply_invalidation(rt, project_id, report, new_spec_id=new.id)
    rt.repo.save_change(ch.model_copy(update={"status": "applied"}))
    rt.bus.emit("spec.updated", {"project_id": project_id, "spec_id": new.id, "change_id": ch.id, "parts": touched}, project_id)
    if origin == "final_pick":                                  # the duo is stale: Gate 3 closes, the board takes over until the tiles are approved again
        for g in rt.repo.list_gates(project_id, "open"):
            if g.kind == GateKind.FINAL_PICK:
                rt.gates.close_gate(g.id, "superseded")
        rt.repo.set_project_stage(project_id, _stage_after_change(), bus=rt.bus)
    ac.tile.state = TileState.APPROVED
    return ApplyResult(close_gate=True, resulting_spec_id=new.id, change_request_id=ch.id)


def _stage_after_change() -> Any:
    from duoskin.models.project import Stage

    return Stage.PARTS


def common_op(o: dict[str, Any]) -> Any:
    from duoskin.models.llm_io import ChangeOp

    return ChangeOp.model_validate(o)


def wrap_appliers(rt: Runtime) -> None:
    """Install the change applier around whatever is registered for CHANGE_CONFIRM and CLARIFY (Gate 1's), once."""
    for kind in (GateKind.CHANGE_CONFIRM, GateKind.CLARIFY):
        prev = rt.gates._appliers.get(kind.value)
        if getattr(prev, "_partchange", False):
            continue

        def make(prev_=prev):
            def apply(ac: ApplyContext) -> ApplyResult | None:
                return apply_change_gate(ac, prev_)

            apply._partchange = True      # type: ignore[attr-defined]
            return apply

        rt.gates.register_applier(kind, make())


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("partchange.interpret", run_interpret, version=1, pool="api", paid=True, provider="anthropic", Params=InterpretParams,
                              estimate=lambda p: llmcall.estimate_llm_usd("L7.change_interpreter"), cacheable=False)
    if rt is not None:
        wrap_appliers(rt)


_ = common
