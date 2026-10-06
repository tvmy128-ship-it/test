"""A 3D model that was made by hand before its tile was approved ends at ``build.finish`` with no approval to stamp a build on.

The part waits for the person's OK (STALE: "approve again"). The board has to say so: the tile was never refreshed, so it kept saying "Waiting for the
3D file you make on Tripo" although the file was in and had passed the checks (found by clicking the whole app through, tests/e2e_ui).
"""
from __future__ import annotations

from types import SimpleNamespace

from pfix import make_project

from duoskin.models.common import new_id, utcnow
from duoskin.models.gate import Gate, GateKind
from duoskin.models.job import Job, JobKind
from duoskin.models.part import PartState
from duoskin.pipeline import build, parts


def test_a_manual_model_for_an_unapproved_tile_turns_the_board_tile_into_a_part_that_waits_for_the_ok(rt):
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    job = rt.repo.insert_job(Job(id=new_id("job"), project_id=p.id, kind=JobKind.PARTS, created_at=utcnow()))
    part = rt.repo.get_part(p.id, "a.acc.0")
    gate = rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=job.id, kind=GateKind.PART_BOARD, tiles=[parts.tile_for(rt, p.id, part, version=0)], opened_at=utcnow()))
    parts.set_part_state(rt, p.id, "a.acc.0", PartState.WAITING_MANUAL, flags_add=["waiting_manual"])
    parts.refresh_tile(rt, p.id, "a.acc.0")
    assert rt.repo.get_gate(gate.id).tiles[0].state.value == "waiting_manual"

    result = build.run_finish(SimpleNamespace(rt=rt, step=SimpleNamespace(project_id=p.id)), build.FinishParams(part_id="a.acc.0", from_step=""), [])

    assert result.message == "the approval is gone: approve the tile again"
    after = rt.repo.get_part(p.id, "a.acc.0")
    assert after.state == PartState.STALE and "waiting_manual" not in after.flags
    tile = rt.repo.get_gate(gate.id).tiles[0]
    assert tile.state.value == "stale", "the board tile says what the part says"
    assert "approve" in tile.facts["report"].lower() and "approve" in tile.allowed_actions
