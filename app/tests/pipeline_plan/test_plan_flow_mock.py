"""The PLAN job in mock mode, up to the end of ``plan.select``: the specs the planner writes, the rules, the revisions, the drops and the
notice. (The concept pictures and Gate 1 are covered by ``test_gate1_flow_mock.py``; here the job is stopped when the plans are chosen.)"""
from __future__ import annotations

import json
import re

import pytest
from helpers import (
    all_records,
    failed_hard,
    job_steps,
    new_project,
    shown,
    start_plan,
    step_result,
    wait_select,
)

from duoskin.prompts import llm as prompts_llm

pytestmark = pytest.mark.timeout(600)

SCENARIOS = {
    "open": {"brief": "a cosy duo for a rainy day", "combo": "bg"},
    "twins": {"brief": "twin sisters in matching winter coats", "combo": "gg"},
    "dropdown": {"brief": "two club mates", "combo": "bb", "structure_request": "same_club"},
    "dangling": {"brief": "a bright duo [mock:dangling]", "combo": "bg"},
    "unfixable": {"brief": "a bright duo [mock:unfixable]", "combo": "gb"},
    "no_wild": {"brief": "a bold duo [mock:unfixable_wildcard] [mock:no_wildcard_replacement]", "combo": "bg"},
    "wild_back": {"brief": "a bold duo [mock:unfixable_wildcard]", "combo": "gg"},
    "must": {"brief": "a duo with a bow [mock:critic_fix]", "combo": "bg", "must_include": ["a teal bow on the left"]},
}


@pytest.fixture(scope="module")
def l3_prompts():
    """Every LLM prompt compiled during the module (template id, user text, system text), to look at what the models were sent."""
    seen: list[dict] = []
    real = prompts_llm.compile_llm

    def spy(template_id, inputs=None, **kw):
        p = real(template_id, inputs, **kw)
        seen.append({"id": template_id, "user": p.user_text, "system": "\n".join(b["text"] for b in p.system), "inputs": dict(inputs or {})})
        return p

    prompts_llm.compile_llm = spy
    yield seen
    prompts_llm.compile_llm = real


@pytest.fixture(scope="module")
def world(client, rt, l3_prompts):
    """Every scenario planned once (all jobs started first, so the module pays the planning time once)."""
    pids = {}
    for name, kw in SCENARIOS.items():
        pids[name] = new_project(client, **kw)
    for pid in pids.values():
        start_plan(client, pid)
    for pid in pids.values():
        wait_select(client, pid)
    return pids


# ---------------------------------------------------------------------------------------------------- the open brief
def test_an_open_brief_gives_three_clean_plans_with_three_structures_and_one_wildcard(client, rt, world):
    pid = world["open"]
    rows = shown(client, pid)
    assert len(rows) == 3
    assert len({r["spec"]["world"]["pair_structure"] for r in rows}) == 3, "an open brief mixes the structures"
    assert sum(1 for r in rows if r["spec"]["is_wildcard"]) == 1
    assert [r["rank"] for r in rows] == [0, 1, 2]
    for r in rows:
        assert failed_hard(r) == [], f"plan {r['plan_index']} shows with a failed HARD rule"
        assert r["lint"], "the lint results are stored on the record"
        assert client.get(f"/api/specs/{r['id']}").json()["dna_card"]["spec_id"] == r["id"]
    plan = client.get(f"/api/projects/{pid}/plan").json()
    assert plan["notice"] == "" and sorted(plan["brief_read_as"]) == sorted({r["spec"]["world"]["pair_structure"] for r in rows})
    kinds = [s["kind"] for s in plan["steps"]]
    for k in ("plan.planner", "plan.lint", "plan.critic", "plan.pairwise", "plan.revise", "plan.select"):
        assert k in kinds, k
    assert kinds.count("plan.critic") == 3 and kinds.count("plan.pairwise") == 3 and "plan.reference" not in kinds


def test_the_critics_and_the_ranker_ran_with_fresh_context_and_their_scores_are_stored(client, rt, world):
    rows = shown(client, world["open"])
    assert all(r["critic_levels"] for r in rows)
    assert all(set(r["critic_levels"].values()) <= {"fail", "weak", "ok", "strong"} for r in rows)
    wins = sum(r["pairwise_wins"] for r in rows)
    assert wins == pytest.approx(3.0), "three pairs, one win (or two halves) each"


def test_the_critic_sees_an_anonymised_spec_without_the_story_and_the_wildcard_flag(client, rt, world, l3_prompts):
    critics = [p for p in l3_prompts if p["id"] == "L4.critic"]
    assert critics, "L4 ran"
    for p in critics:
        assert re.search(r'<spec id="[XYZUVW]">', p["user"]), "the spec is anonymised"
        assert "prj_" not in p["user"] and "spc_" not in p["user"]
        assert '"story"' not in p["user"], "the story is removed (verbosity bias)"
    assert any("wildcard: ignore taste_fit" in p["user"] for p in critics), "the wildcard is scored without taste fit"
    rankers = [p for p in l3_prompts if p["id"] == "L5.pairwise_ranker"]
    assert len(rankers) >= 6, "both orders of every pair"


def test_l3_never_receives_a_worked_example(l3_prompts, world):
    prompts = [p for p in l3_prompts if p["id"] == "L3.planner"]
    assert len(prompts) >= len(SCENARIOS)
    for p in prompts:
        text = p["user"]
        assert "<spec" not in text and "<example" not in text.lower()
        for key in ('"shared_anchors"', '"contrasts"', '"is_wildcard"', '"palette"', '"lash_style"', '"hair_kit_ids"' if "recently_used>none" in text else ""):
            if key:
                assert key not in text, f"{key} in the planner's user message would be a copyable example"
        assert "<recently_used>" in text and "<avoid>" in text and "<recent_cards>" in text
        system = p["system"]
        assert '"contrasts":[' not in system and '"shared_anchors":[' not in system and '"is_wildcard":' not in system


def test_the_planner_prompt_of_equal_projects_is_identical(l3_prompts, world):
    """Nothing in the planner's input is random: the same brief and memory give the same prompt text (so the content cache can serve it)."""
    prompts = [p["user"] for p in l3_prompts if p["id"] == "L3.planner" and "a cosy duo for a rainy day" in p["user"]]
    assert len(prompts) >= 1 and len(set(prompts)) == 1


# ---------------------------------------------------------------------------------------------------- named structures
def test_a_brief_that_names_a_structure_keeps_it_for_all_three_plans_and_still_has_a_wildcard(client, world):
    rows = shown(client, world["twins"])
    assert len(rows) == 3
    assert {r["spec"]["world"]["pair_structure"] for r in rows} == {"mirror"}, "twin sisters is a mirror pair"
    wild = [r for r in rows if r["spec"]["is_wildcard"]]
    assert len(wild) == 1
    others = [r for r in rows if not r["spec"]["is_wildcard"]]
    wf = wild[0]["spec"]["world"]["palette_family"]
    assert any(o["spec"]["world"]["palette_family"] != wf for o in others), "the wildcard departs in palette family"
    assert all(failed_hard(r) == [] for r in rows)


def test_the_dropdown_structure_is_a_hard_rule_and_is_kept(client, world):
    rows = shown(client, world["dropdown"])
    assert {r["spec"]["world"]["pair_structure"] for r in rows} == {"same_club"} and len(rows) == 3
    assert sum(1 for r in rows if r["spec"]["is_wildcard"]) == 1
    names = {c["metric"] for r in rows for c in r["lint"] if c["metric"]}
    assert "structure_request" in names or all(failed_hard(r) == [] for r in rows)


# ---------------------------------------------------------------------------------------------------- revision, drops, replacements
def test_a_plan_that_fails_hard_lint_is_revised_and_never_shown(client, rt, world):
    pid = world["dangling"]
    records = all_records(client, pid)
    broken = [r for r in records if r["version"] == 1 and r["created_by"] == "planner" and failed_hard(r)]
    assert broken, "the planner's plan 2 refers to a palette colour that does not exist"
    for b in broken:
        assert b["status"] != "shown", "a plan with a failed HARD rule is never shown"
        assert b["status"] == "superseded"
    shown_rows = shown(client, pid)
    assert len(shown_rows) == 3 and all(failed_hard(r) == [] for r in shown_rows)
    revised = [r for r in shown_rows if r["created_by"] == "reviser"]
    assert len(revised) == 1 and revised[0]["version"] == 2
    assert revised[0]["parent_spec_id"] == broken[0]["id"]
    assert revised[0]["patch_from_parent"] and revised[0]["patch_from_parent"][0]["path"] == "/a/top/base_ref"
    assert revised[0]["plan_index"] == broken[0]["plan_index"]
    assert {r["id"] for r in shown_rows}.isdisjoint({b["id"] for b in broken})
    plan_step = next(s for s in job_steps(client, pid, "plan.planner") if not rt.repo.get_step(s["id"]).params.get("replacement"))
    assert step_result(rt, plan_step["id"])["rule_problems"], "the provider rejected the answer's rules; the plan loop kept it and linted it"
    scope = rt.db.conn().execute("select passed from checks where project_id=? and check_id='CHK-G0-11'", (pid,)).fetchall()
    assert scope and all(r[0] for r in scope), "the patch stayed inside its findings"


def test_a_plan_the_reviser_cannot_fix_is_dropped_after_two_rounds_and_replaced(client, rt, world):
    pid = world["unfixable"]
    assert len(job_steps(client, pid, "plan.revise")) == 2 + 0, "two revision rounds, not more"
    records = all_records(client, pid)
    dropped = [r for r in records if r["status"] == "dropped" and failed_hard(r)]
    assert dropped and all(d["spec"]["a"]["top"]["base_ref"] == "p98" for d in dropped)
    planners = [rt.repo.get_step(s["id"]) for s in job_steps(client, pid, "plan.planner")]
    replacement = [p for p in planners if p.params.get("replacement")]
    assert len(replacement) == 1 and replacement[0].params["replaces"] == dropped[0]["id"] and replacement[0].params["dropped_reasons"]
    shown_rows = shown(client, pid)
    assert len(shown_rows) == 3 and all(failed_hard(r) == [] for r in shown_rows)
    assert dropped[0]["id"] not in {r["id"] for r in shown_rows}
    assert sum(1 for r in shown_rows if r["spec"]["is_wildcard"]) == 1
    assert client.get(f"/api/projects/{pid}/plan").json()["notice"] == ""
    texts = [p for p in planners if p.params.get("replacement")]
    assert texts and texts[0].params["wildcard"] is False


def test_a_wildcard_that_could_not_be_built_shows_the_notice(client, rt, world):
    pid = world["no_wild"]
    rows = shown(client, pid)
    assert len(rows) == 3 and sum(1 for r in rows if r["spec"]["is_wildcard"]) == 0
    plan = client.get(f"/api/projects/{pid}/plan").json()
    assert plan["notice"] == "wildcard could not be built"
    assert any(d["wildcard"] for d in plan["dropped"])
    for r in rows:        # the only rule left failing is the set rule "exactly one wildcard", which the notice answers
        allowed = {c["check_id"] for c in r["lint"] if c["metric"] == "wildcard_count"}
        assert set(failed_hard(r)) <= allowed


def test_a_dropped_wildcard_is_replaced_by_a_new_wildcard(client, rt, world):
    pid = world["wild_back"]
    rows = shown(client, pid)
    assert len(rows) == 3 and sum(1 for r in rows if r["spec"]["is_wildcard"]) == 1
    assert client.get(f"/api/projects/{pid}/plan").json()["notice"] == ""
    replacement = [rt.repo.get_step(s["id"]) for s in job_steps(client, pid, "plan.planner")]
    assert any(p.params.get("replacement") and p.params.get("wildcard") for p in replacement), "the replacement keeps the wildcard role"


def test_a_high_severity_critic_fix_is_revised_in_round_one_and_must_include_lines_are_covered(client, rt, world):
    pid = world["must"]
    rows = shown(client, pid)
    assert len(rows) == 3
    revised = [r for r in rows if r["created_by"] == "reviser"]
    assert revised, "the critic's high-severity fix triggers the reviser"
    assert all(r["patch_from_parent"][0]["path"] == "/a/accessories/0/description" for r in revised)
    assert all(failed_hard(r) == [] for r in rows)
    from duoskin.pipeline import plan as PL

    env = PL.get_envelope(rt, rows[0]["plan_set_id"])
    assert [c["text"] for c in env["brief_constraints"]] == ["a teal bow on the left"]
    from duoskin.pipeline import brief as BR

    for r in rows:
        cover = BR.must_include_coverage(["a teal bow on the left"], env["brief_constraints"], r["spec"])
        assert cover[0]["covered"] is True


# ---------------------------------------------------------------------------------------------------- costs and the cache
def test_every_claude_call_is_recorded_in_the_ledger_and_an_identical_second_plan_costs_nothing(client, rt, world, l3_prompts):
    pid = world["open"]
    rows = rt.db.conn().execute("select step_id, operation, usd from cost_ledger where project_id=?", (pid,)).fetchall()
    ops = [r[1] for r in rows]
    assert len({(r[0], r[1]) for r in rows}) == len(rows), "one ledger row per call of a step"
    assert any(o.startswith("messages.stream:L3") for o in ops) and any(o.startswith("messages.stream:L4") for o in ops)
    assert any(o.startswith("messages.stream:L5") for o in ops)
    twin = new_project(client, **SCENARIOS["open"])
    start_plan(client, twin)
    wait_select(client, twin)
    planner = job_steps(client, twin, "plan.planner")[0]
    assert step_result(rt, planner["id"])["cached"] is True, "the same request is served from the content cache"
    paid = rt.db.conn().execute("select count(*) from cost_ledger where project_id=? and operation like 'messages.stream:L3%'", (twin,)).fetchone()[0]
    assert paid == 0
    assert [r["spec"]["world"]["pair_structure"] for r in shown(client, twin)] == [r["spec"]["world"]["pair_structure"] for r in shown(client, pid)]


def test_the_planner_thinking_is_shown_as_an_event_of_at_most_400_characters(client, rt, world):
    rows = rt.db.conn().execute("select payload from events where type='llm.thinking' limit 20").fetchall()
    assert rows, "the Plan page can show what the planner is doing"
    assert all(len(json.loads(r[0]).get("text", "")) <= 400 for r in rows)


def test_the_plan_job_is_a_real_job_of_the_project(client, rt, world):
    pid = world["open"]
    project = client.get(f"/api/projects/{pid}").json()["project"]
    assert project["plan_job_id"] and project["pins"] is not None
    assert project["pins"]["house_style_version"] == 0 and project["pins"]["models"]["planner"]
