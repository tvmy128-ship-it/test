"""``Part`` and approval records (APP_SPEC §6.6, plus the ``build_hash`` second stamp from the APP_SPEC issue list).

Two stamps, never one (issue file, ENG-01 fix):

* ``approval_hash`` (Gate 2) covers the spec slice, inputs, **board** outputs, prompts, models, kit subset and house
  style. It never covers ``build_assets``, which do not exist yet when the user approves.
* ``build_hash`` is written when the part reaches BUILT and is confirmed by the Gate 3 pick. A rebuilt mesh changes
  ``build_hash`` and needs a Gate 3 look, never a Gate 2 re-approval.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from duoskin.models.common import PartId, Sha256, Strict, UtcDatetime


class PartKind(StrEnum):
    COLOURS = "colours"
    FACE = "face"
    HAIR = "hair"
    ACCESSORY = "accessory"
    PRINT = "print"
    SHIRT = "shirt"
    PANTS = "pants"
    DUO = "duo"


class PartState(StrEnum):
    PLANNED = "planned"               # created; nothing generated yet
    GENERATING = "generating"         # asset loop running
    READY = "ready"                   # finals passed HARD checks; the tile waits for the user
    NEEDS_HUMAN = "needs_human"       # ladder rung 6: best-so-far shown with a report
    APPROVED = "approved"             # ApprovalRecord valid
    STALE = "stale"                   # an upstream change invalidated the approval: re-run, then re-approve
    RECHECK = "recheck"               # pair-level checks re-running; the approval survives if they pass (§2 S21)
    WAITING_MANUAL = "waiting_manual"  # Tripo pack or polish pack is out; waiting for an import
    BUILDING = "building"
    BUILT = "built"                   # build outputs exist and passed the file gate (G3)
    FAILED = "failed"                 # stop rule reached with no acceptable version; the user must act


class DepEffect(StrEnum):
    REGENERATE = "regenerate"         # the AI asset must be generated again (costs money)
    RECOMPOSE = "recompose"           # code-only rebuild: recolour, re-place, re-composite, re-render ($0)
    RECHECK = "recheck"               # re-run checks only; the approval survives if they pass


class DepRule(Strict):
    pattern: str                      # JSON Pointer glob over the spec; "{c}" = this part's character, "*" = one segment
    effect: DepEffect


class LadderState(Strict):
    fixes_used: int = 0               # stop rule: <= 3 per part (ladder.max_fixes_per_part)
    rung: int = 0                     # 0 = not in the ladder; 1..6 (§8.9)
    technique_index: int = 0          # position in the asset's technique ladder (bible §19)
    failed_rungs: dict[str, int] = Field(default_factory=dict)   # rung/technique -> failures (twice = skipped)
    best_asset_sha: Sha256 | None = None
    history: list[str] = Field(default_factory=list)             # "rung2:masked_edit:fail", ...


class ApprovalRecord(Strict):
    part_id: PartId
    approval_hash: Sha256             # §9.7 (Gate 2 stamp; board outputs only)
    spec_id: str
    spec_version: int
    spec_slice_sha: Sha256
    input_shas: list[Sha256] = Field(default_factory=list)
    output_shas: list[Sha256] = Field(default_factory=list)
    pins_sha: Sha256
    approved_at: UtcDatetime
    decision_id: str
    # second stamp (additive): written when the part reaches BUILT, confirmed by the Gate 3 pick
    build_hash: Sha256 | None = None
    build_stamped_at: UtcDatetime | None = None
    build_confirmed_at: UtcDatetime | None = None
    build_confirmed_decision_id: str | None = None


class Part(Strict):
    id: PartId
    project_id: str
    character: Literal["a", "b", "duo"]
    kind: PartKind
    label: str                        # "A · plush koi (collar)"
    deps: list[DepRule] = Field(default_factory=list)          # from the §9.8 table
    part_deps: list[PartId] = Field(default_factory=list)      # other parts whose outputs feed this one
    route: str = ""                   # "R1", "I3", "I2", "I4+T1", "compositor", "kit", "tripo_api", ...
    state: PartState = PartState.PLANNED
    board_assets: dict[str, Sha256] = Field(default_factory=dict)   # role -> asset shown at Gate 2
    alternatives: list[dict[str, Sha256]] = Field(default_factory=list)
    build_assets: dict[str, Sha256] = Field(default_factory=dict)   # role -> final files
    approval: ApprovalRecord | None = None
    ladder: LadderState = Field(default_factory=LadderState)
    open_warnings: list[str] = Field(default_factory=list)     # SOFT CheckResult ids (<= 2 shown, after first choice)
    license: Literal["n/a", "tripo_api_private_commercial", "tripo_paid_private_commercial",
                     "tripo_free_public_ccby_noncommercial", "unknown"] = "n/a"
    flags: list[str] = Field(default_factory=list)             # "views_from_gpt", "procedural_folds", ...
    version: int = 0                  # optimistic lock for gate decisions
