"""APP_SPEC §3.9: the regression job end to end in mock mode, on a small sample of the fixed briefs.

The sample (3 briefs) keeps the test to about a minute; the code path is the one the 40-brief run takes."""
from __future__ import annotations

import pytest
from lfix import make_project

from duoskin.engine.testkit import wait_for
from duoskin.models.job import JobKind, JobState
from duoskin.pipeline import brief as BR
from duoskin.pipeline import plan as PL
from duoskin.pipeline import regression as R

TERMINAL = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


def finish(rt, job, timeout=420):
    wait_for(lambda: rt.repo.get_job(job.id).state in TERMINAL, timeout=timeout, interval=0.5, message=f"regression job {job.id}")
    return rt.repo.get_job(job.id)


def tables(rt):
    c = rt.db.conn()
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("registry_face", "registry_print", "duo_memory", "labels")}


@pytest.mark.timeout(900)
def test_the_whole_regression_cycle_baseline_repeat_candidate_guard_and_promotion(client, app):
    rt = app.state.rt
    before = {"taste": PL.taste_tables(rt), "used": BR.recently_used(rt), "tables": tables(rt), "cards": BR.recent_cards(rt)}

    # ---- a baseline run: no candidate, so the current versions are measured
    job = R.start_regression(rt, stage="plan", sample=3)
    assert job.kind == JobKind.REGRESSION and job.params["estimate_usd"] == 0.0 and job.params["estimate"]["live"] is False
    assert finish(rt, job).state == JobState.SUCCEEDED
    (base,) = R.list_runs(rt, "plan")
    assert base["role"] == "baseline" and base["state"] == "done" and base["n_briefs"] == 3 and base["ok_briefs"] == 3 and base["failed_briefs"] == []
    assert base["image_mode"] == "degraded" and base["metrics"]["images"] == 3 and base["metrics"]["image_distance"] > 0.0
    assert 0.0 < base["metrics"]["variety_index"] < 1.0 and 0.3 < base["quality"] <= 1.0 and base["guard"] is None
    assert base["metrics"]["distinct"]["structures"] >= 2 and base["metrics"]["categories"]["structures"]["n"] == 3
    assert "claude-opus-5" in base["served_models"] and len(base["per_brief"]) == 3 and all(p["ok"] for p in base["per_brief"])
    assert R.baseline_of(rt, "plan")["id"] == base["id"] and base["brief_set_sha"] == R.ensure_briefs(rt).sha256 and base["memory_sha"]

    # ---- nothing is left behind: no approval, no registry row, no label, no gate, no taste evidence, no memory
    projects = rt.repo.list_projects(include_archived=True)
    assert len(projects) == 3 and all(p.name.startswith("[regression] ") and p.archived for p in projects)
    assert rt.repo.list_projects() == [], "the Home page lists no regression project"
    assert rt.repo.list_gates(state="open") == [] and not [j for j in rt.repo.list_jobs() if j.kind == JobKind.PLAN and j.state == JobState.RUNNING]
    streams = {r[0] for r in rt.db.conn().execute("SELECT DISTINCT stream FROM asset_links WHERE project_id IS NOT NULL").fetchall()}
    assert streams == {"regression"}
    assert [r for r in rt.db.conn().execute("SELECT 1 FROM specs WHERE json_extract(json,'$.rank') IS NOT NULL")] == []
    assert tables(rt) == before["tables"]
    assert PL.taste_tables(rt) == before["taste"] and BR.recently_used(rt) == before["used"], "the planner's memory is untouched"
    assert BR.recent_cards(rt) == before["cards"] == [] and BR.approved_specs(rt) == []
    from duoskin.engine import calibration as cal

    assert cal.approved_duo_count(rt) == 0 and cal.fire_rates(rt) == {} and cal.cost_per_duo(rt)["total_usd"] == 0.0
    assert not any(p.approved_spec_id for p in projects)

    # ---- the same inputs again: the plan loop is served from the content cache, so the numbers are the same
    job2 = R.start_regression(rt, stage="plan", sample=3)
    assert finish(rt, job2).state == JobState.SUCCEEDED
    again = R.list_runs(rt, "plan")[0]
    assert again["id"] != base["id"] and again["role"] == "baseline"
    for key in ("variety_index", "image_distance", "spec_distance", "category_entropy"):
        assert again["metrics"][key] == pytest.approx(base["metrics"][key]), key
    assert again["quality"] == pytest.approx(base["quality"])
    assert len(rt.repo.list_projects(include_archived=True)) == 3, "the hidden projects are reused, not multiplied"

    # ---- a candidate model: it is switched on in memory, really called, and judged by the guard
    cand = "claude-opus-5-20261001"
    rt.update_settings({"models": {"candidates": {"planner": cand}}})
    job3 = R.start_regression(rt, stage="plan", sample=3, candidate_versions={"planner": cand})
    assert job3.params["run_ids"] and len(job3.params["run_ids"]) == 1, "a comparable baseline exists, so only the candidate arm runs"
    assert finish(rt, job3).state == JobState.SUCCEEDED
    run = R.load_run(rt, job3.params["run_ids"][0])
    assert run["role"] == "candidate" and cand in run["served_models"], "the candidate snapshot was really used"
    assert rt.model_override == {} and rt.effective_settings().models.planner == "claude-opus-5" and rt.settings.models.planner == "claude-opus-5"
    assert run["guard"]["baseline_id"] == again["id"], "the newest baseline is what a candidate is compared with"
    assert run["guard"]["decision"] == "accept", run["guard"]["reasons"]
    assert {c["name"] for c in run["guard"]["checks"]} >= {"image_distance", "variety_index", "quality", "served:planner"}

    # ---- promotion goes only through the guard
    out = R.promote(rt, "planner", cand)
    assert out["run_id"] == run["id"] and rt.settings.models.planner == cand and rt.settings.models.candidates == {}
    assert R.baseline_of(rt, "plan")["id"] == run["id"]


@pytest.mark.timeout(900)
def test_a_candidate_that_lowers_variety_is_rejected_and_cannot_be_promoted(client, app):
    rt = app.state.rt
    job = R.start_regression(rt, stage="plan", sample=2)
    assert finish(rt, job).state == JobState.SUCCEEDED
    base = R.baseline_of(rt, "plan")
    # the baseline run claims more variety than any rerun can reproduce (as if the old prompts had been far more varied)
    base["metrics"]["image_distance"] *= 1.5
    base["metrics"]["variety_index"] *= 1.5
    R.save_run(rt, base)
    cand = "claude-opus-5-20261002"
    rt.update_settings({"models": {"candidates": {"critic": cand}}})
    job2 = R.start_regression(rt, stage="plan", sample=2, candidate_versions={"critic": cand})
    assert finish(rt, job2).state == JobState.SUCCEEDED
    run = R.load_run(rt, job2.params["run_ids"][0])
    assert run["guard"]["decision"] == "reject" and any("fell" in r for r in run["guard"]["reasons"])
    with pytest.raises(R.RegressionError) as e:
        R.promote(rt, "critic", cand)
    assert e.value.code == "guard" and rt.settings.models.critic == "claude-opus-5" and rt.settings.models.candidates == {"critic": cand}


def test_a_candidate_model_is_refused_while_another_duo_is_being_worked_on(client, app):
    rt = app.state.rt
    p = make_project(rt, "Busy duo")
    rt.scheduler.submit_job(JobKind.PLAN, p.id, {}, steps=[])
    with pytest.raises(R.RegressionError) as e:
        R.start_regression(rt, stage="plan", sample=2, candidate_versions={"planner": "claude-opus-5-20261003"})
    assert e.value.code == "busy" and "other duos" in str(e.value)
    assert rt.model_override == {}


def test_a_real_run_above_the_ask_threshold_waits_at_a_budget_gate_and_stops_cleanly(client, app):
    rt = app.state.rt
    rt.provider_override.clear()
    rt.update_settings({"providers": {"modes": {"anthropic": "real", "openai": "real"}}})
    job = R.start_regression(rt, stage="plan")                                       # all 40 briefs: $80 to $180
    assert job.params["estimate_usd"] == 180.0 and job.params["estimate"]["needs_confirmation"] is True
    from duoskin.models.gate import GateAction, GateDecisionIn, GateKind

    gate = wait_for(lambda: next((g for g in rt.repo.list_gates(state="open") if g.kind == GateKind.BUDGET and g.job_id == job.id), None), timeout=30,
                    interval=0.2, message="the regression BUDGET gate")
    assert gate.tiles[0].facts["estimate_usd"] == 180.0 and gate.tiles[0].facts["reason"] == "regression" and len(rt.repo.list_gates(state="open")) == 1, "one gate for the whole job"
    assert rt.repo.list_projects(include_archived=True) == [], "nothing was planned before the person confirmed"
    rt.gates.decide(gate.id, GateDecisionIn(tile_id=gate.tiles[0].tile_id, action=GateAction.STOP, expected_version=0, client_decision_id="stop1"))
    wait_for(lambda: rt.repo.get_job(job.id).state in TERMINAL, timeout=30, interval=0.2, message="the stopped job")
    assert rt.repo.list_projects(include_archived=True) == []
    assert R.list_runs(rt)[0]["state"] == "stopped" and R.baseline_of(rt, "plan") is None


@pytest.mark.timeout(900)
def test_a_threshold_set_is_judged_on_the_baseline_plans_linted_again(client, app):
    rt = app.state.rt
    assert R.evaluate_threshold_candidate(rt, {"pln.contrast_colour_de": 12.0}).decision == "needs_baseline"
    job = R.start_regression(rt, stage="plan", sample=2)
    assert finish(rt, job).state == JobState.SUCCEEDED
    base = R.baseline_of(rt, "plan")
    same = R.evaluate_threshold_candidate(rt, {})
    assert same.accepted and next(c for c in same.checks if c["name"] == "quality")["change"] == pytest.approx(0.0)
    # a bound the plans already clear changes nothing; an impossible one fails the contrasts the baseline accepted
    loose = R.evaluate_threshold_candidate(rt, {"pln.contrast_colour_de": 1.0})
    assert loose.accepted
    tight = R.evaluate_threshold_candidate(rt, {"pln.contrast_colour_de": 99.0, "pln.anchor_colour_de_max": 0.0, "pln.kit_hair_iou_warn": 0.0})
    assert tight.decision == "reject" and any("quality score fell" in r for r in tight.reasons)
    # variety cannot fall: the plans and the pictures are the baseline's own
    for c in tight.checks:
        if c["name"] in ("image_distance", "variety_index"):
            assert c["change"] == 0.0 and c["ok"]
    assert tight.baseline_id == base["id"]
