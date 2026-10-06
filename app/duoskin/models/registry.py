"""Registries, memory, labels and the small bookkeeping tables (APP_SPEC §6.13, §6.14)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from duoskin.models.common import Sha256, Strict, UtcDatetime


class RegistryEntry(Strict):
    """A row of ``registry_face`` or ``registry_print``. Exact pixel reuse is blocked forever; near-duplicates within
    the sliding window (or rows with ``listed=True``)."""

    id: str
    project_id: str
    asset_sha: Sha256
    pixel_sha: Sha256
    phash: str
    embedding: bytes | None = None
    character: Literal["a", "b"] | None = None
    part_role: str = ""
    grammar: dict[str, Any] = Field(default_factory=dict)
    duo_seq: int
    listed: bool = False
    registered_at: UtcDatetime


class DuoMemory(Strict):
    project_id: str
    embedding: bytes | None = None
    dna_card: dict[str, Any] = Field(default_factory=dict)
    kit_ids: dict[str, Any] = Field(default_factory=dict)
    approved_at: UtcDatetime


class Label(Strict):
    id: str
    kind: Literal["clone_real_stranger", "like_dislike", "rule_verdict", "warning_override"]
    source: Literal["gate", "drill", "calibration"]
    subject_ids: list[str] = Field(default_factory=list)
    value: Any = None
    ts: UtcDatetime


class CheckStat(Strict):
    """One check in one ISO week (``window_start`` = the Monday, ``YYYY-MM-DD``). ``check_id="*gate:<kind>"`` rows hold the denominators:
    ``approved_total`` / ``rejected_total`` count the tile decisions of that gate kind (additive fields of the learning track)."""

    check_id: str
    window_start: str
    flagged_approved: int = 0
    flagged_rejected: int = 0
    shown: int = 0
    overridden: int = 0
    approved_total: int = 0
    rejected_total: int = 0


class InboxEntry(Strict):
    path: str
    sha256: Sha256 | None = None
    size: int = 0
    state: str = "new"
    assigned_part: str | None = None
    info: dict[str, Any] = Field(default_factory=dict)
    seen_at: UtcDatetime


class ChildProc(Strict):
    pid: int
    create_time: float
    step_id: str | None = None
    started_at: UtcDatetime
