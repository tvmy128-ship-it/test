"""Hard vs soft check policy (APP_SPEC §3.1, PROPOSAL_DECISION safeguards).

``data/checks.json`` lists every check id with its ``kind`` (``hard | soft | assert``), its ``policy_class``, the stage,
the threshold keys, the warning text and whether it may be demoted. This module loads that file and answers
``meta(check_id)``. Unknown ids never crash: they get a conservative SOFT "unregistered" meta, and
``is_registered()`` tells the two cases apart.

Rules enforced here (and by ``tests/checks/test_chk_policy.py``):

* only the classes in ``HARD_CLASSES`` may be hard or assert;
* every ``taste``, ``colour_distance``, ``restraint`` and ``novelty`` check is soft, and a soft check can never be
  turned hard by an override;
* checks in the classes ``roblox``, ``ip``, ``stray_text``, ``security`` and ``integrity`` are never demotable;
* an override (``settings.check_overrides``) can only demote a demotable hard check to soft.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from duoskin.checks.model import CheckResult
from duoskin.models.common import Strict

HARD_CLASSES = {"roblox", "buildability", "ip", "stray_text", "registry", "clone_lower_edge", "integrity", "security",
                "consistency"}   # consistency = part-vs-concept palette (CHK-A13/D05) and finalize drift; FM marks them HARD
SOFT_CLASSES = {"taste", "colour_distance", "restraint", "novelty"}
NEVER_DEMOTABLE = {"roblox", "ip", "stray_text", "security", "integrity"}
UNREGISTERED_CLASS = "unregistered"

Kind = Literal["hard", "soft", "assert"]
Stage = Literal["startup", "call", "G0", "gate1", "gate2", "build", "gate3", "export", "always"]
Requires = Literal["head_base", "body_base"]


class CheckMeta(Strict):
    check_id: str
    kind: Kind
    policy_class: str
    stage: Stage
    threshold_keys: list[str]           # names in checks/thresholds.py
    warn_text: str                      # plain sentence shown under "approve anyway?"
    demotable: bool                     # may be auto-demoted to warning when miscalibrated (DES/UNV thresholds only)
    # ---- additive fields (not in the APP_SPEC sketch; all have defaults) ----
    title: str = ""
    fm_ids: list[str] = []              # FAILURE_MODES ids this check covers
    contract_ids: list[str] = []        # the other id of the same check (A_* <-> CHK-A*)
    requires: list[Requires] = []       # kit that must be present, else the check is not_applicable
    registered: bool = True

    @model_validator(mode="after")
    def _policy_rules(self) -> "CheckMeta":
        if not self.registered:
            return self
        if self.policy_class in SOFT_CLASSES and self.kind != "soft":
            raise ValueError(f"{self.check_id}: class {self.policy_class} must be soft")
        if self.kind in ("hard", "assert") and self.policy_class not in HARD_CLASSES:
            raise ValueError(f"{self.check_id}: class {self.policy_class} may not be {self.kind}")
        if self.demotable and (self.policy_class in NEVER_DEMOTABLE or self.kind != "hard"):
            raise ValueError(f"{self.check_id}: not demotable (class {self.policy_class}, kind {self.kind})")
        return self


_DATA = Path(__file__).resolve().parent.parent / "data" / "checks.json"
_OVERRIDES: contextvars.ContextVar[Mapping[str, str] | None] = contextvars.ContextVar("duoskin_check_overrides",
                                                                                        default=None)


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, CheckMeta]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, CheckMeta] = {}
    for row in raw["checks"]:
        m = CheckMeta(**row)
        if m.check_id in out:
            raise ValueError(f"duplicate check id {m.check_id}")
        out[m.check_id] = m
    return out


def registry(path: str | Path | None = None) -> dict[str, CheckMeta]:
    """All registered checks by id (cached; pass ``path`` for a test registry)."""
    return _load(str(path or _DATA))


def is_registered(check_id: str) -> bool:
    return check_id in registry()


def meta(check_id: str) -> CheckMeta:
    """The registered meta, or a SOFT ``unregistered`` meta for an id the registry does not know (never raises)."""
    m = registry().get(check_id)
    if m is not None:
        return m
    return CheckMeta(check_id=str(check_id), kind="soft", policy_class=UNREGISTERED_CLASS, stage="always",
                     threshold_keys=[], warn_text="This check is not in the registry, so it only warns.",
                     demotable=False, registered=False)


def ids_in_class(policy_class: str) -> list[str]:
    return sorted(i for i, m in registry().items() if m.policy_class == policy_class)


def can_demote(check_id: str) -> bool:
    m = registry().get(check_id)
    return bool(m and m.kind == "hard" and m.demotable and m.policy_class not in NEVER_DEMOTABLE)


def validate_overrides(overrides: Mapping[str, str]) -> dict[str, str]:
    """Check a ``settings.check_overrides`` map. Only ``"soft"`` demotions of demotable hard checks are allowed."""
    clean: dict[str, str] = {}
    for cid, kind in overrides.items():
        if kind != "soft":
            raise ValueError(f"{cid}: only a demotion to 'soft' is allowed, got {kind!r}")
        if not can_demote(cid):
            raise ValueError(f"{cid}: this check cannot be demoted")
        clean[cid] = "soft"
    return clean


@contextlib.contextmanager
def check_overrides(overrides: Mapping[str, str]) -> Iterator[None]:
    """Apply demotions for the duration of a ``with`` block (``run_check`` reads them)."""
    tok = _OVERRIDES.set(validate_overrides(overrides))
    try:
        yield
    finally:
        _OVERRIDES.reset(tok)


def active_overrides() -> Mapping[str, str]:
    return _OVERRIDES.get() or {}


def effective_kind(check_id: str, overrides: Mapping[str, str] | None = None, declared: Kind | None = None) -> Kind:
    """The kind that counts: the registry's kind, demoted by a valid override.

    An unregistered id keeps the kind its producer declared (``declared``), else soft.
    """
    m = registry().get(check_id)
    if m is None:
        return declared or "soft"
    ov = overrides if overrides is not None else active_overrides()
    if m.kind == "hard" and ov.get(check_id) == "soft" and can_demote(check_id):
        return "soft"
    return m.kind


def is_blocking(result: CheckResult, overrides: Mapping[str, str] | None = None) -> bool:
    """True when this result blocks its gate: a failed (or not-run) hard/assert check.

    ``not_applicable`` results count as passed and never block; soft results never block.
    """
    kind = effective_kind(result.check_id, overrides, declared=result.kind)
    return kind in ("hard", "assert") and not result.passed


def apply_policy(results: list[CheckResult], overrides: Mapping[str, str] | None = None) -> list[CheckResult]:
    """Return copies whose ``kind`` follows the registry and the overrides (what ``gate_verdict`` should see)."""
    out: list[CheckResult] = []
    for r in results:
        k = effective_kind(r.check_id, overrides, declared=r.kind)
        out.append(r if k == r.kind else r.model_copy(update={"kind": k}))
    return out
