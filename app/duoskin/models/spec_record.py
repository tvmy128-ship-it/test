"""``SpecRecord`` and patches (APP_SPEC §6.4).

The ``DuoSpec`` itself is owned by another track (``models/spec.py``). This module stores it as an opaque JSON dict so
the foundation never depends on that schema; the pipeline validates it with ``DuoSpec`` before and after use.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from duoskin.checks.model import CheckResult
from duoskin.models.common import Sha256, Strict

# ``RevisionOp`` (L6, with ``finding``) and ``ChangeOp`` (L7, with ``reason``) are the LLM-facing classes of Track S
# (``models/llm_io.py``, locked in ``prompts/SCHEMAS.lock``). They are re-exported here so ``SpecRecord.patch_from_parent`` and the
# schema lock compare the same classes (APP_SPEC §6.4).
from duoskin.models.llm_io import ChangeOp, RevisionOp

__all__ = ["ChangeOp", "RevisionOp", "SpecRecord"]


class SpecRecord(Strict):
    id: str
    project_id: str
    plan_set_id: str
    plan_index: int                                 # 0..2 inside its PlanSet
    parent_spec_id: str | None = None
    version: int = 1                                # 1 = planner output; +1 per applied patch
    created_by: Literal["planner", "reviser", "change", "palette_lock", "user"] = "planner"
    patch_from_parent: list[RevisionOp | ChangeOp] = Field(default_factory=list)   # internal model: a union is allowed
    spec: dict[str, Any]                            # a DuoSpec as plain JSON (opaque here)
    schema_version: Literal[1] = 1
    text_policy: Literal["no_text"] = "no_text"     # code constant, never a model field
    palette_source: Literal["planner", "concept_extracted"] = "planner"
    status: Literal["candidate", "dropped", "shown", "approved", "superseded"] = "candidate"
    lint: list[CheckResult] = Field(default_factory=list)
    critic_levels: dict[str, str] = Field(default_factory=dict)   # criterion -> level (L4)
    pairwise_wins: float = 0.0                      # L5, both orders; disagreement = 0.5 each
    rank: int | None = None
    sha256: Sha256                                  # sha256_of(spec)
