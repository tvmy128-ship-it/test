"""L1 (the reference analyst) and L2 (the taste profile): what they receive, what the code keeps of their answers, and that both reach the
planner as data (never as an example)."""
from __future__ import annotations

import json

import pytest
import specfix
from planhelpers import approved_spec, db_project, job_steps, new_project, shown, start_plan, step_result, wait_select

from duoskin.engine.testkit import png_bytes
from duoskin.models.llm_io import ReferenceAnalysis, TasteProfile
from duoskin.pipeline import plan as PL
from duoskin.prompts import llm as prompts_llm


def analysis(rules, do_not_copy=("the exact motif drawn in the first picture",)):
    return ReferenceAnalysis.model_validate({"rules": rules, "duo_devices": ["a shared colour at the same body spot"],
                                             "quality_bar": ["clean readable shapes"], "do_not_copy": list(do_not_copy), "brand_or_character_flags": []})


def rule(axis, text, obs="what is visible"):
    return {"axis": axis, "observation": obs, "rule": text}


# ---------------------------------------------------------------------------------------------------- Gate A of L1
def test_gate_a_of_the_reference_analyst_drops_rules_that_copy_content_or_use_banned_words():
    a = analysis([rule("line_weight", "keep outlines thin and even"),
                  rule("palette", "paint it like Disney does"),
                  rule("silhouette", "repeat the exact motif drawn in the first picture"),
                  rule("shading", "use flat shadows")])
    clean, dropped = PL._clean_analysis(a)
    assert [r.axis for r in clean.rules] == ["line_weight", "shading"]
    assert len(dropped) == 2 and any("banned" in d or "disney" in d.lower() for d in dropped) and any("copies" in d for d in dropped)


def test_gate_a_keeps_at_most_three_rules_per_axis():
    a = analysis([rule("palette", f"use {w} colours only") for w in ("two", "three", "four", "five", "six")])
    clean, dropped = PL._clean_analysis(a)
    assert len(clean.rules) == 3 and len(dropped) == 2 and all("more than 3" in d for d in dropped)


# ---------------------------------------------------------------------------------------------------- L2
@pytest.mark.usefixtures("demo_inv")
def test_the_taste_tables_are_counted_in_code_from_approved_and_rejected_plans(unit_rt):
    p1, p2, p3 = (db_project(unit_rt, name=f"Duo {i}") for i in range(3))
    approved_spec(unit_rt, p1.id, specfix.load_dict("spec_complement_gb"), plan_set_id="a")
    approved_spec(unit_rt, p2.id, specfix.load_dict("spec_same_club_bb"), plan_set_id="b")
    shown_rec = PL.new_spec_record(unit_rt, p3.id, "c", 0, specfix.load_dict("spec_mirror_gg"), status="superseded")
    PL.save_spec(unit_rt, shown_rec.model_copy(update={"rank": 1}))                       # shown to the person and not chosen
    PL.new_spec_record(unit_rt, p3.id, "c", 1, specfix.load_dict("spec_twins_bg"), status="dropped")      # never shown: not a rejection
    tables = PL.taste_tables(unit_rt)
    assert tables["approved_count"] == 2 and tables["rejected_count"] == 1
    structure = tables["fields"]["pair_structure"]
    assert set(structure) == {"complement", "same_club", "mirror"}
    assert len(structure["complement"]["approved"]) == 1 and len(structure["mirror"]["rejected"]) == 1 and not structure["mirror"]["approved"]
    assert PL.taste_has_data(tables) and not PL.taste_has_data({"approved_count": 1})
    assert PL.taste_tables(unit_rt, exclude_project=p1.id)["approved_count"] == 1


def test_gate_a_of_the_taste_builder_keeps_only_tendencies_with_two_known_evidence_ids():
    tables = {"fields": {"hair_style": {"hair_bob_03": {"approved": ["spc_1", "spc_2"], "rejected": []}}}}
    profile = TasteProfile.model_validate({
        "likes": [{"field": "hair_style", "tendency": "likes a bob", "evidence_ids": ["spc_1", "spc_2"], "strength": "moderate"},
                  {"field": "hair_style", "tendency": "likes one thing once", "evidence_ids": ["spc_1"], "strength": "weak"},
                  {"field": "saturation", "tendency": "made up", "evidence_ids": ["spc_8", "spc_9"], "strength": "strong"}],
        "dislikes": [], "open_questions": [], "explore": ["a calmer palette"]})
    kept = PL._clean_taste(profile, tables)
    assert [r.tendency for r in kept.likes] == ["likes a bob"], "a like needs two ids from the tables (PLN-17)"


# ---------------------------------------------------------------------------------------------------- the real pipeline
@pytest.fixture(scope="module")
def seen():
    log: list[dict] = []
    real = prompts_llm.compile_llm

    def spy(template_id, inputs=None, **kw):
        p = real(template_id, inputs, **kw)
        log.append({"id": template_id, "user": p.user_text, "inputs": dict(inputs or {})})
        return p

    prompts_llm.compile_llm = spy
    yield log
    prompts_llm.compile_llm = real


@pytest.mark.timeout(400)
def test_a_reference_picture_becomes_construction_rules_and_reaches_the_planner(client, rt, seen):
    pid = new_project(client, brief="two friends at a lantern festival")
    r = client.post(f"/api/projects/{pid}/references", files={"file": ("ref.png", png_bytes(64, 64), "image/png")},
                    data={"role": "reference", "note": "I like the thin outlines"})
    assert r.status_code == 200, r.text
    start_plan(client, pid)
    wait_select(client, pid)
    ref = job_steps(client, pid, "plan.reference")
    assert len(ref) == 1 and ref[0]["state"] == "succeeded"
    res = step_result(rt, ref[0]["id"])
    assert len(res["analysis"]["rules"]) >= 3 and res["flags"] == []
    l1 = [p for p in seen if p["id"] == "L1.reference_analyst"]
    assert l1 and "I like the thin outlines" in l1[-1]["user"]
    l3 = [p for p in seen if p["id"] == "L3.planner" and "lantern festival" in p["user"]]
    assert l3
    block = json.loads(l3[-1]["inputs"]["reference_analysis"])
    assert block["rules"] and block["do_not_copy"], "the planner is told what not to copy"
    assert "none" not in l3[-1]["user"].split("<reference_analysis>")[1].split("</reference_analysis>")[0]
    assert len(shown(client, pid)) == 3
    rows = rt.db.conn().execute("select operation from cost_ledger where project_id=? and operation like 'messages.stream:L1%'", (pid,)).fetchall()
    assert len(rows) == 1, "the analyst's call was paid and recorded once"


@pytest.mark.timeout(600)
def test_the_taste_profile_is_built_once_there_are_two_approved_duos_and_reaches_the_planner(client, rt, seen):
    firsts = []
    for i in range(2):
        pid = new_project(client, brief=f"duo number {i} with a picnic", name=f"Taste duo {i}")
        start_plan(client, pid)
        wait_select(client, pid)
        rows = shown(client, pid)
        assert len(rows) == 3
        for n, rec in enumerate(rows):                      # the first plan is approved, the other two were shown and not chosen
            rt.repo.set_spec_status(rec["id"], "approved" if n == 0 else "superseded")
        firsts.append(rows[0]["id"])
    third = new_project(client, brief="duo number 3 with a picnic", name="Taste duo 3")
    start_plan(client, third)
    wait_select(client, third)
    taste = job_steps(client, third, "plan.taste")
    assert len(taste) == 1 and taste[0]["state"] == "succeeded"
    res = step_result(rt, taste[0]["id"])
    assert "skipped" not in res and res["version"] >= 1 and res["profile"]["explore"]
    assert (rt.paths.user_data_dir / "taste_profile.json").exists()
    doc = json.loads((rt.paths.user_data_dir / "taste_profile.json").read_text(encoding="utf-8"))
    assert doc["profile"] == res["profile"]
    for like in res["profile"]["likes"] + res["profile"]["dislikes"]:
        assert len(set(like["evidence_ids"])) >= 2
    l3 = [p for p in seen if p["id"] == "L3.planner" and "duo number 3" in p["user"]][-1]
    assert l3["inputs"]["taste_profile"] != "none" and "profile" in json.loads(l3["inputs"]["taste_profile"])
    first_project = [p for p in seen if p["id"] == "L3.planner" and "duo number 0" in p["user"]][-1]
    assert first_project["inputs"]["taste_profile"] == "none", "the first duo had no taste data"
    wildcard_critics = [p for p in seen if p["id"] == "L4.critic" and "wildcard: ignore taste_fit" in p["user"]]
    assert wildcard_critics, "the wildcard's critique ignores the taste profile"
