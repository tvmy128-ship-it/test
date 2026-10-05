"""The concept ladder in a running app: the figure-boxes mask first (the mock repaints the whole editable area flat, so every draft fails A_SIL_GUIDE),
then the tight mask, which keeps the silhouette (a flat repaint may still fail the colour checks, which is the mock's doing); and a draw that no
rung can save ends as NEEDS_HUMAN instead of crashing."""
from __future__ import annotations

import pytest
from planhelpers import plan_only, shown

from duoskin.engine.testkit import wait_for
from duoskin.models.job import JobKind
from duoskin.pipeline import concept as CO

pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def planned(client, rt):
    pid = plan_only(client, brief="a ladder duo")
    rec = shown(client, pid)[0]
    return pid, rec


def run_char(rt, pid, rec, slot, **kw):
    job = rt.scheduler.submit_job(JobKind.PLAN, pid, {"purpose": "test"}, steps=[])
    step = CO.char_step(rt, job.id, pid, rec["plan_set_id"], slot, rec["id"], "a", **kw)
    rt.scheduler.spawn(job.id, [step])
    wait_for(lambda: rt.repo.get_step(step.id).state.value in ("succeeded", "failed"), timeout=300, message="the concept step")
    st = rt.repo.get_step(step.id)
    assert st.state.value == "succeeded", st.error
    return st, CO.get_state(rt, rec["plan_set_id"], slot, "a")


def test_every_draft_fails_the_silhouette_with_the_figure_boxes_mask_and_the_tight_mask_keeps_it(rt, planned):
    pid, rec = planned
    step, state = run_char(rt, pid, rec, 7, mask_mode="figure")
    first, second = state["rungs"][0], state["rungs"][1]
    assert (first["mask_mode"], first["survivors"]) == ("figure", 0) and "A_SIL_GUIDE" in first["failed"]
    assert second["mask_mode"] == "tight" and "A_SIL_GUIDE" not in second["failed"], "the tight mask keeps the body inside the guide"
    assert [r["n"] for r in state["rungs"]] == [4, 4, 8][:len(state["rungs"])], "the ladder: first drafts, tight mask, eight drafts"
    assert state["rungs"][-1]["mask_mode"] == "tight"
    assert first["template"] == "I1.concept_char@s0", "no house style sheet yet: the guide-only variant runs"
    if state["chosen"] is not None:
        assert step.result["mask_mode"] == "tight" and state["failed"] is None
    else:
        assert step.result["failed"] is True and state["failed"]["checks"]


def test_a_draw_that_no_rung_can_save_ends_as_needs_human(rt, planned, monkeypatch):
    pid, rec = planned
    monkeypatch.setattr(CO, "blocking_failures", lambda results: [r for r in results if r.check_id == "A_SIL_GUIDE"] or [r for r in results if r.check_id == "A_SIZE"] or
                        [type("R", (), {"check_id": "A_OCR", "passed": False})()])
    real_gate_a = CO.gate_a

    def failing_gate_a(*args, **kw):
        """Every draft fails the silhouette check, whatever colours the mock happened to paint for this plan (the planner's answer, and so the
        mock picture, changes with the prompt text; the test is about the ladder, not about one seeded picture)."""
        results, close, de = real_gate_a(*args, **kw)
        results = [r for r in results if r.check_id != "A_SIL_GUIDE"]
        results.append(CO.runner.build_result("A_SIL_GUIDE", passed=False, subject_sha=args[4], metric="forced", evidence="forced by the test"))
        return results, close, de

    monkeypatch.setattr(CO, "gate_a", failing_gate_a)
    step, state = run_char(rt, pid, rec, 8)
    assert state["chosen"] is None and state["failed"]["checks"] and len(state["rungs"]) == 2, "head mask, then eight drafts"
    assert step.result["failed"] is True
    from duoskin.models.gate import TileState
    from duoskin.pipeline import plan as PL

    project = rt.repo.get_project(pid)
    tile = CO.tile_for(rt, project, rec["plan_set_id"], 8, rt.repo.get_spec(rec["id"]), [rt.repo.get_spec(rec["id"])], PL.get_envelope(rt, rec["plan_set_id"]))
    assert tile.state == TileState.NEEDS_HUMAN and tile.facts["failed"]["a"]["message"]
