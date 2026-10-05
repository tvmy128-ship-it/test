"""The plugin hook (``pipeline.register``) wires this track's handlers, job factory and gate appliers; ``plan.start_parts`` is the hand-off to the
part board that the lane of the part board can call or register."""
from __future__ import annotations

import pytest
from planhelpers import db_project

from duoskin.engine import registry as eng_registry
from duoskin.engine import scheduler as sched
from duoskin.models.gate import GateKind
from duoskin.models.job import JobKind
from duoskin.models.project import Stage
from duoskin.pipeline import plan as PL

HANDLERS = ("plan.reference", "plan.taste", "plan.planner", "plan.lint", "plan.critic", "plan.pairwise", "plan.revise", "plan.select",
            "concept.char", "concept.assemble", "concept.gate", "concept.lock", "change.interpret")


def test_register_wires_handlers_the_plan_job_factory_and_the_gate_appliers(app, rt):
    kinds = set(eng_registry.registered_kinds())
    assert set(HANDLERS) <= kinds
    assert sched.has_job_factory(JobKind.PLAN)
    appliers = rt.gates._appliers
    for kind in (GateKind.CONCEPT, GateKind.CLARIFY, GateKind.CHANGE_CONFIRM):
        assert kind.value in appliers, kind


def test_the_paid_handlers_declare_their_provider_and_the_free_ones_do_not(app):
    for kind, paid, provider in (("plan.planner", True, "anthropic"), ("plan.critic", True, "anthropic"), ("plan.lint", False, None),
                                 ("plan.select", False, None), ("concept.char", True, "openai"), ("concept.gate", False, None),
                                 ("change.interpret", True, "anthropic")):
        h = eng_registry.get(kind)
        assert h.paid is paid and h.provider == provider, kind
        assert h.cacheable is False, "plan steps are never served from the engine's step cache: their paid calls are cached by content inside"


def test_register_can_be_called_twice_and_without_a_runtime(app):
    from duoskin.pipeline import change, concept

    PL.register()
    concept.register()
    change.register()
    assert eng_registry.get("plan.planner") is not None


@pytest.fixture
def no_parts_lane(monkeypatch):
    """No PARTS job factory and no starter: only the stage can change (the lane registers itself when it exists)."""
    monkeypatch.delitem(sched._factories, JobKind.PARTS.value, raising=False)
    monkeypatch.setattr(PL, "_PARTS_STARTERS", [])


def stages(rt, pid):
    import json

    return [json.loads(r[0]).get("stage") for r in rt.db.conn().execute(
        "select payload from events where type='project.stage' and project_id=? order by id", (pid,)).fetchall()]


def test_start_parts_advances_the_stage_and_emits_the_event(unit_rt, no_parts_lane):
    p = db_project(unit_rt, stage=Stage.GATE1)
    assert PL.start_parts(p.id, unit_rt) is None
    assert unit_rt.repo.get_project(p.id).stage == Stage.PARTS
    assert stages(unit_rt, p.id)[-1] == "parts"


def test_a_registered_starter_builds_the_parts_job(unit_rt, no_parts_lane):
    p = db_project(unit_rt, stage=Stage.GATE1)
    calls = []

    def starter(rt, project_id):
        calls.append(project_id)
        return rt.scheduler.submit_job(JobKind.PARTS, project_id, {"from": "starter"}, steps=[])

    PL.register_parts_starter(starter)
    PL.register_parts_starter(starter)                  # registering twice does not run it twice
    job = PL.start_parts(p.id, unit_rt)
    assert calls == [p.id] and job.kind == JobKind.PARTS and job.params == {"from": "starter"}
    assert PL.start_parts(p.id, unit_rt).id == job.id and calls == [p.id], "a running PARTS job is returned, not a second one"


def test_a_starter_that_has_nothing_to_start_falls_through(unit_rt, no_parts_lane):
    p = db_project(unit_rt, stage=Stage.GATE1)
    PL.register_parts_starter(lambda rt, pid: None)
    assert PL.start_parts(p.id, unit_rt) is None


def test_the_stage_is_not_pushed_back_when_the_board_is_already_open(unit_rt, no_parts_lane):
    p = db_project(unit_rt, stage=Stage.GATE2)
    PL.start_parts(p.id, unit_rt)
    assert unit_rt.repo.get_project(p.id).stage == Stage.GATE2


def test_without_a_starter_the_lane_job_factory_is_used(unit_rt, monkeypatch):
    monkeypatch.setattr(PL, "_PARTS_STARTERS", [])
    seen = []

    def factory(rt, job, project):
        seen.append(job.id)
        return []

    monkeypatch.setitem(sched._factories, JobKind.PARTS.value, factory)
    monkeypatch.setattr("duoskin.pipeline.parts.start_parts_job", None, raising=False)
    p = db_project(unit_rt, stage=Stage.GATE1)
    job = PL.start_parts(p.id, unit_rt)
    assert job is not None and job.kind == JobKind.PARTS and seen == [job.id]
