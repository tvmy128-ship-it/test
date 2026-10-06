"""Fix ladder skeleton (APP_SPEC §8.9, §3.1). **SOFT results never climb.**

``next_action(part, outcome, budget)`` sees only HARD/ASSERT failures. If there are none it returns ``Done`` with the
SOFT results as warnings: no fix is counted, nothing regenerates, no plan revision is proposed, and an approved part is
never touched. Rung 1 (a free, deterministic code fix) is the only thing a soft failure may still trigger, and only on
an unapproved candidate; it never counts toward the 3-fix cap.

Rungs: 1 code auto-fix (free, not counted) -> 2 masked edit (<= 2 per asset) -> 3 regenerate the one asset (n up to 6-8)
-> 4 next technique in the asset's ladder -> 5 revise the plan (duo level, a CHANGE_CONFIRM gate) -> 6 human review.

The cap comes from ``checks.thresholds`` (``ladder.max_fixes_per_part``, default 3) and blocking from ``checks.policy``;
both are imported lazily so the engine works before those modules are complete.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import Field

from duoskin.checks.model import CheckResult
from duoskin.models.common import Strict
from duoskin.models.cost import Budget
from duoskin.models.part import LadderState, Part, PartState

DEFAULT_MAX_FIXES = 3


def max_fixes() -> int:
    try:
        from duoskin.checks import thresholds

        return int(thresholds.get("ladder.max_fixes_per_part"))
    except Exception:  # noqa: BLE001
        return DEFAULT_MAX_FIXES


def is_blocking(result: CheckResult) -> bool:
    """HARD and ASSERT results block; SOFT never does. Uses ``checks.policy`` when it exists."""
    try:
        from duoskin.checks import policy

        return bool(policy.is_blocking(result))
    except Exception:  # noqa: BLE001
        return result.kind in ("hard", "assert")


class LoopOutcome(Strict):
    """What one pass of the asset loop learned about the current best candidate."""

    results: list[CheckResult] = Field(default_factory=list)
    best: str | None = None                    # best asset sha so far
    code_fixable: bool = False                 # a failing HARD result has a code fix_hint
    code_fix_tried: bool = False
    fix_hints: list[str] = Field(default_factory=list)
    batch_pass_rate: float = 1.0               # share of this batch's candidates that passed HARD
    near_miss: bool = False                    # exactly one failing HARD rule, local, fix_hint == masked_edit
    masked_repairs: int = 0
    regenerated_once: bool = False
    n: int = 4
    next_est: float = 0.0                      # USD of the next paid action
    report: str = ""

    @property
    def soft_failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.kind == "soft" and r.ran and not r.passed]

    @classmethod
    def from_results(cls, results: Iterable[CheckResult], **kw: object) -> LoopOutcome:
        """Fill ``code_fixable``, ``fix_hints`` and ``near_miss`` from the results (override with ``kw``)."""
        res = list(results)
        hard = [r for r in res if is_blocking(r) and not r.passed]
        hints = sorted({r.fix_hint for r in hard if r.fix_hint.startswith("code_")})
        near = len(hard) == 1 and hard[0].fix_hint == "masked_edit"
        base: dict[str, object] = {"results": res, "code_fixable": bool(hints), "fix_hints": hints, "near_miss": near}
        base.update(kw)
        return cls.model_validate(base)


class Done(Strict):
    action: Literal["done"] = "done"
    best: str | None = None
    warnings: list[str] = Field(default_factory=list)      # SOFT check ids (shown after the first choice)
    suggested_code_fix: list[str] = Field(default_factory=list)   # free rung-1 fixes allowed on an unapproved candidate


class Human(Strict):
    action: Literal["human"] = "human"
    best: str | None = None
    report: str = ""


class CodeFix(Strict):
    action: Literal["code_fix"] = "code_fix"
    hints: list[str] = Field(default_factory=list)


class MaskedEdit(Strict):
    action: Literal["masked_edit"] = "masked_edit"


class Regenerate(Strict):
    action: Literal["regenerate"] = "regenerate"
    n: int = 4


class NextTechnique(Strict):
    action: Literal["next_technique"] = "next_technique"
    index: int = 0


class RevisePlan(Strict):
    action: Literal["revise_plan"] = "revise_plan"
    reason: str = ""


LadderAction = Done | Human | CodeFix | MaskedEdit | Regenerate | NextTechnique | RevisePlan

_RUNG = {"done": 0, "code_fix": 1, "masked_edit": 2, "regenerate": 3, "next_technique": 4, "revise_plan": 5, "human": 6}


def next_action(part: Part, outcome: LoopOutcome, budget: Budget) -> LadderAction:
    hard_fails = [r for r in outcome.results if is_blocking(r) and not r.passed]
    if not hard_fails:
        soft = [r.check_id for r in outcome.soft_failures]
        hints = sorted({r.fix_hint for r in outcome.soft_failures if r.fix_hint.startswith("code_")})
        unapproved = part.state not in (PartState.APPROVED, PartState.BUILT, PartState.BUILDING)
        return Done(best=outcome.best, warnings=soft, suggested_code_fix=hints if unapproved else [])
    if part.ladder.fixes_used >= max_fixes() or budget.remaining < outcome.next_est:
        return Human(best=part.ladder.best_asset_sha or outcome.best, report=outcome.report)         # rung 6
    if outcome.code_fixable and not outcome.code_fix_tried:
        return CodeFix(hints=outcome.fix_hints)                                                        # rung 1 (free)
    if outcome.batch_pass_rate == 0:
        return NextTechnique(index=part.ladder.technique_index + 1)                                    # 0% pass: skip rung 3
    if outcome.near_miss and outcome.masked_repairs < 2:
        return MaskedEdit()                                                                            # rung 2
    if not outcome.regenerated_once:
        return Regenerate(n=min(8, outcome.n * 2))                                                     # rung 3
    return NextTechnique(index=part.ladder.technique_index + 1)                                        # rung 4


def pick_technique(ladder: list[str], start: int, state: LadderState, *, available_providers: set[str],
                   provider_of: Mapping[str, str | None]) -> int | None:
    """First technique at or after ``start`` whose provider key exists and that has not failed twice (else None -> rung 6)."""
    for i in range(max(start, 0), len(ladder)):
        name = ladder[i]
        prov = provider_of.get(name)
        if prov is not None and prov not in available_providers:
            continue
        if state.failed_rungs.get(name, 0) >= 2:
            continue
        return i
    return None


def record(state: LadderState, action: LadderAction, *, outcome: str = "", best: str | None = None) -> LadderState:
    """Return the ladder state after taking ``action``. Rung 1 is free; rungs 2-5 count toward the fix cap."""
    rung = _RUNG[action.action]
    fixes = state.fixes_used + (1 if action.action in ("masked_edit", "regenerate", "next_technique", "revise_plan") else 0)
    update: dict[str, object] = {"rung": rung, "fixes_used": fixes,
                                 "history": [*state.history, f"rung{rung}:{action.action}:{outcome or 'taken'}"]}
    if isinstance(action, NextTechnique):
        update["technique_index"] = action.index
    if best:
        update["best_asset_sha"] = best
    return state.model_copy(update=update)


def note_failure(state: LadderState, technique: str) -> LadderState:
    failed = dict(state.failed_rungs)
    failed[technique] = failed.get(technique, 0) + 1
    return state.model_copy(update={"failed_rungs": failed})
