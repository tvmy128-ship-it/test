"""Specs and change requests (APP_SPEC §13): one spec with its DNA card, and the state of a "Change..." request.

``GET /api/projects/{id}/specs`` and ``GET /api/specs/{id}/diff/{other}`` belong to the foundation (``api/projects.py``). This module adds
``GET /api/specs/{spec_id}`` and ``GET /api/changes/{change_id}``, which the confirm dialog and the change follower of the web app poll.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from duoskin.api import RT
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


@router.get("/specs/{spec_id}")
def get_spec(spec_id: str, rt: Runtime = RT) -> dict[str, Any]:
    """One spec record with its DNA card and the results of the plan linter."""
    rec = rt.repo.get_spec(spec_id)
    return {"spec": rec.model_dump(mode="json"), "dna_card": rt.repo.get_dna_card(rec.id),
            "lint": [r.model_dump(mode="json") for r in rec.lint]}


@router.get("/changes/{change_id}")
def get_change(change_id: str, rt: Runtime = RT) -> dict[str, Any]:
    """A change request: its status, what L7 understood, the diff (spec and DNA-card changes), the parts it would redo (``invalidation``), the
    estimate and, for a rejected change, ``plan.reason`` (why nothing was changed). The new spec itself is not sent: it is applied on confirm."""
    cr = rt.repo.get_change(change_id)
    plan = dict(cr.plan or {})
    plan.pop("new_spec", None)
    out = cr.model_dump(mode="json")
    out["plan"] = plan
    out["diff"] = plan.get("diff") or {"spec_changes": [], "dna_changes": []}
    out["understood_as"] = plan.get("understood_as", "")
    out["question"] = plan.get("question", "")
    out["answers"] = list(plan.get("answers", []))
    out["target"] = plan.get("target", "both")
    out["origin"] = plan.get("origin", {})
    return out


@router.get("/projects/{project_id}/changes")
def list_changes(project_id: str, rt: Runtime = RT) -> list[dict[str, Any]]:
    """The change requests of a project, newest first (for the history of a duo)."""
    rt.repo.get_project(project_id)
    rows = rt.db.conn().execute("SELECT id FROM changes WHERE project_id=? ORDER BY created_at DESC, rowid DESC", (project_id,)).fetchall()
    return [get_change(r["id"], rt) for r in rows]
