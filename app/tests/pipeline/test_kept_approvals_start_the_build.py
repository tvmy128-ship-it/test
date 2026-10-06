"""A part that keeps its approval through a recompute (a change the program redoes for free) has no decision to say "the board is done".

Found by clicking the whole app through (tests/e2e_ui): a change at Gate 3 recoloured a shirt, the shirt kept its approval, every tile of the reopened part
board was approved, and nothing ever started the build: the project waited at the board for ever. Also, a duo that is stale must be made again even when the
rebuilt parts are byte for byte the same (the build hashes repeat, so the "already started" key must not hold it back).
"""
from __future__ import annotations

from pfix import make_project

from duoskin.models.common import new_id, utcnow
from duoskin.models.gate import Gate, GateKind
from duoskin.models.job import Job, JobKind
from duoskin.models.part import PartKind, PartState
from duoskin.models.project import Stage
from duoskin.pipeline import build, parts


def board(rt, monkeypatch, *, states: dict[str, PartState] | None = None):
    started: list[str] = []
    monkeypatch.setattr(build, "start_build", lambda _rt, pid: started.append(pid))
    p, rec = make_project(rt, stage=Stage.GATE2)
    parts.ensure_parts(rt, p.id, rec.spec)
    for part in rt.repo.list_parts(p.id):
        if part.kind != PartKind.DUO:
            parts.set_part_state(rt, p.id, part.id, (states or {}).get(part.id, PartState.APPROVED))
    job = rt.repo.insert_job(Job(id=new_id("job"), project_id=p.id, kind=JobKind.PARTS, created_at=utcnow()))
    tiles = [parts.tile_for(rt, p.id, x, version=0) for x in rt.repo.list_parts(p.id) if x.kind != PartKind.DUO]
    gate = rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=job.id, kind=GateKind.PART_BOARD, tiles=tiles, opened_at=utcnow()))
    return p, gate, started


def test_when_every_part_kept_its_approval_the_board_closes_and_the_build_starts(rt, monkeypatch):
    p, gate, started = board(rt, monkeypatch)
    assert parts.maybe_open_gate2(rt, p.id) is False
    assert rt.repo.get_gate(gate.id).state == "decided", "an open board with every tile approved is a dead end"
    assert started == [p.id]


def test_while_a_part_still_needs_a_look_the_board_stays_open_and_nothing_is_built(rt, monkeypatch):
    p, gate, started = board(rt, monkeypatch, states={"a.shirt": PartState.READY})
    parts.maybe_open_gate2(rt, p.id)
    assert rt.repo.get_gate(gate.id).state == "open" and started == []


def test_the_build_is_not_started_again_once_the_project_is_past_the_board(rt, monkeypatch):
    p, _gate, started = board(rt, monkeypatch)
    rt.repo.set_project_stage(p.id, Stage.BUILDING)
    assert parts.start_build_for_kept_approvals(rt, p.id) is False and started == []


def test_a_stale_duo_is_made_again_even_when_the_rebuilt_parts_have_the_same_hashes(rt, monkeypatch):
    from duoskin.pipeline import duo

    p, _gate, _started = board(rt, monkeypatch)
    starts: list[str] = []
    monkeypatch.setattr(duo, "start_duo", lambda _rt, pid: starts.append(pid))
    for part in rt.repo.list_parts(p.id):
        if part.kind != PartKind.DUO:
            parts.set_part_state(rt, p.id, part.id, PartState.BUILT)
    assert build.maybe_start_duo(rt, p.id) is True and build.maybe_start_duo(rt, p.id) is False, "the duo starts once for one set of builds"
    parts.set_part_state(rt, p.id, "duo", PartState.STALE)
    assert build.maybe_start_duo(rt, p.id) is True and starts == [p.id, p.id]
