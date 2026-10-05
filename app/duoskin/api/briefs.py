"""The brief form's helper routes (APP_SPEC §13, §10.2): the pair-structure dropdown, a gentle check of the typed text and the plan estimate.

Nothing here blocks anything: a brief is data, and the planner reads what the person typed. The check only says which words the design
cannot use (brands, known characters, words printed on clothes) so the person is not surprised when the planner leaves them out.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from duoskin.api import RT
from duoskin.engine.runtime import Runtime
from duoskin.pipeline import brief as BR
from duoskin.prompts import freetext
from duoskin.prompts.catalog import default_ctx

router = APIRouter(prefix="/api")

MUST_INCLUDE_MAX_LINES = 5
MUST_INCLUDE_MAX_WORDS = 12


class BriefCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief: str = Field(default="", max_length=2000)
    must_include: list[str] = Field(default_factory=list, max_length=MUST_INCLUDE_MAX_LINES)
    structure_request: str = "auto"
    combo: Literal["bb", "gg", "bg", "gb"] = "bg"


@router.get("/briefs/structures")
def structures() -> dict[str, Any]:
    """The "Pair structure" dropdown: ``auto`` first, then every structure with a plain label."""
    return {"structures": BR.structure_choices()}


@router.post("/briefs/check")
def check_brief(body: BriefCheckIn) -> dict[str, Any]:
    """Warnings about the typed text (never an error): words the design cannot use, must-include lines that are too long, an unknown
    structure. ``ok`` is always true for a brief that can be sent."""
    banned = default_ctx().banned
    notes: list[dict[str, str]] = []
    for where, text in [("brief", body.brief), *((f"must_include {i + 1}", t) for i, t in enumerate(body.must_include))]:
        for pr in freetext.free_text_problems(text, None, banned, strict=False):
            if pr.kind == "banned":
                notes.append({"field": where, "text": f"The design cannot use: {pr.detail}. The planner will leave it out."})
    for i, line in enumerate(body.must_include):
        if len(line.split()) > MUST_INCLUDE_MAX_WORDS:
            notes.append({"field": f"must_include {i + 1}", "text": f"This line is longer than {MUST_INCLUDE_MAX_WORDS} words. Shorten it."})
    if not BR.valid_structure_request(body.structure_request):
        notes.append({"field": "structure_request", "text": "That pair structure is not one of the choices."})
    return {"ok": not any(n["field"] == "structure_request" for n in notes), "notes": notes, "combo": BR.combo_words(body.combo)}


@router.get("/projects/{project_id}/plan/estimate")
def plan_estimate(project_id: str, rt: Runtime = RT) -> dict[str, Any]:
    """The estimate shown beside the "Make plans" button (a range; the cost bar shows the real spend)."""
    return BR.estimate_plan_loop(rt, rt.repo.get_project(project_id))
