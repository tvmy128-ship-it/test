"""Mock-mode end to end, BRIEF to the part board (APP_SPEC §17.4, track B1): a brief (a boy and a girl, open) becomes a PLAN job with three
lint-clean plans (three pair structures, exactly one labelled wildcard), concept pictures, Gate 1 with front and back of both characters per
plan, and an approval that locks the palette and the DNA card (v1) and advances the project to the part-board stage.

Also: a brief that names a structure keeps it, and the planner's prompt of the next duo differs from this duo's only by the memory hints
(``<recently_used>`` and ``<recent_cards>``), never by an example.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline_plan"))

from planhelpers import (
    approve,
    concept_gate,
    failed_hard,
    job_steps,
    new_project,
    png_size,
    shown,
    specs_of,
    start_plan,
    tile_by_slot,
    wait_gate1,
    wait_select,
)

from duoskin.engine.testkit import wait_for
from duoskin.prompts import llm as prompts_llm

BRIEF = "two friends sharing a rainy-day picnic"
MUST = ["a teal ribbon"]


@pytest.fixture
def prompts():
    """Every LLM prompt compiled while the test runs."""
    seen: list[dict] = []
    real = prompts_llm.compile_llm

    def spy(template_id, inputs=None, **kw):
        p = real(template_id, inputs, **kw)
        seen.append({"id": template_id, "user": p.user_text, "system": "\n".join(b["text"] for b in p.system)})
        return p

    prompts_llm.compile_llm = spy
    yield seen
    prompts_llm.compile_llm = real


def l3(seen: list[dict]) -> list[str]:
    return [p["user"] for p in seen if p["id"] == "L3.planner"]


def strip_hints(text: str) -> str:
    """The planner message without the memory hints (the only part that may differ between two duos with the same brief)."""
    return re.sub(r"<(recently_used|recent_cards)>.*?</\1>", r"<\1>…</\1>", text, flags=re.DOTALL)


@pytest.mark.timeout(900)
def test_a_brief_becomes_three_plans_gate1_and_an_approval_that_locks_the_concept(make_runtime, prompts):
    rt, client = make_runtime()
    pid = new_project(client, brief=BRIEF, combo="bg", must_include=MUST)
    job = start_plan(client, pid)
    assert job["kind"] == "plan" and client.get(f"/api/projects/{pid}").json()["project"]["stage"] == "planning"
    gate = wait_gate1(client, pid)

    # ---- the plans: three lint-clean specs, three structures, exactly one wildcard
    rows = shown(client, pid)
    assert len(rows) == 3 and [r["rank"] for r in rows] == [0, 1, 2]
    assert len({r["spec"]["world"]["pair_structure"] for r in rows}) == 3, "an open brief mixes the pair structures"
    assert sum(1 for r in rows if r["spec"]["is_wildcard"]) == 1
    assert all(failed_hard(r) == [] for r in rows)
    assert {r["spec"]["combo"] for r in rows} == {"bg"}
    plan_steps = [s["kind"] for s in job_steps(client, pid) if s["kind"].startswith("plan.")]
    for kind in ("plan.planner", "plan.lint", "plan.critic", "plan.pairwise", "plan.revise", "plan.select"):
        assert kind in plan_steps

    # ---- Gate 1: front and back of both characters per plan, one wildcard label, the must-include line covered
    assert gate["kind"] == "concept" and len(gate["tiles"]) == 3
    for t in gate["tiles"]:
        assert t["state"] == "ready"
        for role in ("a_front", "a_back", "b_front", "b_back"):
            assert png_size(rt, t["assets"][role]) == (768, 1024)
        assert t["facts"]["must_include"] == [{"text": "a teal ribbon", "covered": True, "paths": t["facts"]["must_include"][0]["paths"]}]
    assert sum(1 for t in gate["tiles"] if "Wildcard" in t["badges"]) == 1
    assert client.get(f"/api/projects/{pid}").json()["project"]["stage"] == "gate1"
    first_prompt = l3(prompts)[0]

    # ---- approve one plan: C3 locks the palette and DNA v1, the project advances to the part board
    tile = next(t for t in gate["tiles"] if "Wildcard" not in t["badges"])
    approve(client, gate, tile)
    wait_for(lambda: client.get(f"/api/projects/{pid}").json()["project"]["stage"] == "parts", timeout=300, message="the part board stage")
    bundle = client.get(f"/api/projects/{pid}").json()
    project = bundle["project"]
    locked = next(r for r in specs_of(client, pid) if r["spec"]["id"] == project["approved_spec_id"])
    assert locked["spec"]["created_by"] == "palette_lock" and locked["spec"]["palette_source"] == "concept_extracted"
    assert locked["dna_card"]["locked"] is True and locked["dna_card"]["version"] == 1 and locked["dna_card"]["source"] == "concept_extracted"
    assert [r for r in rt.repo.list_links(project_id=pid, role="concept_of_record")]
    assert concept_gate(client, pid) is None
    kinds = [json.loads(e[0])["stage"] for e in rt.db.conn().execute(
        "select payload from events where type='project.stage' and project_id=? order by id", (pid,)).fetchall()]
    assert [k for k in kinds if k in ("planning", "gate1", "parts")] == ["planning", "gate1", "parts"], kinds
    assert [jv["job"]["kind"] for jv in client.get("/api/jobs", params={"project_id": pid}).json() if jv["job"]["kind"] == "parts"] == ["parts"]
    for jv in client.get("/api/jobs", params={"project_id": pid}).json():
        client.post(f"/api/jobs/{jv['job']['id']}/cancel")

    # ---- the next duo with the same brief: the planner's message differs only by the memory hints, and never holds an example
    pid2 = new_project(client, brief=BRIEF, combo="bg", must_include=MUST, name="Second duo")
    start_plan(client, pid2)
    wait_select(client, pid2)
    second_prompt = l3(prompts)[-1]
    assert first_prompt != second_prompt, "the finished duo is now memory"
    assert strip_hints(first_prompt) == strip_hints(second_prompt)
    assert "<recently_used>none</recently_used>" in first_prompt and "<recently_used>none</recently_used>" not in second_prompt
    hints = json.loads(re.search(r"<recently_used>(.*?)</recently_used>", second_prompt, re.DOTALL).group(1))
    assert locked["spec"]["spec"]["world"]["pair_structure"] in hints["pair_structures"]
    for key in ('"shared_anchors"', '"contrasts"', '"is_wildcard"'):
        assert key not in second_prompt


@pytest.mark.timeout(600)
def test_a_brief_that_names_a_structure_keeps_it_and_still_has_one_wildcard(make_runtime):
    rt, client = make_runtime()
    pid = new_project(client, brief="twin sisters in matching raincoats", combo="gg")
    start_plan(client, pid)
    wait_select(client, pid)
    rows = shown(client, pid)
    assert len(rows) == 3 and sum(1 for r in rows if r["spec"]["is_wildcard"]) == 1
    assert {r["spec"]["world"]["pair_structure"] for r in rows} == {"mirror"}
    assert all(failed_hard(r) == [] for r in rows)
    assert tile_by_slot is not None and rt is not None
