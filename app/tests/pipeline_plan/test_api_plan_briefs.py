"""The routes of this track: the brief helpers, the plan of a project, one spec, a change request."""
from __future__ import annotations

import pytest
from planhelpers import new_project


def test_the_structure_dropdown_starts_with_auto(client):
    rows = client.get("/api/briefs/structures").json()["structures"]
    assert rows[0] == {"value": "auto", "label": "Let the planner choose"}
    assert {"complement", "same_club", "mirror", "seasonal_twins", "object_mascot", "leader_chaotic"} <= {r["value"] for r in rows}


def test_the_brief_check_warns_about_words_the_design_cannot_use_but_never_blocks(client):
    ok = client.post("/api/briefs/check", json={"brief": "two friends at a night market", "must_include": ["a teal bow"]}).json()
    assert ok["ok"] is True and ok["notes"] == []
    bad = client.post("/api/briefs/check", json={"brief": "a Roblox logo duo like Disney", "must_include": ["one two three four five six seven eight nine ten eleven twelve thirteen"],
                                                "structure_request": "mirror"}).json()
    assert bad["ok"] is True
    fields = {n["field"] for n in bad["notes"]}
    assert "brief" in fields and "must_include 1" in fields
    assert any("cannot use" in n["text"] for n in bad["notes"] if n["field"] == "brief")
    unknown = client.post("/api/briefs/check", json={"brief": "x", "structure_request": "twin_sisters"}).json()
    assert unknown["ok"] is False and unknown["notes"][0]["field"] == "structure_request"
    assert client.post("/api/briefs/check", json={"brief": "x", "extra": 1}).status_code == 422


def test_the_plan_estimate_is_a_range(client):
    pid = new_project(client)
    est = client.get(f"/api/projects/{pid}/plan/estimate").json()
    assert est["estimate"] is True and 0 < est["usd_low"] < est["usd_high"] and est["breakdown"]


def test_the_plan_of_a_project_that_has_not_started_is_empty(client):
    pid = new_project(client)
    plan = client.get(f"/api/projects/{pid}/plan").json()
    assert plan["job"] is None and plan["steps"] == [] and plan["specs"] == [] and plan["plan_set_id"] is None
    assert plan["gate_id"] is None and plan["notice"] == "" and plan["stage"] == "brief"
    assert client.get("/api/projects/prj_missing/plan").status_code == 404


@pytest.mark.parametrize("url", ["/api/specs/spc_missing", "/api/changes/chg_missing"])
def test_unknown_ids_are_404(client, url):
    assert client.get(url).status_code == 404


def test_the_changes_of_a_project_start_empty(client):
    pid = new_project(client)
    assert client.get(f"/api/projects/{pid}/changes").json() == []
