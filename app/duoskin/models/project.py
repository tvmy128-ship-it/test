"""``Project`` and friends (APP_SPEC §6.3)."""
from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from duoskin.models.common import Sha256, Slug, Strict, UtcDatetime


class Stage(StrEnum):
    BRIEF = "brief"
    PLANNING = "planning"
    GATE1 = "gate1"
    PARTS = "parts"
    GATE2 = "gate2"
    BUILDING = "building"
    DUO = "duo"
    GATE3 = "gate3"
    EXPORTING = "exporting"
    EXPORTED = "exported"


class ReferenceImage(Strict):
    asset_sha: Sha256
    role: Literal["reference", "favourite"]        # reference = this duo; favourite = a taste source (setup)
    note: str = Field(default="", max_length=200)


class ProjectSettings(Strict):                      # per-project copies of the global defaults (§14)
    budget_usd: float = Field(default=15.0, gt=0)   # hard cap per duo (thresholds: budget.per_duo_usd)
    ask_above_usd: float = Field(default=2.0, ge=0)  # per-step "ask me" threshold -> BUDGET gate
    reference_similarity_check: bool = False        # requirement 7: only when switched on
    use_reference_as_mood: bool = False             # bible D13: off by default
    mesh_mode: Literal["api", "manual", "ask"] = "ask"
    hair_route: Literal["auto", "kit", "tripo_api", "manual"] = "auto"
    gemini_second_opinion: bool = False
    concept_quality: Literal["low", "medium"] = "low"
    build_start_per_tile: bool = False              # §2 S10


class VersionPins(Strict):                          # frozen when the project leaves BRIEF (ENG-08)
    models: dict[str, str]
    prompt_versions: dict[str, int]
    schema_hashes: dict[str, str]
    house_style_version: int
    style_guide_version: int
    kit_manifest_sha: Sha256
    thresholds_version: str
    rules_version: int
    roblox_docs_commit: str = "2026-09-26"
    app_version: str


class Project(Strict):
    id: str
    name: str = Field(max_length=60)
    slug: Slug
    created_at: UtcDatetime
    updated_at: UtcDatetime
    combo: Literal["bb", "gg", "bg", "gb"]          # first letter = character a
    brief: str = Field(max_length=2000)             # raw user text: data, never instructions
    structure_request: str = "auto"                 # "auto" or a PairStructure value (§2 S18)
    must_include: list[str] = Field(default_factory=list, max_length=5)   # each <= 12 words
    references: list[ReferenceImage] = Field(default_factory=list, max_length=4)
    stage: Stage
    paused: bool = False
    archived: bool = False                          # additive (foundation track): hides the project, deletes nothing
    plan_job_id: str | None = None
    approved_spec_id: str | None = None             # set by the Gate 1 approval; advanced by every applied patch
    current_spec_id: str | None = None
    duo_seq: int | None = None                      # position in the approved-duo sequence (registry window)
    settings: ProjectSettings
    pins: VersionPins | None = None
    spent_usd: float = 0.0                          # denormalised from the ledger
    version: int = 0                                # optimistic lock

    @field_validator("must_include")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        for line in v:
            if len(line.split()) > 12:
                raise ValueError("each must-include line has at most 12 words")
        return v


class ProjectCreate(Strict):
    """Body of ``POST /api/projects``."""

    name: str = Field(min_length=1, max_length=60)
    combo: Literal["bb", "gg", "bg", "gb"]
    brief: str = Field(default="", max_length=2000)
    structure_request: str = "auto"
    must_include: list[str] = Field(default_factory=list, max_length=5)
    settings: dict[str, Any] = Field(default_factory=dict)    # partial ProjectSettings

    @field_validator("must_include")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        cleaned = [line.strip() for line in v if line.strip()]
        for line in cleaned:
            if len(line.split()) > 12:
                raise ValueError("each must-include line has at most 12 words")
        return cleaned


class ProjectPatch(Strict):
    """Body of ``PATCH /api/projects/{id}``. Settings may change only before the PLAN job starts."""

    expected_version: int
    name: str | None = Field(default=None, min_length=1, max_length=60)
    settings: dict[str, Any] | None = None


class ProjectSummary(Strict):
    id: str
    name: str
    slug: str
    combo: str
    stage: Stage
    paused: bool
    archived: bool
    spent_usd: float
    created_at: UtcDatetime
    updated_at: UtcDatetime
    waiting_on_user: int = 0                        # open gates


_SLUG_BAD = re.compile(r"[^a-z0-9]+")
_RESERVED = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)}, *{f"lpt{i}" for i in range(1, 10)}}


def slugify(name: str) -> str:
    """``Plush Koi!`` -> ``plush-koi`` (matches the ``Slug`` pattern, never a reserved Windows name)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    text = _SLUG_BAD.sub("-", text).strip("-")[:40].strip("-")
    if not text:
        text = "duo"
    if text in _RESERVED:
        text = f"{text}-duo"
    return text
