"""Gate B: the yes/no rule library and the call wrapper (PROMPT_BIBLE §2.9, §7.2, §16.1; FAILURE_MODES VLM-01..VLM-07).

* **The rule library** is ``data/rules.json`` (the bible §7.2 table): one stable ``RuleId`` enum, a statement per rule, hard or soft.
  Only the statement is ever sent to the judge. ``RuleId`` is the closed enum of the ``AssetCheck`` schema (``models/llm_io.py``);
  it never changes per call (LLM-08), only when the library changes (``RULES_VERSION``).
* **The call wrapper** :func:`run_gate_b` sends ONE rule per call by default (``rules_per_call=1``; the library allows at most 5) through
  a **provider callable** ``provider(GateBRequest) -> AssetCheck | dict | str``. The always-on ``ip_*`` rules never share a call with
  other rules. The returned rule-id set must equal the requested set (VLM-03): a mismatch or an unparsable answer is retried once and
  then fails closed (``ran=False``). ``unsure`` counts as a fail; a failed hard rule fails the asset; soft rules only rank and warn.
  Rules tagged ``code_measurable`` may not go to the VLM (VLM-05). Rules the code cannot measure can be asked ``votes`` times
  (majority of 3, ``vlm.costly_votes``) before a costly step; automatic approval needs all votes.
* An ``unsure`` on any ``ip_*`` rule is reported in ``GateBResult.escalate`` for L13 (bible §17.3).

The wrapper reads no key and does no network I/O: the provider callable is the only thing that talks to Claude (or to a mock).
Kind (hard/soft) always comes from ``data/checks.json`` through ``checks/runner.py``, never from this module.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from duoskin.checks import runner
from duoskin.checks.model import CheckResult
from duoskin.models.llm_io import (ALL_RULE_IDS, LOCATIONS, RULES_VERSION, AssetCheck, RuleDef, RuleId, Verdict, _rules_doc,
                                   rule_library)
from duoskin.prompts.limits import thr

__all__ = ["ALL_RULE_IDS", "RULES_VERSION", "RuleId", "RuleDef", "RuleRequest", "GateBRequest", "GateBResult", "rule", "is_hard",
           "statement", "change_aware_statement", "rules_block", "plan_calls", "run_gate_b", "run_rule", "ProviderFn",
           "MATCHES_CONCEPT_RULES", "IP_RULES"]

IP_RULES = ("ip_no_brand", "ip_no_known_character", "ip_no_text", "ip_age_appropriate")
MATCHES_CONCEPT_RULES = ("hr_matches_concept", "ac_matches_concept", "pr_matches_concept", "fh_matches_concept", "gm_matches_concept")
_SLOT = re.compile(r"\{([^{}]+)\}")


class GateBError(ValueError):
    """The request is invalid (an unknown rule id, a missing slot, a code-measurable rule)."""


# --------------------------------------------------------------------------------------------- the library
def rule(rule_id: str) -> RuleDef:
    try:
        return rule_library()[rule_id]
    except KeyError:
        raise GateBError(f"unknown rule id {rule_id!r}") from None


def is_hard(rule_id: str) -> bool:
    """Whether the bible marks the rule hard (the policy registry decides what blocks; this is the library's own flag)."""
    return rule(rule_id).hard


def _slot_key(name: str) -> str:
    return "_".join(name.lower().split())


def statement(rule_id: str, **slots: str) -> str:
    """The statement with its ``{slots}`` filled (slot names with spaces use underscores: ``accessory_list``).

    A missing or empty slot raises: a half-filled statement would be a different question.
    """
    text = rule(rule_id).statement
    have = {_slot_key(k): v for k, v in slots.items()}

    def fill(m: re.Match[str]) -> str:
        key = _slot_key(m.group(1))
        val = str(have.get(key, "")).strip()
        if not val:
            raise GateBError(f"rule {rule_id}: slot {m.group(1)!r} has no value")
        return val

    return _SLOT.sub(fill, text)


def change_aware_statement(rule_id: str, fix_sentence: str, **slots: str) -> str:
    """After an applied change the five ``*_matches_concept`` rules judge only what the change did not touch (bible §7.2).

    "...matches the reference crop in everything except this requested change: {fix_sentence}".
    """
    if rule_id not in MATCHES_CONCEPT_RULES:
        raise GateBError(f"{rule_id} is not a *_matches_concept rule")
    fix = " ".join(str(fix_sentence).split())
    if not fix:
        raise GateBError("a change-aware statement needs the fix sentence")
    return f"{statement(rule_id, **slots).rstrip('.')}, in everything except this requested change: {fix}"


@dataclass(frozen=True)
class RuleRequest:
    """One rule to ask, with the values for its slots and an optional statement override (change-aware statements)."""

    rule_id: str
    slots: Mapping[str, str] = field(default_factory=dict)
    statement_override: str = ""

    def text(self) -> str:
        return self.statement_override or statement(self.rule_id, **self.slots)


def rules_block(requests: Sequence[RuleRequest]) -> str:
    """The numbered ``<rules>`` lines (``rule_id: statement``) of one call (bible §16.1 content order, item 4)."""
    return "\n".join(f"{i}. {r.rule_id}: {r.text()}" for i, r in enumerate(requests, 1))


def plan_calls(rule_ids: Sequence[str], rules_per_call: int | None = None) -> list[list[str]]:
    """Group rules into calls: the ``ip_*`` rules never share a call with other rules; at most ``max_rules_per_call`` per call."""
    cfg = _rules_doc()["gate_b"]
    per = int(rules_per_call or cfg["default_rules_per_call"])
    if not 1 <= per <= int(cfg["max_rules_per_call"]):
        raise GateBError(f"rules_per_call must be 1..{cfg['max_rules_per_call']}")
    ids = list(dict.fromkeys(rule_ids))
    for rid in ids:
        rule(rid)
    ip = [r for r in ids if r in IP_RULES]
    other = [r for r in ids if r not in IP_RULES]
    return [grp[i:i + per] for grp in (ip, other) for i in range(0, len(grp), per)]


# --------------------------------------------------------------------------------------------- the provider contract
@dataclass(frozen=True)
class GateBRequest:
    """What the provider callable receives for one call: the compiled L11 prompt and the rule ids it must answer."""

    rule_ids: tuple[str, ...]
    prompt: Any                      # prompts.llm.LlmPrompt (system blocks, user text, route, schema name)
    context: Mapping[str, Any]       # images and other call context supplied by the caller (never read here)
    attempt: int                     # 0, then 1 for the single retry
    vote: int                        # 0 .. votes-1


ProviderFn = Callable[[GateBRequest], "AssetCheck | Mapping[str, Any] | str | bytes"]


@dataclass
class GateBResult:
    results: list[CheckResult]
    verdicts: dict[str, list[Verdict]]
    calls: int
    escalate: list[str]              # ip_* rules that came back unsure: send them to L13 before BUILD

    @property
    def passed(self) -> bool:
        """No hard rule failed or did not run. Soft rules never decide."""
        return not any(r.kind in ("hard", "assert") and not r.passed for r in self.results)

    def failed_rules(self, *, hard_only: bool = False) -> list[str]:
        return [r.check_id for r in self.results if not r.passed and (not hard_only or r.kind in ("hard", "assert"))]


def _parse(raw: Any) -> AssetCheck:
    if isinstance(raw, AssetCheck):
        return raw
    if isinstance(raw, (str, bytes)):
        return AssetCheck.model_validate_json(raw)
    if isinstance(raw, Mapping):
        return AssetCheck.model_validate(dict(raw))
    raise TypeError(f"the provider returned {type(raw).__name__}, not an AssetCheck")


def _ask_once(provider: ProviderFn, rule_ids: tuple[str, ...], prompt: Any, context: Mapping[str, Any], vote: int) -> AssetCheck | str:
    """One provider call with at most one retry; returns the parsed answer or the reason it failed."""
    reason = ""
    for attempt in range(2):
        try:
            answer = _parse(provider(GateBRequest(rule_ids, prompt, context, attempt, vote)))
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
            reason = f"unusable answer ({type(exc).__name__})"
            continue
        got = [v.rule_id for v in answer.verdicts]
        if sorted(got) != sorted(rule_ids) or len(set(got)) != len(got):
            reason = f"the answer covers {sorted(set(got))}, not the requested {sorted(rule_ids)}"
            continue
        return answer
    return reason


def run_gate_b(provider: ProviderFn, requests: Sequence[RuleRequest], *, measured_facts: str = "", context: Mapping[str, Any] | None = None,
               rules_per_call: int | None = None, votes: int = 1, unanimous: bool = False, subject_sha: str = "",
               inventory: Any = None) -> GateBResult:
    """Ask the judge every requested rule (one rule per call by default) and return one ``CheckResult`` per rule.

    ``votes`` repeats each call (3 before a costly step: a majority of ``vlm.costly_votes`` must pass; ``unanimous=True`` requires all,
    which automatic approval needs). A rule whose answers never became valid, or whose rule is ``code_measurable``, is a failed check with
    ``ran=False`` (fail closed).
    """
    from duoskin.prompts.llm import compile_llm

    context = context or {}
    by_id = {r.rule_id: r for r in requests}
    if len(by_id) != len(requests):
        raise GateBError("a rule may appear once per run")
    banned = set(_rules_doc().get("code_measurable", ()))
    if votes < 1:
        raise GateBError("votes must be 1 or more")
    results: list[CheckResult] = []
    verdicts: dict[str, list[Verdict]] = {}
    escalate: list[str] = []
    n_calls = 0
    need_majority, total = thr("vlm.costly_votes")
    for group in plan_calls(list(by_id), rules_per_call):
        blocked = [g for g in group if g in banned]
        for g in blocked:
            results.append(runner.fail_closed(g, "this rule is measured in code and may not be sent to the judge (VLM-05)", subject_sha))
        group = [g for g in group if g not in banned]
        if not group:
            continue
        prompt = compile_llm("L11.asset_checker", {"measured_facts": measured_facts or "none",
                                                   "rules": rules_block([by_id[g] for g in group])}, inventory=inventory)
        answers: list[AssetCheck] = []
        why = ""
        for vote in range(votes):
            n_calls += 1
            got = _ask_once(provider, tuple(group), prompt, context, vote)
            if isinstance(got, str):
                why = got
            else:
                answers.append(got)
        for rid in group:
            vs = [v for a in answers for v in a.verdicts if v.rule_id == rid]
            verdicts[rid] = vs
            if len(vs) < votes:
                results.append(runner.fail_closed(rid, f"{why or 'no valid answer'} after a retry", subject_sha))
                continue
            passes = sum(1 for v in vs if v.verdict == "pass")
            if votes == 1:
                ok = passes == 1
            else:
                ok = passes == votes if unanimous else passes >= min(need_majority, votes)
            if rid in IP_RULES and any(v.verdict == "unsure" for v in vs):
                escalate.append(rid)
            worst = next((v for v in vs if v.verdict != "pass"), vs[0])
            loc = f" [{worst.location}]" if worst.location != "none" else ""
            results.append(runner.build_result(
                rid, passed=ok, subject_sha=subject_sha, metric="verdict", value=passes / len(vs), threshold=f"pass ({votes} vote(s), of {total} when costly)" if votes > 1 else "pass",
                evidence=f"{worst.verdict}{loc}: {worst.observation}"))
    return GateBResult(results=results, verdicts=verdicts, calls=n_calls, escalate=sorted(set(escalate)))


def run_rule(provider: ProviderFn, request: RuleRequest, **kw: Any) -> CheckResult:
    """One rule, one call (a convenience around :func:`run_gate_b`)."""
    return run_gate_b(provider, [request], **kw).results[0]


def locations() -> tuple[str, ...]:
    """The closed ``location`` values of a verdict (the repair mask seed, bible §16.1)."""
    return LOCATIONS
