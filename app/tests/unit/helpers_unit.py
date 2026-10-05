"""Small builders shared by the foundation unit tests (imported by basename)."""
from __future__ import annotations

from duoskin.engine import deps
from duoskin.models.common import new_id, utcnow
from duoskin.models.part import Part, PartKind, PartState
from duoskin.models.project import Project, ProjectSettings, Stage


def make_project(rt, name="Test Duo", **settings):
    now = utcnow()
    p = Project(id=new_id("prj"), name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo="bg", brief="",
                stage=Stage.BRIEF, settings=ProjectSettings(**settings))
    return rt.repo.create_project(p)


def make_part(rt, project, part_id="a.shirt", kind=PartKind.SHIRT, board=None, **kw):
    character = kw.pop("character", "duo" if part_id == "duo" else part_id[0])
    kw.setdefault("state", PartState.READY)
    return Part(id=part_id, project_id=project.id, character=character, kind=kind, label=part_id,
                deps=deps.default_dep_rules(kind.value, character), board_assets=board or {}, **kw)
