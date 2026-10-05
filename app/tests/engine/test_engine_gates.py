"""APP_SPEC §9.1, §9.5, §9.7, §9.9: gate lifecycle, idempotency, 409s, warning release, provisional approvals, approval stamps."""
from __future__ import annotations

import pytest
from helpers_f import make_project, new_job

from duoskin.db.errors import ConflictError
from duoskin.engine import deps
from duoskin.engine.gates import ApplyResult, GateError, allowed_actions_for
from duoskin.models.common import new_id, sha256_of, utcnow
from duoskin.models.gate import Gate, GateAction, GateDecisionIn, GateKind, GateTile, TileState
from duoskin.models.part import Part, PartKind, PartState
from duoskin.models.spec_record import SpecRecord

A = GateAction
SPEC = {"combo": "bg", "a": {"top": {"recipe_id": "tee", "fabric_id": "cotton"}, "hair": {"kit_style_id": "bob"}},
        "b": {"top": {"recipe_id": "hoodie"}}, "world": {"detail_level": "standard"}}


def make_part(rt, project, part_id="a.shirt", kind=PartKind.SHIRT, **kw):
    sha = sha256_of({"board": part_id})
    from duoskin.engine.cas import make_prov

    asset = rt.cas.put(f"board-{part_id}".encode(), "txt", prov=make_prov("code"))
    part = Part(id=part_id, project_id=project.id, character=part_id[0], kind=kind, label=part_id,
                deps=deps.default_dep_rules(kind.value, part_id[0]), state=PartState.READY,
                board_assets={"flat": asset.sha256}, **kw)
    del sha
    return rt.repo.save_part(part)


def make_spec(rt, project):
    rec = SpecRecord(id=new_id("spc"), project_id=project.id, plan_set_id="ps1", plan_index=0, spec=SPEC, sha256=sha256_of(SPEC))
    rt.repo.add_spec(rec)
    rt.repo.mutate_project(project.id, lambda p: setattr(p, "approved_spec_id", rec.id))
    return rec


def open_board(rt, project, part_ids=("a.shirt", "a.hair"), warnings=None, with_step=False):
    tiles = []
    for pid in part_ids:
        kind = PartKind.SHIRT if pid.endswith("shirt") else PartKind.HAIR
        facts = {"warnings": warnings} if warnings and pid == part_ids[0] else {}
        tiles.append(GateTile(tile_id=pid, part_id=pid, label=pid, state=TileState.READY, facts=facts,
                              allowed_actions=allowed_actions_for(GateKind.PART_BOARD, kind, can_manual=True)))
    job = new_job(rt, project.id)
    return rt.gates.open_gate(Gate(id="", project_id=project.id, job_id=job.id, kind=GateKind.PART_BOARD, tiles=tiles,
                                   opened_at=utcnow()))


def decision(tile, action=A.APPROVE, version=0, cid=None, **kw):
    return GateDecisionIn(tile_id=tile, action=action, expected_version=version, client_decision_id=cid or new_id("cid"), **kw)


@pytest.fixture
def board(rt):
    project = make_project(rt)
    spec = make_spec(rt, project)
    shirt, hair = make_part(rt, project), make_part(rt, project, "a.hair", PartKind.HAIR)
    gate = open_board(rt, project)
    return rt, project, spec, gate, shirt, hair


def test_approving_a_part_tile_stamps_the_gate2_approval_hash(board):
    rt, project, spec, gate, shirt, _ = board
    out = rt.gates.decide(gate.id, decision("a.shirt"))
    approval = out.decision.approval
    part = rt.repo.get_part(project.id, "a.shirt")
    assert part.state == PartState.APPROVED and part.approval == approval
    assert approval.approval_hash == deps.approval_hash(shirt, SPEC, None, deps.collect_facts(rt.repo, shirt))
    assert approval.spec_id == spec.id and approval.output_shas == sorted(shirt.board_assets.values())
    assert part.build_stamp is None and rt.repo.valid_build_stamp(project.id, "a.shirt") is None   # the second stamp does not exist yet
    stored = rt.repo.valid_approval(project.id, "a.shirt")
    assert stored.approval_hash == approval.approval_hash
    g = rt.repo.get_gate(gate.id)
    assert g.tiles[0].state == TileState.APPROVED and g.tiles[0].version == 1 and g.tiles[1].version == 0
    assert g.state == "open"                                             # one tile left


def test_gate_is_decided_when_every_tile_is_approved_and_its_step_completes(rt):
    project = make_project(rt)
    make_part(rt, project), make_part(rt, project, "a.hair", PartKind.HAIR)
    from duoskin.engine.registry import register_handler

    def handler(ctx, p, i):
        tiles = [GateTile(tile_id=pid, part_id=pid, label=pid, allowed_actions=allowed_actions_for(GateKind.PART_BOARD, "shirt"))
                 for pid in ("a.shirt", "a.hair")]
        ctx.open_gate(Gate(id="", project_id=project.id, job_id="", kind=GateKind.PART_BOARD, tiles=tiles, opened_at=utcnow()))

    register_handler("t.board", handler)
    rt.scheduler.start()
    job = new_job(rt, project.id)
    from helpers_f import add_step, wait_state

    from duoskin.models.job import StepState

    s = add_step(rt, job, "t.board")
    parked = wait_state(rt, s, StepState.WAITING_USER)
    gid = parked.gate_id
    rt.gates.decide(gid, decision("a.shirt"))
    assert rt.repo.get_step(s.id).state == StepState.WAITING_USER
    rt.gates.decide(gid, decision("a.hair"))
    assert rt.repo.get_gate(gid).state == "decided" and rt.repo.get_gate(gid).decided_at is not None
    wait_state(rt, s, StepState.SUCCEEDED)


def test_decisions_are_idempotent_by_client_decision_id(board):
    rt, _project, _spec, gate, *_ = board
    first = rt.gates.decide(gate.id, decision("a.shirt", cid="dbl-click"))
    again = rt.gates.decide(gate.id, decision("a.shirt", cid="dbl-click"))
    assert again.replay is True and again.decision.id == first.decision.id
    assert len(rt.repo.list_decisions(gate.id)) == 1
    assert rt.repo.get_gate(gate.id).tiles[0].version == 1               # applied once


def test_stale_version_conflicts_with_the_current_gate_attached(board):
    rt, _project, _spec, gate, *_ = board
    rt.gates.decide(gate.id, decision("a.shirt", A.CHANGE, text="shorter sleeves"))
    with pytest.raises(ConflictError) as ei:
        rt.gates.decide(gate.id, decision("a.shirt", A.APPROVE, version=0))
    assert ei.value.code == "version_conflict" and ei.value.current.tiles[0].version == 1


def test_closed_gate_unknown_tile_and_disallowed_action_are_refused(board):
    rt, _project, _spec, gate, *_ = board
    with pytest.raises(GateError) as e1:
        rt.gates.decide(gate.id, decision("nope"))
    assert e1.value.status == 404
    with pytest.raises(GateError) as e2:
        rt.gates.decide(gate.id, decision("a.shirt", A.PICK))
    assert e2.value.code == "action_not_allowed"
    rt.gates.close_gate(gate.id)
    with pytest.raises(ConflictError) as e3:
        rt.gates.decide(gate.id, decision("a.shirt"))
    assert e3.value.code == "gate_closed"


def test_soft_warnings_are_withheld_until_the_first_choice_then_at_most_two_are_released(rt):
    project = make_project(rt)
    make_part(rt, project), make_part(rt, project, "a.hair", PartKind.HAIR)
    warnings = [{"id": "w_low", "text": "x", "severity": "low", "catch_rate": 0.1},
                {"id": "w_catch", "text": "y", "severity": "low", "catch_rate": 0.9},
                {"id": "w_sev", "text": "z", "severity": "high", "catch_rate": 0.5},
                {"id": "w_hidden", "text": "h", "severity": "high", "catch_rate": 1.0, "visible": False}]
    gate = open_board(rt, project, warnings=warnings)
    assert "warnings" not in rt.gates.view(gate.id).tiles[0].facts                  # withheld before any choice
    out = rt.gates.decide(gate.id, decision("a.hair", A.CHANGE, text="rounder"))     # first choice: not an approval
    ids = [w["id"] for w in out.released_warnings]
    assert ids == ["w_catch", "w_sev"] and out.decision.warnings_shown == ids         # visible -> catch rate -> severity; <= 2
    assert out.decision.provisional is False
    visible = rt.gates.view(gate.id).tiles[0].facts["warnings"]
    assert [w["id"] for w in visible] == ["w_catch", "w_sev"]                         # only the released ones
    later = rt.gates.decide(gate.id, decision("a.shirt"))                             # later decisions release nothing new
    assert later.released_warnings == [] and later.decision.provisional is False


def test_an_approval_with_released_warnings_is_provisional_until_confirmed(rt):
    project = make_project(rt)
    make_spec(rt, project)
    make_part(rt, project), make_part(rt, project, "a.hair", PartKind.HAIR)
    gate = open_board(rt, project, warnings=[{"id": "w1", "text": "x", "catch_rate": 0.5}, {"id": "w2", "text": "y"}])
    out = rt.gates.decide(gate.id, decision("a.shirt", A.APPROVE))
    assert out.decision.provisional is True and {w["id"] for w in out.released_warnings} == {"w1", "w2"}
    # follow-up steps do not start: the part is not approved and the tile is unchanged
    assert rt.repo.get_part(project.id, "a.shirt").state == PartState.READY
    assert rt.repo.get_gate(gate.id).tiles[0].state == TileState.READY and rt.repo.get_gate(gate.id).tiles[0].version == 0
    with pytest.raises(ConflictError) as ei:
        rt.gates.decide(gate.id, decision("a.shirt", A.REIMAGINE))
    assert ei.value.code == "provisional_pending"
    with pytest.raises(GateError):
        rt.gates.confirm(gate.id, out.decision.id, ["w_unknown"])
    final = rt.gates.confirm(gate.id, out.decision.id, ["w1"])
    assert final.provisional is False and final.warnings_overridden == ["w1"] and final.approval is not None
    assert rt.repo.get_part(project.id, "a.shirt").state == PartState.APPROVED
    labels = rt.db.conn().execute("SELECT kind, source, json FROM labels").fetchall()
    assert [(r["kind"], r["source"]) for r in labels] == [("warning_override", "gate")] and "w1" in labels[0]["json"]
    with pytest.raises(ConflictError):
        rt.gates.confirm(gate.id, out.decision.id, [])                                 # already final


def test_go_back_deletes_the_provisional_decision_and_the_tile_stays_ready(rt):
    project = make_project(rt)
    make_part(rt, project), make_part(rt, project, "a.hair", PartKind.HAIR)
    gate = open_board(rt, project, warnings=[{"id": "w1", "text": "x"}])
    out = rt.gates.decide(gate.id, decision("a.shirt", A.APPROVE))
    rt.gates.withdraw(gate.id, out.decision.id)
    assert rt.repo.list_decisions(gate.id) == [] and rt.repo.find_decision(out.decision.id) is None
    assert rt.repo.get_gate(gate.id).tiles[0].state == TileState.READY
    again = rt.gates.decide(gate.id, decision("a.shirt", A.CHANGE, text="x"))        # the user can decide again, version unchanged
    assert again.decision.provisional is False
    with pytest.raises(ConflictError):
        rt.gates.withdraw(gate.id, again.decision.id)                                   # only provisional decisions can go back


def test_approve_all_approves_only_ready_tiles_without_hard_failures(rt):
    project = make_project(rt)
    for pid, kind in (("a.shirt", PartKind.SHIRT), ("a.pants", PartKind.PANTS), ("a.hair", PartKind.HAIR)):
        make_part(rt, project, pid, kind)
    tiles = [GateTile(tile_id="a.shirt", part_id="a.shirt", label="s", state=TileState.READY,
                      allowed_actions=allowed_actions_for(GateKind.PART_BOARD, "shirt")),
             GateTile(tile_id="a.pants", part_id="a.pants", label="p", state=TileState.READY, facts={"hard_failures": ["A_ALPHA"]},
                      allowed_actions=allowed_actions_for(GateKind.PART_BOARD, "pants")),
             GateTile(tile_id="a.hair", part_id="a.hair", label="h", state=TileState.GENERATING,
                      allowed_actions=allowed_actions_for(GateKind.PART_BOARD, "hair"))]
    gate = rt.gates.open_gate(Gate(id="", project_id=project.id, job_id="j", kind=GateKind.PART_BOARD, tiles=tiles, opened_at=utcnow()))
    rt.gates.decide(gate.id, decision("a.shirt", A.APPROVE_ALL))
    g = rt.repo.get_gate(gate.id)
    assert [t.state for t in g.tiles] == [TileState.APPROVED, TileState.READY, TileState.GENERATING]
    assert rt.repo.get_part(project.id, "a.shirt").state == PartState.APPROVED
    assert rt.repo.get_part(project.id, "a.pants").state == PartState.READY and g.state == "open"


def test_registered_applier_runs_inside_the_decision_and_failures_roll_everything_back(board):
    rt, _project, _spec, gate, *_ = board
    spawned = []

    def applier(ac):
        spawned.append(ac.decision.action)
        if ac.decision.text == "explode":
            raise RuntimeError("applier bug")
        ac.tile.state = TileState.STALE
        return ApplyResult(spawned_step_ids=["stp_x"], close_gate=False)

    rt.gates.register_applier(GateKind.PART_BOARD, applier)
    with pytest.raises(RuntimeError):
        rt.gates.decide(gate.id, decision("a.shirt", A.CHANGE, text="explode", cid="boom"))
    assert rt.repo.list_decisions(gate.id) == [] and rt.repo.get_gate(gate.id).tiles[0].version == 0
    out = rt.gates.decide(gate.id, decision("a.shirt", A.CHANGE, text="fine"))
    assert out.decision.spawned_step_ids == ["stp_x"]
    assert rt.repo.get_gate(gate.id).tiles[0].state == TileState.STALE


def test_gate3_pick_confirms_every_build_stamp(rt):
    project = make_project(rt)
    make_spec(rt, project)
    make_part(rt, project)
    gate = open_board(rt, project, part_ids=("a.shirt",))
    rt.gates.decide(gate.id, decision("a.shirt"))
    rt.repo.mutate_part(project.id, "a.shirt", lambda p: p.build_assets.update({"texture": "e" * 64}))
    stamped = deps.stamp_build(rt.repo, project.id, "a.shirt")
    assert stamped.state == PartState.BUILT and stamped.build_stamp.build_hash and stamped.build_stamp.confirmed_decision_id is None
    tile = GateTile(tile_id="cand1", label="Duo 1", allowed_actions=allowed_actions_for(GateKind.FINAL_PICK))
    g3 = rt.gates.open_gate(Gate(id="", project_id=project.id, job_id="j", kind=GateKind.FINAL_PICK, tiles=[tile], opened_at=utcnow()))
    out = rt.gates.decide(g3.id, decision("cand1", A.PICK))
    confirmed = rt.repo.get_part(project.id, "a.shirt")
    assert confirmed.build_stamp.confirmed_decision_id == out.decision.id
    assert rt.repo.valid_build_stamp(project.id, "a.shirt").confirmed_decision_id == out.decision.id
    assert confirmed.approval.approval_hash == stamped.approval.approval_hash                     # the pick never touches the Gate 2 stamp
    assert rt.repo.get_gate(g3.id).state == "open"                                    # pick + export decide Gate 3
    rt.gates.decide(g3.id, decision("cand1", A.EXPORT, version=1))
    assert rt.repo.get_gate(g3.id).state == "decided"


@pytest.mark.parametrize("kind,part_kind,expect,absent", [
    (GateKind.CONCEPT, None, {A.APPROVE, A.REIMAGINE, A.CHANGE, A.NEW_PLAN, A.SELECT_ALTERNATIVE}, {A.PICK}),
    (GateKind.PART_BOARD, "hair", {A.APPROVE, A.REIMAGINE, A.CHANGE, A.MAKE_MANUAL, A.BACK_TO_CONCEPT, A.APPROVE_ALL}, {A.PICK}),
    (GateKind.PART_BOARD, "face", {A.APPROVE, A.REIMAGINE, A.CHANGE, A.SELECT_ALTERNATIVE}, {A.MAKE_MANUAL}),
    (GateKind.PART_BOARD, "colours", {A.APPROVE, A.CHANGE}, {A.REIMAGINE}),
    (GateKind.PART_BOARD, "shirt", {A.APPROVE, A.REIMAGINE, A.CHANGE}, {A.MAKE_MANUAL, A.SELECT_ALTERNATIVE}),
    (GateKind.FINAL_PICK, None, {A.PICK, A.CHANGE, A.EXPORT}, {A.APPROVE}),
    (GateKind.BUDGET, None, {A.CONTINUE, A.RAISE_CAP, A.STOP}, {A.APPROVE}),
    (GateKind.CHANGE_CONFIRM, None, {A.CONFIRM, A.CANCEL}, {A.APPROVE}),
])
def test_tile_action_sets_follow_the_9_5_table(kind, part_kind, expect, absent):
    got = set(allowed_actions_for(kind, part_kind, can_manual=True))
    assert expect <= got and not (absent & got)
    assert A.OVERRIDE_WARNING not in got and "reject" not in {a.value for a in GateAction}   # S8: there is no reject


def test_reannounce_and_close(rt):
    gate = rt.gates.open_gate(Gate(id="", project_id="", job_id="j", kind=GateKind.MANUAL_IMPORT, opened_at=utcnow(),
                                   tiles=[GateTile(tile_id="t", label="pack", allowed_actions=allowed_actions_for(GateKind.MANUAL_IMPORT))]))
    assert rt.gates.reannounce_open_gates() == 1
    assert rt.gates.close_gate(gate.id, "superseded").state == "superseded"
    assert rt.gates.reannounce_open_gates() == 0
