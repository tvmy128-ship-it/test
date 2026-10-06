"""Helpers of the plan-rule tests: mutate a fixture dict, lint it, look at one metric. Test code only."""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import specfix

from duoskin.checks import plan_rules as P
from duoskin.models.spec import DuoSpec

SPEC_NAMES = specfix.SPEC_NAMES


def load(name: str = "spec_complement_gb") -> dict[str, Any]:
    return copy.deepcopy(specfix.load_dict(name))


def spec_of(d: dict[str, Any]) -> DuoSpec:
    """Parse without the spec rules, so the linter (not the parser) reports the problem."""
    return DuoSpec.model_validate(d, context={"skip_rules": True})


def lint(d: dict[str, Any] | DuoSpec, **ctx: Any) -> P.LintReport:
    spec = d if isinstance(d, DuoSpec) else spec_of(d)
    return P.lint_spec(spec, P.PlanLintCtx(**ctx) if ctx else None)


def mutate(name: str, fn: Callable[[dict[str, Any]], None], **ctx: Any) -> P.LintReport:
    d = load(name)
    fn(d)
    return lint(d, **ctx)


def result(rep: P.LintReport, metric: str):
    got = rep.by_metric(metric)
    assert got, f"no result for metric {metric}: {[r.metric for r in rep.results]}"
    assert len(got) == 1, f"{metric} appears {len(got)} times"
    return got[0]


def failed(rep: P.LintReport) -> list[str]:
    return [r.metric for r in rep.results if not r.passed]


def hard_failed(rep: P.LintReport) -> list[str]:
    return [r.metric for r in rep.blocking]


def palette_id(d: dict[str, Any], role: str) -> str:
    return next(c["id"] for c in d["palette"] if c["role"] == role)


def set_hex(d: dict[str, Any], role: str, hex_: str) -> None:
    for c in d["palette"]:
        if c["role"] == role:
            c["hex"] = hex_
