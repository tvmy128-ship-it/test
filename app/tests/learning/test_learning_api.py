"""APP_SPEC §13: /api/calibration/* and /api/learning/*, /api/regression/*, /api/versions/*."""
from __future__ import annotations

import json

import pytest
from lfix import add_check, approved_duo, figure, make_project, seed_labels, spec_dict, store_png, store_renders

from duoskin.engine import calibration as cal
from duoskin.pipeline import regression as R


@pytest.fixture
def rt(client, app):
    """The runtime of the app under test (the plain ``rt`` fixture would be a second runtime on the same folder)."""
    return app.state.rt


def seed_duos(rt, n=6, gate_labels=120):
    for i in range(n):
        p = approved_duo(rt, f"Base {i}", spec=spec_dict(), minutes=i)
        store_renders(rt, p, (200 - 25 * i, 70 + 20 * i, 80 + 10 * i), (40 + 30 * i, 160 - 15 * i, 200 - 20 * i), a_shape=i % 3, b_shape=(i + 2) % 5)
    seed_labels(rt, gate=gate_labels)


# ------------------------------------------------------------------------------------------------------------------ calibration
def test_the_drill_screen_is_locked_below_five_approved_duos(client, rt):
    r = client.get("/api/calibration/session", params={"kind": "drill"})
    assert r.status_code == 200
    d = r.json()
    assert d["locked"] is True and d["approved_duos"] == 0 and d["needed"] == 5 and d["items"] == []
    assert client.get("/api/calibration/status").json()["locked"] is True


def test_a_round_is_served_blind_answered_and_counted(client, rt):
    seed_duos(rt)
    d = client.get("/api/calibration/session", params={"kind": "drill"}).json()
    assert d["locked"] is False and d["items"] and set(d["items"][0]) == {"item_id", "images", "question"}
    assert d["counts"]["drill"] == 0 and d["counts"]["cap"] == 0.25 and d["session"]["total"] == 20
    item = d["items"][0]
    assert client.get(f"/cas/{item['images'][0]}.png").status_code == 200, "the picture is a CAS file"
    r = client.post("/api/calibration/labels", json={"item_id": item["item_id"], "label": "real_duo", "like": True})
    assert r.status_code == 204 and r.content == b""
    assert cal.label_counts(rt).drill == 2
    again = client.post("/api/calibration/labels", json={"item_id": item["item_id"], "label": "clone"})
    assert again.status_code == 409 and again.json()["error"] == "already_answered"
    assert client.post("/api/calibration/labels", json={"item_id": "dri_nope", "label": "clone"}).json()["error"] == "unknown_item"
    bad = client.post("/api/calibration/labels", json={"item_id": client.get("/api/calibration/session").json()["items"][0]["item_id"], "label": "meh"})
    assert bad.status_code == 422 and bad.json()["error"] == "bad_label"
    assert client.post("/api/calibration/labels", json={"item_id": "x", "label": "clone", "extra": 1}).status_code == 422
    assert client.get("/api/calibration/labels").json()["drill"] == 2


def test_skip_and_stop_here(client, rt):
    seed_duos(rt)
    first = client.get("/api/calibration/session").json()
    sid = first["session"]["id"]
    assert client.post("/api/calibration/labels", json={"item_id": first["items"][0]["item_id"], "label": "skip"}).status_code == 204
    assert cal.label_counts(rt).drill == 0
    assert client.post("/api/calibration/session/end").status_code == 204
    assert client.get("/api/calibration/session").json()["session"]["id"] != sid, "stop here closes the round"


def test_the_cap_is_a_clear_409_and_the_screen_says_so(client, rt):
    seed_duos(rt, gate_labels=3)
    d = client.get("/api/calibration/session").json()
    assert client.post("/api/calibration/labels", json={"item_id": d["items"][0]["item_id"], "label": "clone"}).status_code == 204
    d2 = client.get("/api/calibration/session").json()
    assert d2["items"] == [] and d2["cap_reached"] is True and "quarter" in d2["message"]
    assert client.get("/api/calibration/status").json()["cap_reached"] is True


def test_the_judge_set_serves_pictures_and_stores_my_verdict_next_to_the_judges(client, rt):
    p = make_project(rt, "Judged")
    sha = store_png(rt, figure((200, 60, 60)))
    add_check(rt, p.id, "cn_back_view", passed=True, kind="hard", subject_sha=sha)
    add_check(rt, p.id, "A_ALPHA", passed=False, kind="hard", subject_sha=sha)          # not a judge rule: never served
    d = client.get("/api/calibration/session", params={"kind": "judge"}).json()
    assert d["kind"] == "judge" and len(d["items"]) == 1 and d["items"][0]["images"] == [sha] and "judge" not in json.dumps(d["items"]).lower().replace("judged", "")
    item = d["items"][0]["item_id"]
    assert item.startswith("chk_") and "passed" not in json.dumps(d["items"])
    assert client.post("/api/calibration/labels", json={"item_id": item, "label": "fail"}).status_code == 204
    (lab,) = cal.list_labels(rt, kind="rule_verdict", source="calibration")
    assert lab.value == {"label": "fail", "check_id": "cn_back_view", "subject_sha": sha, "judge_passed": True}
    assert client.post("/api/calibration/labels", json={"item_id": item, "label": "pass"}).json()["error"] == "already_answered"
    assert client.get("/api/calibration/session", params={"kind": "judge"}).json()["items"] == []


# ------------------------------------------------------------------------------------------------------------------ learning
def test_the_report_has_the_numbers_and_the_regression_summary(client, rt):
    d = client.get("/api/learning/report").json()
    for k in ("week", "per_check", "hard_reject_rate_on_approved", "wildcard_pick_rate", "gate1_first_try_approval", "structure_use", "cost_per_duo",
              "registry_reject_rate", "labels", "tuner", "regression"):
        assert k in d, k
    assert d["regression"]["briefs"] == 40 and d["regression"]["sample"] == 10 and d["regression"]["estimate"]["usd"] == 0.0
    assert d["regression"]["versions"]["baseline"] is None and {r["role"] for r in d["regression"]["versions"]["roles"]} >= {"planner", "critic"}
    assert client.get("/api/learning/report", params={"week": "all"}).json()["week"]["label"] == "all time"
    assert client.get("/api/learning/report", params={"week": "2026-W41"}).json()["week"]["start"] == "2026-10-05"
    bad = client.get("/api/learning/report", params={"week": "nonsense"})
    assert bad.status_code == 422 and bad.json()["error"] == "bad_week"


def test_the_tuner_reports_proposals_and_never_applies_them(client, rt):
    cal.record_showings(rt, [("DUO-04", True)] * 10)
    for i in range(30):
        p = approved_duo(rt, f"V{i}", minutes=i)
        add_check(rt, p.id, "DUO-04", passed=True, metric="hair_accessory_silhouette_iou", value=0.3 + 0.01 * i)
    seed_labels(rt, gate=200)
    r = client.post("/api/learning/tuner/run", json={}).json()
    assert r["ready"] is True and any(p["key"] == "duo.silhouette_iou_cold" and p["status"] == "proposed" for p in r["proposals"])
    last = client.get("/api/learning/tuner").json()
    assert last["active"] == {} and last["report"]["generated_at"] and len(last["open_proposals"]) >= 1
    assert client.post("/api/learning/tuner/run", json={"keys": ["pln.contrast_colour_de"]}).json()["proposals"][0]["status"] == "insufficient"
    assert client.post("/api/learning/tuner/run", json={"nonsense": 1}).status_code == 422


def test_accepting_thresholds_needs_a_proposal_and_a_passing_guard(client, rt):
    assert client.post("/api/learning/thresholds/accept", json={"keys": ["duo.silhouette_iou_cold"]}).json()["error"] == "no_proposal"
    for i in range(30):
        p = approved_duo(rt, f"V{i}", minutes=i)
        add_check(rt, p.id, "DUO-04", passed=True, metric="hair_accessory_silhouette_iou", value=0.3 + 0.01 * i)
    seed_labels(rt, gate=200)
    client.post("/api/learning/tuner/run", json={})
    r = client.post("/api/learning/thresholds/accept", json={"keys": ["duo.silhouette_iou_cold"]})
    assert r.status_code == 409 and r.json()["error"] == "guard" and r.json()["guard"]["decision"] == "needs_baseline"
    from duoskin.checks import thresholds as TH

    assert TH.get("duo.silhouette_iou_cold") == 0.85 and cal.active_thresholds(rt) == {}
    assert client.post("/api/learning/thresholds/accept", json={"keys": []}).status_code == 422


def test_a_demoted_check_can_be_turned_back_on(client, rt):
    rt.update_settings({"checks": {"check_overrides": {"A_ALPHA": "soft"}}})
    assert client.post("/api/learning/demotions/restore", json={"check_id": "A_ALPHA"}).status_code == 204
    assert rt.settings.checks.check_overrides == {}
    assert client.post("/api/learning/demotions/restore", json={}).status_code == 422


# ------------------------------------------------------------------------------------------------------------------ regression and versions
def test_the_estimate_and_the_run_list(client, rt):
    e = client.get("/api/regression/estimate", params={"stage": "plan", "sample": 10}).json()
    assert e["live"] is False and e["usd"] == 0.0 and e["stage"] == "plan"
    assert client.get("/api/regression/runs").json()["runs"] == []
    assert client.get("/api/regression/runs/reg_nope").status_code == 404
    assert client.get("/api/versions").json()["baseline"] is None


def test_starting_a_regression_validates_its_arguments(client, rt):
    assert client.post("/api/regression/run", json={"stage": "all"}).status_code == 422
    assert client.post("/api/regression/run", json={"candidate_versions": {"nonsense": "x"}}).json()["error"] == "bad_role"
    r = client.post("/api/regression/run", json={"stage": "parts", "templates": ["I2"]})
    assert r.status_code == 422 and r.json()["error"] == "not_enough_specs"
    assert client.post("/api/regression/run", json={"compare": True, "sample": 2}).json()["error"] == "no_baseline"
    assert client.post("/api/regression/run", json={"unknown": 1}).status_code == 422


def test_promotion_is_refused_over_http_without_a_passing_guard(client, rt):
    r = client.post("/api/versions/promote", json={"role": "planner", "version": "claude-opus-5-20261001"})
    assert r.status_code == 409 and r.json()["error"] == "not_a_candidate"
    rt.update_settings({"models": {"candidates": {"planner": "claude-opus-5-20261001"}}})
    r = client.post("/api/versions/promote", json={"role": "planner", "version": "claude-opus-5-20261001"})
    assert r.status_code == 409 and r.json()["error"] == "no_regression"
    base = {"id": "b", "stage": "plan", "brief_set_sha": "x", "n_briefs": 3, "image_mode": "degraded", "template_kinds": [], "quality": 0.7,
            "metrics": {"image_distance": 0.4, "variety_index": 0.6}}
    cand = {**base, "id": "c", "role": "candidate", "state": "done", "quality": 0.7, "candidate_versions": {"models": {"planner": "claude-opus-5-20261001"}},
            "served_models": ["claude-opus-5-20261001"], "metrics": {"image_distance": 0.2, "variety_index": 0.6}}
    cand["guard"] = R.variety_guard(base, cand).as_dict()
    R.save_run(rt, cand)
    r = client.post("/api/versions/promote", json={"role": "planner", "version": "claude-opus-5-20261001"})
    assert r.status_code == 409 and r.json()["error"] == "guard" and "variety guard" in r.json()["message"]
    assert rt.settings.models.planner == "claude-opus-5"
    v = client.get("/api/versions").json()
    planner = next(x for x in v["roles"] if x["role"] == "planner")
    assert planner["candidate"] == "claude-opus-5-20261001" and planner["can_promote"] is False and planner["run"]["guard"]["decision"] == "reject"
    cand2 = {**cand, "id": "c2", "metrics": base["metrics"]}
    cand2["guard"] = R.variety_guard(base, cand2).as_dict()
    R.save_run(rt, cand2)
    ok = client.post("/api/versions/promote", json={"role": "planner", "version": "claude-opus-5-20261001"})
    assert ok.status_code == 200 and ok.json()["run_id"] == "c2" and rt.settings.models.planner == "claude-opus-5-20261001"
    assert client.post("/api/regression/runs/nope/adopt").json()["error"] == "unknown_run"


def test_a_real_start_returns_the_job_with_its_estimate(client, rt):
    r = client.post("/api/regression/run", json={"stage": "plan", "sample": 2})
    assert r.status_code == 200
    j = r.json()
    assert j["kind"] == "regression" and j["estimate"]["usd"] == 0.0 and j["params"]["run_ids"]
    assert client.post("/api/regression/run", json={"stage": "plan", "sample": 2}).json()["error"] == "busy", "one regression at a time"
    client.post(f"/api/jobs/{j['id']}/cancel")


def test_calibration_routes_need_the_token(app):
    from duoskin.engine.testkit import make_client

    anon = make_client(app, authed=False)
    assert anon.post("/api/calibration/labels", json={"item_id": "x", "label": "clone"}).status_code == 403
    assert anon.post("/api/regression/run", json={}).status_code == 403
    assert anon.post("/api/versions/promote", json={"role": "planner", "version": "v"}).status_code == 403
    assert anon.post("/api/learning/tuner/run", json={}).status_code == 403


@pytest.mark.parametrize("path", ["/api/learning/report", "/api/calibration/status", "/api/regression/runs", "/api/versions"])
def test_read_routes_answer_json_without_a_stack_trace(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "Traceback" not in r.text
