"""Check results and the gate verdict (FAILURE_MODES §5.1, with the v1.2 `not_applicable` status).

Rules that must never be broken:
* ``ran=False`` means the checker could not run, so ``passed`` must be False (fail closed).
* ``status="not_applicable"`` is different from ``ran=False``: the check was evaluated and found to
  not apply (for example a head-base check when no head base exists). It counts as passed and is
  never treated as a failure, but the reason is always recorded.
* Only ``hard`` and ``assert`` failures block a gate. ``soft`` results are warnings and ranking inputs.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

FixHint = Literal[
    "none", "code_palette_snap", "code_alpha_cleanup", "code_recrop", "code_bleed", "masked_edit",
    "regenerate", "change_technique", "revise_plan", "human",
]
CheckStatus = Literal["passed", "failed", "not_applicable", "not_run"]


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    fm_ids: list[str] = []
    subject_sha: str = ""
    kind: Literal["hard", "soft", "assert"]
    passed: bool
    metric: str = ""
    value: float | None = None
    threshold: str = ""
    evidence: str = ""
    ran: bool = True
    status: CheckStatus = "passed"
    na_reason: str = ""
    fix_hint: FixHint = "none"
    thresholds_version: str = "v1"

    @model_validator(mode="after")
    def _consistent(self) -> "CheckResult":
        if not self.ran:
            # fail closed: a check that did not run can never pass
            self.passed = False
            self.status = "not_run"
        elif self.status == "not_applicable":
            if not self.na_reason:
                raise ValueError("not_applicable requires na_reason")
            self.passed = True
        else:
            self.status = "passed" if self.passed else "failed"
        return self


def not_applicable(check_id: str, kind: Literal["hard", "soft", "assert"], reason: str, **kw) -> CheckResult:
    return CheckResult(check_id=check_id, kind=kind, passed=True, ran=True, status="not_applicable",
                       na_reason=reason, evidence=f"n/a: {reason}", **kw)


def not_run(check_id: str, kind: Literal["hard", "soft", "assert"], why: str, **kw) -> CheckResult:
    return CheckResult(check_id=check_id, kind=kind, passed=False, ran=False, evidence=f"did not run: {why}", **kw)


def gate_verdict(results: list[CheckResult]) -> Literal["pass", "fail"]:
    """Any hard or assert failure blocks; never average. Soft results only warn."""
    bad = [r for r in results if r.kind in ("hard", "assert") and not r.passed]
    return "fail" if bad else "pass"


def warnings_of(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.kind == "soft" and r.ran and not r.passed]
