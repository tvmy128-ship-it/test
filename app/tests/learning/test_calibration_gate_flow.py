"""APP_SPEC §3.1 / §9.9 through the real gate service: every real choice is a label, flags and catches are counted, overrides feed the auto-hide,
and the warnings a gate releases are ranked by their catch rate on rejected duos."""
from __future__ import annotations

from lfix import make_project

from duoskin.engine import calibration as cal
from duoskin.engine.gates import allowed_actions_for
from duoskin.models.common import new_id, utcnow
from duoskin.models.gate import Gate, GateAction, GateDecisionIn, GateKind, GateTile, TileState
from duoskin.models.job import JobKind

A = GateAction


def open_gate(rt, project, tiles, kind=GateKind.PART_BOARD):
    job = rt.scheduler.submit_job(JobKind.PARTS, project.id, {})
    gate = Gate(id="", project_id=project.id, job_id=job.id, kind=kind, opened_at=utcnow(), tiles=[
        GateTile(tile_id=tid, part_id=None, label=tid, state=TileState.READY, facts=facts, allowed_actions=allowed_actions_for(kind, "shirt"))
        for tid, facts in tiles])
    return rt.gates.open_gate(gate)


def decide(rt, gate, tile, action, **kw):
    return rt.gates.decide(gate.id, GateDecisionIn(tile_id=tile, action=action, expected_version=kw.pop("version", 0), client_decision_id=new_id("cid"), **kw))


def warning(wid, **kw):
    return {"id": wid, "text": "x", "severity": "low", "catch_rate": 0.0, "visible": True, "fresh": True, **kw}


def labels(rt, kind=None):
    return [(lab.kind, lab.source, lab.value) for lab in cal.list_labels(rt, kind=kind)]


def test_an_approval_is_a_label_and_counts_the_checks_that_flagged_the_tile(rt):
    p = make_project(rt)
    gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO"), warning("a.shirt:F_BLUSH", visible=False)],
                                         "hard_failures": [{"id": "A_ALPHA"}]}), ("a.pants", {})])
    out = decide(rt, gate, "a.pants", A.APPROVE)                       # the first choice of the gate releases the warnings of any tile
    assert out.decision.provisional and [w["id"] for w in out.released_warnings] == ["a.shirt:TASTE_RATIO"]
    assert labels(rt, "like_dislike") == [], "nothing is final yet: the person may still go back"
    rt.gates.confirm(gate.id, out.decision.id, [])                     # "approve anyway" without overriding the warning
    (kind, source, value), = labels(rt, "like_dislike")
    assert (kind, source) == ("like_dislike", "gate") and value["liked"] is True and value["gate"] == "part_board" and value["action"] == "approve"
    t = cal.totals_for(rt, "A_ALPHA")
    assert t["approved_total"] == 1 and t["flagged_approved"] == 0, "the hard failure was on the other tile"
    shown = cal.totals_for(rt, "TASTE_RATIO")
    assert shown["shown"] == 1 and shown["overridden"] == 0, "shown and not overridden: the person approved a different tile"


def test_the_flags_of_the_decided_tile_are_counted_even_when_the_warning_was_hidden(rt):
    p = make_project(rt)
    cal.record_showings(rt, [("F_BLUSH", True)] * 10)                   # F_BLUSH is auto-hidden already
    gate = open_gate(rt, p, [("a.face", {"warnings": [warning("a.face:F_BLUSH")], "hard_failures": [{"id": "A_ALPHA"}, "A_HALO"]})])
    out = decide(rt, gate, "a.face", A.APPROVE)
    assert out.released_warnings == [], "an auto-hidden warning is never released"
    assert out.decision.provisional is False
    for cid in ("F_BLUSH", "A_ALPHA", "A_HALO"):
        t = cal.totals_for(rt, cid)
        assert t["flagged_approved"] == 1 and t["approved_total"] == 1, cid


def test_a_rejection_counts_as_a_catch_and_a_change_counts_as_nothing(rt):
    p = make_project(rt)
    gate = open_gate(rt, p, [("a.shirt", {"hard_failures": [{"id": "A_PALETTE"}]}), ("a.pants", {"hard_failures": [{"id": "A_PALETTE"}]})])
    decide(rt, gate, "a.shirt", A.REIMAGINE)
    decide(rt, gate, "a.pants", A.CHANGE, text="shorter", version=0)
    t = cal.totals_for(rt, "A_PALETTE")
    assert t["flagged_rejected"] == 1 and t["rejected_total"] == 1 and t["approved_total"] == 0
    assert [lab[2]["liked"] for lab in labels(rt, "like_dislike")] == [False], "only the reimagine is a like/dislike label"


def test_approve_all_counts_every_ready_tile_without_a_hard_failure(rt):
    p = make_project(rt)
    gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO")]}), ("a.pants", {}), ("a.hair", {"hard_failures": [{"id": "A_ALPHA"}]})])
    out = decide(rt, gate, "a.pants", A.APPROVE_ALL)
    if out.decision.provisional:
        rt.gates.confirm(gate.id, out.decision.id, [])
    assert cal.totals_for(rt, "TASTE_RATIO")["flagged_approved"] == 1
    assert cal.totals_for(rt, "A_ALPHA")["flagged_approved"] == 0, "approve_all leaves tiles with a hard failure alone"
    assert cal.totals_for(rt, "TASTE_RATIO")["approved_total"] == 2


def test_overriding_a_warning_is_logged_counted_and_feeds_the_auto_hide(rt):
    p = make_project(rt)
    for _ in range(8):                                                  # calib.warning_min_showings = 8: the 8th override tips it over 25%
        gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO")]})])
        out = decide(rt, gate, "a.shirt", A.APPROVE)
        assert out.decision.provisional and [w["id"] for w in out.released_warnings] == ["a.shirt:TASTE_RATIO"]
        rt.gates.confirm(gate.id, out.decision.id, ["a.shirt:TASTE_RATIO"])                 # "approve anyway"
    assert len(labels(rt, "warning_override")) == 8
    t = cal.totals_for(rt, "TASTE_RATIO")
    assert t["shown"] == 8 and t["overridden"] == 8 and t["flagged_approved"] == 8
    v = cal.warning_visibility("TASTE_RATIO", rt)
    assert not v.visible and v.overridden == 8
    # the next gate no longer releases it, so the approval goes straight through
    gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO")]})])
    out = decide(rt, gate, "a.shirt", A.APPROVE)
    assert out.released_warnings == [] and out.decision.provisional is False
    assert rt.gates.view(gate.id).tiles[0].facts.get("warnings", []) == []


def test_going_back_means_the_warning_was_heeded(rt):
    p = make_project(rt)
    gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO")]})])
    out = decide(rt, gate, "a.shirt", A.APPROVE)
    rt.gates.withdraw(gate.id, out.decision.id)
    t = cal.totals_for(rt, "TASTE_RATIO")
    assert t["shown"] == 1 and t["overridden"] == 0
    assert labels(rt, "warning_override") == [] and labels(rt, "like_dislike") == []


def test_a_first_choice_that_throws_the_tile_away_heeds_the_warnings_it_released(rt):
    p = make_project(rt)
    gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO")]})])
    out = decide(rt, gate, "a.shirt", A.REIMAGINE)
    assert out.decision.provisional is False and len(out.released_warnings) == 1
    t = cal.totals_for(rt, "TASTE_RATIO")
    assert t["shown"] == 1 and t["overridden"] == 0 and t["flagged_rejected"] == 1


def test_the_two_warnings_a_gate_releases_are_the_ones_with_the_highest_catch_rate_on_rejected_duos(rt):
    p = make_project(rt)
    for i in range(10):                                                   # TASTE_LAYOUT flagged 8 of 10 rejected tiles, TASTE_RATIO only 1, F_BLUSH none
        cal.record_gate_outcome(rt, "part_board", "rejected", ["TASTE_LAYOUT"] if i < 8 else (["TASTE_RATIO"] if i == 8 else []))
    gate = open_gate(rt, p, [("a.shirt", {"warnings": [warning("a.shirt:TASTE_RATIO"), warning("a.shirt:F_BLUSH"), warning("a.shirt:TASTE_LAYOUT")]})])
    out = decide(rt, gate, "a.shirt", A.APPROVE)
    ids = [w["id"] for w in out.released_warnings]
    assert ids == ["a.shirt:TASTE_LAYOUT", "a.shirt:TASTE_RATIO"], "at most two, the best catchers first; F_BLUSH never caught anything"
    assert out.released_warnings[0]["catch_rate"] == 0.8 and out.released_warnings[1]["catch_rate"] == 0.1


def test_a_replayed_decision_is_not_counted_twice(rt):
    p = make_project(rt)
    gate = open_gate(rt, p, [("a.shirt", {})])
    body = GateDecisionIn(tile_id="a.shirt", action=A.REIMAGINE, expected_version=0, client_decision_id=new_id("cid"))
    rt.gates.decide(gate.id, body)
    assert rt.gates.decide(gate.id, body).replay is True
    assert sum(r.rejected_total for r in cal.stat_rows(rt, "*gate:part_board")) == 1 and len(labels(rt, "like_dislike")) == 1


def test_gates_that_are_not_a_design_choice_are_not_learning_material(rt):
    from types import SimpleNamespace

    snap = cal.DecisionSnapshot("gat1", "budget", "prj1", ["step1"], {"step1": []}, "approved")
    cal.on_decision(rt, snap, SimpleNamespace(warnings_shown=[], warnings_overridden=[], id="dec1", tile_id="step1", action=A.CONTINUE))
    assert labels(rt) == [] and cal.stat_rows(rt) == []
