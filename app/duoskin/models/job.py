"""``Job`` and ``Step`` (APP_SPEC §6.8).

Additive fields on ``Step`` (foundation track, all defaulted): ``polls`` (remote poll counter that drives the
5 s, then 3 -> 15 s x1.4 backoff), ``budget_ok`` (set by a BUDGET gate "continue once" / "raise cap" decision) and
``gate_id`` (the gate this step is waiting on).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from duoskin.logsetup import redact
from duoskin.models.common import PartId, Sha256, Strict, UtcDatetime


class JobKind(StrEnum):
    SETUP = "setup"                 # house-style bootstrap S0, taste profile L2
    LIBRARY = "library"             # fabric (I7) and shading-panel (I8) library builds
    PLAN = "plan"                   # L1..L6, C1, I1 concept drafts, C2 -> Gate 1 -> C3
    PARTS = "parts"                 # part-board asset loops -> Gate 2
    BUILD = "build"                 # clothing templates, head texture, hair, accessories, body
    MANUAL_MESH = "manual_mesh"     # pack export -> MANUAL_IMPORT gate -> import -> mesh gate (one per part)
    DUO = "duo"                     # C5 renders, duo checks, L12-L14 -> Gate 3
    EXPORT = "export"
    CALIBRATION = "calibration"     # drill renders, judge calibration runs
    REGRESSION = "regression"       # 40-brief regression + variety guard


class JobState(StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATES = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class Job(Strict):
    id: str
    project_id: str | None = None
    kind: JobKind
    state: JobState = JobState.RUNNING
    spec_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    parent_job_id: str | None = None


class StepState(StrEnum):
    PENDING = "pending"                 # waiting for dependencies
    READY = "ready"                     # claimable
    RUNNING = "running"                 # leased by this process
    WAITING_REMOTE = "waiting_remote"   # remote task submitted (remote_ref stored); polled with backoff
    WAITING_USER = "waiting_user"       # a gate or tile is open
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"           # its result is no longer used; kept for history


TERMINAL_STEP_STATES = (StepState.SUCCEEDED, StepState.FAILED, StepState.CANCELLED, StepState.SUPERSEDED)
ACTIVE_STEP_STATES = (StepState.PENDING, StepState.READY, StepState.RUNNING, StepState.WAITING_REMOTE,
                      StepState.WAITING_USER)


class StepError(Strict):
    kind: str                           # providers.base.ErrorKind (§7.1)
    code: str | None = None
    message: str
    retryable: bool = False
    billed: Literal["no", "yes", "unknown"] = "unknown"
    provider_request_id: str | None = None
    user_hint: str = ""

    @field_validator("code", "message", "provider_request_id", "user_hint")
    @classmethod
    def _no_secrets(cls, v: str | None) -> str | None:
        """An error text may come from an exception, a provider body or a URL: no key or signed URL survives into the database, an event or the UI."""
        return redact(v) if isinstance(v, str) else v


class Step(Strict):
    id: str
    job_id: str
    project_id: str | None = None
    part_id: PartId | None = None
    kind: str                           # registry key, e.g. "img.draft", "tripo.multiview" (§8.2)
    handler_version: int = 1
    pool: Literal["api", "cpu", "proc", "none"] = "cpu"
    state: StepState = StepState.PENDING
    deps: list[str] = Field(default_factory=list)              # step ids
    params: dict[str, Any] = Field(default_factory=dict)       # validated by the handler's Params model
    inputs: list[Sha256] = Field(default_factory=list)         # ordered asset shas
    outputs: list[Sha256] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)       # small JSON result
    cache_key: Sha256 | None = None
    cached: bool = False
    nonce: str = ""
    paid: bool = False                  # true for any call that can cost money
    attempt: int = 0
    max_attempts: int = 3
    not_before: UtcDatetime | None = None
    lease_until: UtcDatetime | None = None
    lease_owner: str | None = None      # process instance id
    remote_ref: str | None = None       # Tripo task id, batch id, ...
    remote_state: Literal["none", "submitted", "submission_uncertain", "polling", "slow", "done"] = "none"
    child_pid: int | None = None
    child_create_time: float | None = None
    progress: float = 0.0
    message: str = ""
    error: StepError | None = None
    cost_estimate_usd: float = 0.0
    priority: int = 100                 # lower runs first; the tile the user is looking at gets 10
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    # additive fields (foundation track)
    polls: int = 0
    budget_ok: bool = False
    gate_id: str | None = None


class StepSummary(Strict):
    """A compact step view for the Jobs page and ``/api/state``."""

    id: str
    job_id: str
    project_id: str | None
    part_id: str | None
    kind: str
    state: StepState
    pool: str
    progress: float
    message: str
    attempt: int
    paid: bool
    cached: bool
    error: StepError | None
    remote_state: str
    priority: int
    created_at: UtcDatetime
    finished_at: UtcDatetime | None


class JobView(Strict):
    job: Job
    steps_by_state: dict[str, int]
    steps: list[StepSummary] = Field(default_factory=list)
