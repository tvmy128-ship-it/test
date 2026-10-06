"""A sticker slab or a primitive that fails the mesh gate is not sent to a Tripo pack.

Its tile has no four approved pictures, so a pack can never be made: the old last resort left the part behind a "Try again" that failed again
("the tile has no approved views yet"), and the project waited in BUILD for ever (found by running the whole app with faults, tests/e2e_ui).
The person is asked to look at the part instead, as for any other failed build.
"""
from __future__ import annotations

from types import SimpleNamespace

from pfix import give_views, make_project

from duoskin.models.common import new_id, utcnow
from duoskin.models.gate import Gate, GateKind
from duoskin.models.job import Job, JobKind
from duoskin.models.part import PartState
from duoskin.pipeline import build, parts


def setup(rt, with_views: bool):
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    job = rt.repo.insert_job(Job(id=new_id("job"), project_id=p.id, kind=JobKind.BUILD, created_at=utcnow()))
    if with_views:
        give_views(rt, p.id, "a.acc.0")
    part = rt.repo.get_part(p.id, "a.acc.0")
    gate = rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=job.id, kind=GateKind.PART_BOARD, tiles=[parts.tile_for(rt, p.id, part, version=0)], opened_at=utcnow()))
    parts.set_part_state(rt, p.id, "a.acc.0", PartState.BUILDING)
    ctx = SimpleNamespace(rt=rt, step=SimpleNamespace(project_id=p.id, job_id=job.id, priority=100), spawn=lambda steps: None)
    return p, gate, ctx, rt.repo.get_part(p.id, "a.acc.0")


def test_a_slab_that_fails_the_gate_asks_the_person_to_look_instead_of_opening_a_pack_that_cannot_be_made(rt):
    p, gate, ctx, part = setup(rt, with_views=False)
    params = build.MeshStepParams(part_id="a.acc.0", source={"kind": "slab", "judge": False})
    nxt = build.mesh_failed(ctx, part, params, {"files": {}}, reason="CHK-M20: the slab is too thin", mirrored_only=False)
    assert nxt == "needs_help"
    after = rt.repo.get_part(p.id, "a.acc.0")
    assert after.state == PartState.NEEDS_HUMAN and "build_failed" in after.flags
    tile = rt.repo.get_gate(gate.id).tiles[0]
    assert tile.state.value == "needs_human" and "approve it again" in tile.facts["report"]
    assert not [s for j in rt.repo.list_jobs(p.id) for s in rt.repo.list_steps(job_id=j.id) if s.kind == "manual.pack"], "no pack step was made"


def test_a_part_with_its_four_views_still_goes_to_the_tripo_pack(rt):
    _p, _gate, ctx, part = setup(rt, with_views=True)
    params = build.MeshStepParams(part_id="a.acc.0", source={"kind": "manual"})
    spawned: list = []
    ctx.spawn = spawned.extend
    nxt = build.mesh_failed(ctx, part, params, {"files": {}}, reason="CHK-M08: orientation", mirrored_only=False)
    assert nxt == "manual" and [s.kind for s in spawned] == ["manual.pack"]
