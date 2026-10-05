"""C1, the plan linter, as the plan loop uses it (APP_SPEC §10.2, bible §9.4).

``checks/plan_rules.py`` holds every rule as a pure function. This module is the glue the pipeline needs around it:

* ``lint_context(rt, project)`` builds the ``PlanLintCtx`` (the kit inventory of this run, the brief form's structure request, the
  must-include lines, the last DNA cards as a SOFT hint);
* ``lint_candidates(candidates, ctx, ...)`` lints a set of candidate specs and the plan-set rules together and returns a
  ``LintBundle``: one ``LintReport`` per spec, the set report, which specs are HARD-clean, and the findings per spec **re-based to the
  spec's own JSON pointers**, ready for the reviser (L6);
* ``reviser_findings`` / ``findings_text`` turn HARD findings (and the critic's high-severity fixes) into the numbered ``<findings>`` list;
* ``wildcard_targets`` decides, in code, which spec has to change when the wildcard count is wrong (a mechanical choice, so two
  revision calls can never both flip their own spec);
* ``gate1_set_check`` is CHK-G1-07: Gate 1 shows 3 plans with exactly one wildcard label, or the remaining plans with the visible
  notice "wildcard could not be built".

HARD rules are Roblox rules, buildability, IP and the registry; taste, colour distance, restraint and novelty only warn (APP_SPEC §3.1).
There are no numeric literals here other than 0, 1, -1 and 2: every number comes from ``checks/thresholds.py`` or ``data/*.json``.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from duoskin.checks import plan_rules as PR
from duoskin.checks import policy, runner
from duoskin.checks.model import CheckResult
from duoskin.checks.plan_rules import Finding, LintReport, PlanLintCtx
from duoskin.models.common import sha256_of
from duoskin.models.spec import DuoSpec
from duoskin.prompts.catalog import data_json

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime
    from duoskin.models.project import Project

SET_PATH = re.compile(r"^/specs/(\d+)(/.*)?$")
ENVELOPE_PREFIXES = ("/brief_constraints", "/how_they_differ")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
WILDCARD_NOTICE = "wildcard could not be built"


# ---------------------------------------------------------------------------------------------------- context
def parse_loose(spec: Mapping[str, Any] | DuoSpec) -> DuoSpec:
    """A ``DuoSpec`` parsed without ``spec_rules`` (the linter reports every problem at once; a strict parse would stop at the first)."""
    if isinstance(spec, DuoSpec):
        return spec
    return DuoSpec.model_validate(dict(spec), context={"skip_rules": True})


def lint_context(rt: Runtime | None, project: Project | None = None, *, recent_cards: Sequence[Any] = (), inventory: Any = None,
                 structure_request: str | None = None, must_include: Sequence[str] | None = None) -> PlanLintCtx:
    """The facts the linter reads. Without a runtime (tests) the current kit inventory and the given values are used."""
    inv = inventory
    sparkle = True
    if rt is not None:
        if inv is None:
            from duoskin.pipeline import kits

            inv = kits.load_context(rt).inventory
        sparkle = bool(rt.effective_settings().checks.sparkle_star_allowed)
    req = structure_request if structure_request is not None else (project.structure_request if project is not None else "auto")
    lines = tuple(must_include) if must_include is not None else (tuple(project.must_include) if project is not None else ())
    return PlanLintCtx(inventory=inv, structure_request=(req or "auto"), must_include=lines, recent_cards=tuple(recent_cards),
                       sparkle_star_allowed=sparkle)


# ---------------------------------------------------------------------------------------------------- the bundle
@dataclass
class LintBundle:
    """The lint of a set of candidate specs. ``order`` is the spec ids in plan order; indexes in set findings (``/specs/1/...``) follow it."""

    order: list[str]
    per_spec: dict[str, LintReport]
    set_report: LintReport
    spec_set_findings: dict[str, list[Finding]] = field(default_factory=dict)   # HARD set findings re-based onto one spec
    envelope_findings: list[Finding] = field(default_factory=list)              # HARD findings about brief_constraints / how_they_differ
    global_findings: list[Finding] = field(default_factory=list)                # HARD findings no reviser call can fix (set size)

    # ---- per spec
    def results(self, spec_id: str) -> list[CheckResult]:
        """Every check result of one spec: its own rules plus the set results (they are stored on every spec, labelled by metric)."""
        return [*self.per_spec[spec_id].results, *self.set_report.results]

    def hard_findings(self, spec_id: str) -> list[Finding]:
        own = [f for f in self.per_spec[spec_id].findings if f.hard]
        return [*own, *self.spec_set_findings.get(spec_id, [])]

    def warnings(self, spec_id: str) -> list[CheckResult]:
        return [r for r in self.per_spec[spec_id].results if r.kind == "soft" and r.ran and not r.passed]

    def clean(self, spec_id: str) -> bool:
        """No HARD or ASSERT rule failed or did not run for this spec (SOFT results never decide), and no set rule targets it."""
        rep = self.per_spec[spec_id]
        return rep.passed and not self.spec_set_findings.get(spec_id)

    def unclean(self) -> list[str]:
        return [s for s in self.order if not self.clean(s)]

    # ---- the set
    @property
    def set_clean(self) -> bool:
        return not (self.envelope_findings or self.global_findings or any(self.spec_set_findings.values()))

    def set_warnings(self) -> list[CheckResult]:
        return [r for r in self.set_report.results if r.kind == "soft" and r.ran and not r.passed]

    def relaxed(self, rules: Sequence[str] = ("wildcard_count",)) -> LintBundle:
        """The same bundle without the set findings of ``rules`` and without the brief_constraints envelope findings: used after a replacement,
        when a missing wildcard is settled by the visible notice and the envelope was already revised."""
        keep = {sid: [f for f in fs if f.rule not in rules] for sid, fs in self.spec_set_findings.items()}
        return replace(self, spec_set_findings={k: v for k, v in keep.items() if v}, envelope_findings=[],
                       global_findings=[f for f in self.global_findings if f.rule not in rules])


def _departure(spec: DuoSpec, others: Sequence[DuoSpec]) -> int:
    """How far a spec departs from the others in palette family, anchor kind and theme (0 to 3 per other spec): the wildcard's job."""
    kinds = {a.kind for a in spec.shared_anchors}
    score = 0
    for o in others:
        score += int(spec.world.palette_family != o.world.palette_family)
        score += int(kinds != {a.kind for a in o.shared_anchors})
        score += int(spec.world.theme.strip().lower() != o.world.theme.strip().lower())
    return score


def wildcard_targets(specs: Sequence[DuoSpec]) -> dict[int, bool]:
    """``{spec index: wanted is_wildcard}`` for the specs that must change so that exactly one is the wildcard.

    No wildcard: the spec that departs most from the others becomes it (ties: the last). More than one: the wildcard that departs most
    stays, the others are cleared (ties: the first stays). Already exactly one: ``{}``."""
    wild = [i for i, s in enumerate(specs) if s.is_wildcard]
    if len(wild) == 1:
        return {}
    scored = [(_departure(s, [o for j, o in enumerate(specs) if j != i]), i) for i, s in enumerate(specs)]
    if not wild:
        best = max(scored, key=lambda t: (t[0], t[1]))[1]
        return {best: True}
    keep = max((t for t in scored if t[1] in wild), key=lambda t: (t[0], -t[1]))[1]
    return {i: False for i in wild if i != keep}


def lint_candidates(candidates: Sequence[tuple[str, Mapping[str, Any] | DuoSpec]], ctx: PlanLintCtx | None = None, *,
                    brief_constraints: Sequence[Any] = (), how_they_differ: str = "", check_set: bool = True) -> LintBundle:
    """Lint ``(spec_id, spec)`` pairs together: every per-spec rule, then the plan-set rules (HARD: set size, exactly one wildcard, the
    structure the brief form named, must-include lines mapped to paths that resolve in every spec; SOFT: the structure mix and the
    wildcard's departure). ``check_set=False`` lints a single replacement spec without the set rules."""
    ctx = ctx or PlanLintCtx()
    parsed = [(sid, parse_loose(sp)) for sid, sp in candidates]
    order = [sid for sid, _ in parsed]
    per: dict[str, LintReport] = {}
    for sid, sp in parsed:
        per[sid] = PR.lint_spec(sp, replace(ctx, subject_sha=sha256_of(sp.model_dump(mode="json"))))
    if check_set:
        set_report = PR.lint_plan_set([sp for _, sp in parsed], ctx, list(brief_constraints), how_they_differ)
    else:
        set_report = LintReport()
    bundle = LintBundle(order=order, per_spec=per, set_report=set_report)
    specs = [sp for _, sp in parsed]
    by_spec: dict[str, list[Finding]] = defaultdict(list)
    wc = wildcard_targets(specs) if check_set else {}
    for f in set_report.findings:
        if not f.hard:
            continue
        m = SET_PATH.match(f.path)
        if m and int(m.group(1)) < len(order):
            by_spec[order[int(m.group(1))]].append(Finding(f.check_id, f.rule, m.group(2) or "", f.message, True))
        elif f.rule == "wildcard_count":
            for i, want in wc.items():
                msg = ("the plan set has no wildcard: set is_wildcard to true on this spec (it departs most from the others)" if want
                       else "the plan set has more than one wildcard: set is_wildcard to false on this spec")
                by_spec[order[i]].append(Finding(f.check_id, f.rule, "/is_wildcard", msg, True))
        elif f.path.startswith(ENVELOPE_PREFIXES):
            bundle.envelope_findings.append(f)
        else:
            bundle.global_findings.append(f)
    bundle.spec_set_findings = dict(by_spec)
    return bundle


# ---------------------------------------------------------------------------------------------------- reviser findings
def reviser_findings(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    """Numbered HARD findings for ``<findings>`` of L6 (bible §9.6): ``{number, path, problem, rule}``."""
    hard = [f for f in findings if f.hard]
    return [{"number": i, "path": f.path or "/", "problem": f.message, "rule": f.rule} for i, f in enumerate(hard, 1)]


def critic_findings(fixes: Iterable[Mapping[str, Any]], *, start: int = 1, min_severity: str = "high") -> list[dict[str, Any]]:
    """The critic's fixes at or above ``min_severity`` as findings (a high-severity fix is a revision trigger; low and medium are not)."""
    floor = SEVERITY_ORDER[min_severity]
    out = []
    for fx in fixes:
        if SEVERITY_ORDER.get(str(fx.get("severity", "low")), 0) < floor:
            continue
        direction = str(fx.get("direction", "")).strip()
        problem = str(fx.get("problem", "")).strip() + (f"; {direction}" if direction else "")
        out.append({"number": start + len(out), "path": str(fx.get("path", "/")), "problem": problem, "rule": "critic"})
    return out


def findings_text(findings: Sequence[Mapping[str, Any]]) -> str:
    """The numbered finding list as the reviser reads it: ``1. /a/top/base_ref: problem (rule)``."""
    return "\n".join(f"{f['number']}. {f['path']}: {f['problem']} ({f['rule']})" for f in findings)


def finding_paths(findings: Iterable[Mapping[str, Any]]) -> list[str]:
    """The pointers a revision patch may touch (an op at or below one of these paths is in scope, CHK-G0-11)."""
    return [str(f["path"]) for f in findings if f.get("path") not in (None, "", "/")] or [""]


# ---------------------------------------------------------------------------------------------------- CHK-G1-07
@dataclass(frozen=True)
class SetShape:
    ok: bool
    notice: str
    evidence: str


def gate1_set_shape(shown: Sequence[Mapping[str, Any] | DuoSpec], *, wildcard_dropped: bool = False,
                    dropped_reasons: Sequence[str] = ()) -> SetShape:
    """What Gate 1 may show (CHK-G1-07, FAILURE_MODES CON-07): exactly 3 plans with one wildcard label, or the remaining plans with the
    visible notice. A documented case of the second kind is never an ASSERT failure that crashes the step."""
    specs = [parse_loose(s) for s in shown]
    n_wild = sum(1 for s in specs if s.is_wildcard)
    cfg = data_json("rules.json")["plan"]["plan_set"]
    if len(specs) == int(cfg["specs"]) and n_wild == int(cfg["wildcards"]):
        return SetShape(True, "", f"{len(specs)} plans, one wildcard")
    if wildcard_dropped or n_wild == 0:
        return SetShape(True, WILDCARD_NOTICE, f"{len(specs)} plans; {WILDCARD_NOTICE}")
    why = "; ".join(dropped_reasons)[:200]
    return SetShape(True, f"only {len(specs)} plans could be built" + (f": {why}" if why else ""), f"{len(specs)} plans, {n_wild} wildcard(s)")


def gate1_set_check(shown: Sequence[Mapping[str, Any] | DuoSpec], *, wildcard_dropped: bool = False, front_back_ready: bool = True,
                    subject_sha: str = "") -> CheckResult:
    """CHK-G1-07 (ASSERT): the gate shows the 3 plans with one wildcard label and front and back of both characters, or the documented
    notice. It fails only when the shape is neither."""
    shape = gate1_set_shape(shown, wildcard_dropped=wildcard_dropped)
    ok = shape.ok and front_back_ready
    return runner.build_result("CHK-G1-07", passed=ok, subject_sha=subject_sha, metric="gate1_shape",
                               evidence=shape.evidence + ("" if front_back_ready else "; a character has no front or back view"))


def blocking(results: Iterable[CheckResult]) -> list[CheckResult]:
    return [r for r in results if policy.is_blocking(r)]
