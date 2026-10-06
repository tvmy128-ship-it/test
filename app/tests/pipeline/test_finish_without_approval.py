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


def test_a_part_whose_model_was_made_by_hand_is_not_sent_to_tripo_again_when_the_build_starts(rt, monkeypatch):
    """Found by clicking the app through: the model was imported on the part board, passed the checks and waited for the OK; then "Approve all" started the
    build, which asked Tripo for ANOTHER model (and, when that one failed the checks, opened a second drop box for a file that was already in)."""
    monkeypatch.setattr(build, "tripo_ok", lambda _rt: True)
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    job = rt.repo.insert_job(Job(id=new_id("job"), project_id=p.id, kind=JobKind.BUILD, created_at=utcnow()))
    spec = rec.spec if isinstance(rec.spec, dict) else rec.spec.model_dump(mode="json")
    part = rt.repo.get_part(p.id, "a.acc.0")
    assert build.needs_mesh_from_tripo(spec, part, ""), "the accessory of this project is a Tripo part"

    fresh = build.part_chain(rt, job.id, p.id, part, spec, "api")
    assert [s.kind for s in fresh] == ["tripo.model"], "a part without a model is made through the API in API mode"

    build.set_build_assets(rt, p.id, "a.acc.0", {"gltf": "0" * 64, "png": "1" * 64}, replace=True)
    part = rt.repo.get_part(p.id, "a.acc.0")
    for mode in ("api", "manual", "ask"):
        kept = build.part_chain(rt, job.id, p.id, part, spec, mode)
        assert [s.kind for s in kept] == ["build.finish"], f"{mode}: the model that is already in is kept, only the stamp is left"
