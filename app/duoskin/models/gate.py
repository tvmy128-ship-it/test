"""Gates, tiles, decisions and change requests (APP_SPEC §6.9).

Additive fields (foundation track): ``Gate.step_id`` (the step the gate holds), ``GateDecision.provisional``
(an approve decision waiting for "approve anyway", §9.9) and ``GateDecision.client_decision_id`` (the idempotency key,
also stored in the ``decisions`` table).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from duoskin.models.common import PartId, Sha256, Strict, UtcDatetime
from duoskin.models.part import ApprovalRecord


class GateKind(StrEnum):
    CONCEPT = "concept"                 # Gate 1
    PART_BOARD = "part_board"           # Gate 2
    FINAL_PICK = "final_pick"           # Gate 3
    BUDGET = "budget"                   # an estimate would pass the cap or the ask threshold
    MANUAL_IMPORT = "manual_import"     # waiting for a user-made mesh (Tripo website, polish)
    CHANGE_CONFIRM = "change_confirm"   # the L7 diff + redo list + estimate; the user confirms or cancels
    CLARIFY = "clarify"                 # L7 needs_clarification
    HUMAN_REVIEW = "human_review"       # ladder rung 6, or a rung-5 notice (plan revision needed)
    SETUP_APPROVAL = "setup_approval"   # house-style exemplars, library admission


class GateAction(StrEnum):
    APPROVE = "approve"
    APPROVE_ALL = "approve_all"
    REIMAGINE = "reimagine"             # same plan/spec, new nonce
    CHANGE = "change"                   # typed change -> L7 -> CHANGE_CONFIRM
    NEW_PLAN = "new_plan"               # Gate 1 only
    BACK_TO_CONCEPT = "back_to_concept"  # Gate 2 only
    SELECT_ALTERNATIVE = "select_alternative"
    MAKE_MANUAL = "make_manual"         # hair/accessory: "Make it myself on Tripo"
    FLIP_MIRRORED = "flip_mirrored"     # hair/accessory: "Flip left/right (I checked)"; only when the mirrored match wins (§10.9)
    PICK = "pick"                       # Gate 3 winner
    EXPORT = "export"                   # Gate 3
    OVERRIDE_WARNING = "override_warning"
    CONFIRM = "confirm"
    CANCEL = "cancel"                   # CHANGE_CONFIRM, CLARIFY, SETUP_APPROVAL
    CONTINUE = "continue"
    RAISE_CAP = "raise_cap"
    STOP = "stop"                       # BUDGET


class TileState(StrEnum):
    GENERATING = "generating"
    READY = "ready"
    APPROVED = "approved"
    STALE = "stale"
    RECHECK = "recheck"
    NEEDS_HUMAN = "needs_human"
    WAITING_MANUAL = "waiting_manual"
    FAILED = "failed"


class GateTile(Strict):
    tile_id: str                        # Gate 1: "plan0".."plan2"; Gate 2: a part id; Gate 3: a candidate id
    part_id: PartId | None = None
    label: str
    state: TileState = TileState.READY
    assets: dict[str, Sha256] = Field(default_factory=dict)    # role -> asset shown
    alternatives: list[dict[str, Sha256]] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)        # cost so far, match score, tris, check summary
    badges: list[str] = Field(default_factory=list)            # "Wildcard", "Views from GPT (lower reliability)", ...
    allowed_actions: list[GateAction]
    version: int = 0


class Gate(Strict):
    id: str
    project_id: str
    job_id: str
    kind: GateKind
    state: Literal["open", "decided", "superseded"] = "open"
    tiles: list[GateTile]
    spent_usd_at_open: float = 0.0
    first_choice_at: UtcDatetime | None = None     # warnings are released after this (§9.9)
    opened_at: UtcDatetime
    decided_at: UtcDatetime | None = None
    step_id: str | None = None                     # additive: the step this gate holds in WAITING_USER


class GateDecisionIn(Strict):           # API request body
    tile_id: str
    action: GateAction
    text: str = Field(default="", max_length=1000)     # "Change..." text or New-plan reasons (data, never instructions)
    mask_sha: Sha256 | None = None      # brush mask for a local edit, uploaded first via /api/uploads
    choice: str | None = None           # alternative index, plan id, candidate id, warning id or new cap
    target: str | None = None           # Gate 1 Reimagine/Change: "a" | "b" | "both" (default both); face tile:
                                        # "iris" | "lash" | "brow" | "mouth_closed" | "mouth_open" | "all"
    expected_version: int               # optimistic lock: 409 if the tile changed (ENG-11)
    client_decision_id: str = Field(min_length=1, max_length=80)   # idempotency key


class GateDecision(Strict):             # stored
    id: str
    gate_id: str
    tile_id: str
    action: GateAction
    text: str = ""
    mask_sha: Sha256 | None = None
    choice: str | None = None
    target: str | None = None           # additive: Gate 1 "a" | "b" | "both"; a face tile's part (GateDecisionIn.target)
    decided_at: UtcDatetime
    warnings_shown: list[str] = Field(default_factory=list)       # SOFT warning ids shown AFTER the first choice
    warnings_overridden: list[str] = Field(default_factory=list)  # logged as calibration labels
    change_request_id: str | None = None
    resulting_spec_id: str | None = None
    spawned_step_ids: list[str] = Field(default_factory=list)
    approval: ApprovalRecord | None = None
    provisional: bool = False           # additive: waits for "approve anyway" (§9.9)
    client_decision_id: str = ""        # additive: the idempotency key


class ChangeRequest(Strict):
    id: str
    project_id: str
    gate_id: str
    tile_id: str | None = None
    text: str                           # raw user text
    mask_sha: Sha256 | None = None
    plan: dict[str, Any] | None = None  # the L7 ChangePlan JSON
    status: Literal["interpreting", "needs_clarification", "awaiting_confirm", "applied", "cancelled", "rejected"]
    invalidation: dict[str, Any] | None = None    # InvalidationReport (§9.8)
    estimate_usd: float | None = None
