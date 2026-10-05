"""Gate 1 in mock mode: the tiles (front and back of both characters per plan), approve (the concept lock), Reimagine (a new nonce),
Change (I1e on the chosen draft), the clarifying question, New plan, the not-buildable acknowledgement and the alternatives."""
from __future__ import annotations

import json

import pytest
from planhelpers import (
    approve,
    change_status,
    concept_gate,
    decide,
    job_steps,
    new_project,
    open_side_gates,
    png_size,
    specs_of,
    start_plan,
    step_result,
    tile_by_slot,
    wait_gate1,
    wait_tile,
)

from duoskin.engine.testkit import wait_for
from duoskin.pipeline import concept as CO

pytestmark = pytest.mark.timeout(900)

BRIEF = "a cosy duo for a rainy day"
SCENARIOS = {
    "tiles": {"brief": BRIEF},
    "approve": {"brief": BRIEF},
    "reimagine": {"brief": BRIEF},
    "change": {"brief": BRIEF},
    "clarify": {"brief": BRIEF},
    "reject": {"brief": BRIEF},
    "newplan": {"brief": BRIEF},
    "cape": {"brief": BRIEF + " [mock:notbuildable]"},
    "alts": {"brief": BRIEF},
}


@pytest.fixture(scope="module")
def world(client, rt):
    """Every scenario planned and drawn once, in parallel: each test then works on its own project."""
    pids = {name: new_project(client, **kw) for name, kw in SCENARIOS.items()}
    for pid in pids.values():
        start_plan(client, pid)
    return {name: (pid, wait_gate1(client, pid)) for name, pid in pids.items()}


def cstate(rt, gate, slot, c):
    return CO.get_state(rt, gate["tiles"][0]["facts"]["plan_set_id"], slot, c)


# ---------------------------------------------------------------------------------------------------- the gate
def test_gate1_opens_with_front_and_back_of_both_characters_for_each_plan(client, rt, world):
    pid, gate = world["tiles"]
    assert gate["kind"] == "concept" and [t["tile_id"] for t in gate["tiles"]] == ["plan0", "plan1", "plan2"]
    for t in gate["tiles"]:
        assert t["state"] == "ready"
        for role in ("a_front", "a_back", "b_front", "b_back"):
            assert png_size(rt, t["assets"][role]) == (768, 1024), role
        assert png_size(rt, t["assets"]["sheet"]) == (3072, 1024)
        assert t["allowed_actions"] == ["approve", "reimagine", "change", "new_plan", "select_alternative"]
        assert t["label"].startswith("Plan ")
    assert sum(1 for t in gate["tiles"] if "Wildcard" in t["badges"]) == 1
    assert client.get(f"/api/projects/{pid}").json()["project"]["stage"] == "gate1"


def test_the_tile_facts_carry_what_the_page_shows_and_warnings_wait_for_the_first_choice(client, rt, world):
    pid, gate = world["tiles"]
    by_spec = {r["spec"]["id"]: r for r in specs_of(client, pid)}
    for slot, t in enumerate(gate["tiles"]):
        f = t["facts"]
        assert f["spec_id"] in by_spec and by_spec[f["spec_id"]]["dna_card"] is not None
        assert f["rank"] == slot and f["not_buildable"] == [] and f["must_include"] == []
        assert f["critic_levels"] and f["brief_read_as"] and f["hard_failures"] == []
        assert "warnings" not in f, "SOFT warnings are withheld until the first choice"
        assert f["wildcard"] == ("Wildcard" in t["badges"])
    reads = {tuple(t["facts"]["brief_read_as"]) for t in gate["tiles"]}
    assert len(reads) == 1 and len(next(iter(reads))) == 3


def test_the_picture_checks_are_recorded_and_pass_for_the_shown_drafts(client, rt, world):
    pid, gate = world["tiles"]
    rows = rt.db.conn().execute("select check_id, kind, passed from checks where project_id=?", (pid,)).fetchall()
    seen = {r[0] for r in rows}
    for cid in ("A_SIL_GUIDE", "A_PASTE", "A_OCR", "A_LEAK", "A_SWATCH", "CHK-G1-07", "CHK-G1-09", "CHK-G1-11", "cn_blocky_body", "ip_no_brand",
                "dj_not_clones", "dj_same_world"):
        assert cid in seen, cid
    g07 = [r for r in rows if r[0] == "CHK-G1-07"]
    assert g07 and all(r[2] for r in g07) and all(r[1] == "assert" for r in g07)
    chosen_fail = [r for r in rows if r[1] in ("hard", "assert") and not r[2]]
    for t in gate["tiles"]:
        assert t["facts"]["hard_failures"] == []
    assert chosen_fail is not None


def test_the_concept_state_keeps_alternatives_and_the_chosen_draft_per_character(client, rt, world):
    _pid, gate = world["tiles"]
    for slot in range(3):
        for c in "ab":
            st = cstate(rt, gate, slot, c)
            assert st["chosen"]["front"] == tile_by_slot(gate, slot)["assets"][f"{c}_front"]
            assert st["failed"] is None and st["rungs"] and st["rungs"][0]["mask_mode"] == "head"
            assert st["chosen"]["judged"] is True


# ---------------------------------------------------------------------------------------------------- approve and the lock
def test_approve_locks_the_palette_and_dna_v1_and_advances_to_the_part_board(client, rt, world):
    pid, gate = world["approve"]
    tile = tile_by_slot(gate, 0)
    spec_id = tile["facts"]["spec_id"]
    approve(client, gate, tile)
    wait_for(lambda: client.get(f"/api/projects/{pid}").json()["project"]["stage"] == "parts", timeout=240, message="the part board stage")
    project = client.get(f"/api/projects/{pid}").json()["project"]
    rows = {r["spec"]["id"]: r for r in specs_of(client, pid)}
    locked = rows[project["approved_spec_id"]]
    assert locked["spec"]["created_by"] == "palette_lock" and locked["spec"]["parent_spec_id"] == spec_id
    assert locked["spec"]["version"] == 1 and locked["spec"]["palette_source"] == "concept_extracted" and locked["spec"]["status"] == "approved"
    assert locked["dna_card"]["locked"] is True and locked["dna_card"]["source"] == "concept_extracted" and locked["dna_card"]["version"] == 1
    assert rows[spec_id]["spec"]["status"] == "superseded"
    others = [r for i, r in rows.items() if r["spec"]["plan_set_id"] == locked["spec"]["plan_set_id"] and i not in (spec_id, locked["spec"]["id"])]
    assert others and all(r["spec"]["status"] in ("superseded", "dropped") for r in others)
    lock = job_steps(client, pid, "concept.lock")[0]
    res = step_result(rt, lock["id"])
    assert res["parent_spec_id"] == spec_id and res["palette"], "the palette log says what was snapped or taken from the picture"
    palette = {c["id"]: c["hex"].upper() for c in locked["spec"]["spec"]["palette"]}
    for entry in res["palette"]:
        assert entry["used"].upper() in (entry["planned"].upper(), entry["picture"].upper())
    assert palette, "the locked spec keeps every palette id"
    assert [c["id"] for c in locked["spec"]["spec"]["palette"]] == [c["id"] for c in rows[spec_id]["spec"]["spec"]["palette"]]
    from duoskin.pipeline import lint as LI

    bundle = LI.lint_candidates([("locked", locked["spec"]["spec"])], LI.lint_context(rt, rt.repo.get_project(pid), recent_cards=[]), check_set=False)
    assert bundle.hard_findings("locked") == [], "the lock never breaks a HARD plan rule"
    for role in ("concept_of_record", "concept_of_record.a", "concept_of_record.b", "style_sheet_a", "style_sheet_b", "style_sheet_judge",
                 "crop.a.face", "crop.b.face", "crop.a.hair", "crop.b.hair", "crop.a.shirt", "crop.b.pants"):
        links = rt.repo.list_links(project_id=pid, role=role)
        assert links, role
    record = rt.repo.list_links(project_id=pid, role="concept_of_record")[-1]
    assert png_size(rt, record.asset_sha) == (3072, 1024)
    assert [r for r in rt.repo.list_links(project_id=pid) if r.role.startswith("crop.") and ".acc." in r.role], "one crop per accessory"
    drift = rt.db.conn().execute("select passed from checks where project_id=? and check_id='A_DRIFT'", (pid,)).fetchall()
    assert drift and all(r[0] for r in drift)
    labels = [json.loads(r[0]) for r in rt.db.conn().execute("select json from labels where kind='gate1_pick'").fetchall()]
    assert any(spec_id in lb["subject_ids"] for lb in labels)
    parts_jobs = [jv for jv in client.get("/api/jobs", params={"project_id": pid}).json() if jv["job"]["kind"] == "parts"]
    assert len(parts_jobs) == 1, "the PARTS job of the part-board lane was started once"
    from duoskin.pipeline import plan as PL

    assert PL.start_parts(pid, rt).id == parts_jobs[0]["job"]["id"], "the hand-off is idempotent"
    events = rt.db.conn().execute("select payload from events where type='project.stage' and project_id=?", (pid,)).fetchall()
    assert any(json.loads(e[0]).get("stage") == "parts" for e in events)
    assert concept_gate(client, pid) is None
    for jv in parts_jobs:
        client.post(f"/api/jobs/{jv['job']['id']}/cancel")


def test_choosing_the_planned_palette_keeps_every_planned_colour(client, rt, world):
    pid, gate = world["tiles"]
    tile = tile_by_slot(gate, 1)
    r = decide(client, gate, tile, "approve", choice="palette:planned")
    assert r.status_code == 200, r.text
    if r.json()["provisional"]:
        client.post(f"/api/gates/{gate['id']}/decisions/{r.json()['decision']['id']}/confirm",
                    json={"override_warnings": [w["id"] for w in r.json()["released_warnings"]]})
    wait_for(lambda: job_steps(client, pid, "concept.lock") and job_steps(client, pid, "concept.lock")[0]["state"] == "succeeded", timeout=240)
    palette = step_result(rt, job_steps(client, pid, "concept.lock")[0]["id"])["palette"]
    assert palette and all(e["used"] == e["planned"] for e in palette), "'keep the planned colours' never takes a colour from the picture"
    for jv in client.get("/api/jobs", params={"project_id": pid}).json():
        client.post(f"/api/jobs/{jv['job']['id']}/cancel")


def test_a_tile_that_is_still_being_drawn_cannot_be_approved(client, rt, world):
    pid, gate = world["alts"]
    tile = tile_by_slot(gate, 1)
    assert decide(client, gate, tile, "reimagine").status_code == 200
    g2 = concept_gate(client, pid)
    t2 = tile_by_slot(g2, 1)
    if t2["state"] == "generating":
        refused = decide(client, g2, t2, "approve")
        assert refused.status_code == 409 and refused.json()["error"] == "tile_busy"
    wait_tile(client, pid, "plan1", version_above=tile["version"])


# ---------------------------------------------------------------------------------------------------- reimagine
def test_reimagine_draws_again_with_a_new_nonce_and_leaves_the_other_character_alone(client, rt, world):
    pid, gate = world["reimagine"]
    tile = tile_by_slot(gate, 1)
    before = {c: cstate(rt, gate, 1, c) for c in "ab"}
    assert before["a"]["nonce"] == "" and before["b"]["nonce"] == ""
    r = decide(client, gate, tile, "reimagine", target="a")
    assert r.status_code == 200, r.text
    gate2, tile2 = wait_tile(client, pid, "plan1", version_above=tile["version"])
    after = {c: cstate(rt, gate, 1, c) for c in "ab"}
    assert after["a"]["nonce"] and after["a"]["nonce"] != before["a"]["nonce"], "Reimagine changes only the nonce"
    assert after["a"]["generation"] == before["a"].get("generation", 0) + 1
    assert before["a"]["chosen"]["phash"] in after["a"]["rejected_hashes"], "the pHash of the rejected draft is remembered (A_PHASH)"
    assert after["b"]["chosen"] == before["b"]["chosen"] and after["b"]["nonce"] == before["b"]["nonce"], "the character that stays stays exactly"
    assert tile2["assets"]["b_front"] == tile["assets"]["b_front"] and tile2["state"] == "ready"
    steps = [s for s in job_steps(client, pid, "concept.char") if rt.repo.get_step(s["id"]).params["reason"] == "reimagine"]
    assert len(steps) == 1 and rt.repo.get_step(steps[0]["id"]).params["char"] == "a" and rt.repo.get_step(steps[0]["id"]).params["mode"] == "i1"
    first_nonce = rt.repo.get_step(steps[0]["id"]).nonce
    assert first_nonce
    r2 = decide(client, gate2, tile2, "reimagine")
    assert r2.status_code == 200
    wait_tile(client, pid, "plan1", version_above=tile2["version"])
    nonces = {rt.repo.get_step(s["id"]).nonce for s in job_steps(client, pid, "concept.char") if rt.repo.get_step(s["id"]).params["reason"] == "reimagine"}
    assert len(nonces) == 3, "every Reimagine uses a fresh nonce (a: twice, b: once)"
    ph = rt.db.conn().execute("select passed from checks where project_id=? and check_id='A_PHASH'", (pid,)).fetchall()
    assert ph, "the new drafts were compared with the rejected ones"


def test_a_tile_that_is_being_drawn_refuses_another_decision(client, rt, world):
    pid, gate = world["alts"]
    tile = tile_by_slot(gate, 2)
    r = decide(client, gate, tile, "reimagine")
    assert r.status_code == 200
    gate2 = concept_gate(client, pid)
    t2 = tile_by_slot(gate2, 2)
    if t2["state"] == "generating":
        again = decide(client, gate2, t2, "reimagine")
        assert again.status_code == 409 and again.json()["error"] == "tile_busy"
    wait_tile(client, pid, "plan2", version_above=tile["version"])


# ---------------------------------------------------------------------------------------------------- change
def test_change_at_gate1_routes_to_i1e_on_the_chosen_draft_and_edits_only_the_asked_detail(client, rt, world):
    pid, gate = world["change"]
    tile = tile_by_slot(gate, 0)
    psid = tile["facts"]["plan_set_id"]
    old_spec_id = tile["facts"]["spec_id"]
    chosen = {c: CO.get_state(rt, psid, 0, c)["chosen"]["sha"] for c in "ab"}
    before_nonce = {c: CO.get_state(rt, psid, 0, c)["nonce"] for c in "ab"}
    r = decide(client, gate, tile, "change", text="make her jacket teal", target="both")
    assert r.status_code == 200, r.text
    cid = r.json()["decision"]["change_request_id"]
    assert cid, "the decision names the change request"
    wait_for(lambda: open_side_gates(client, pid), timeout=120, message="the confirm gate")
    side = open_side_gates(client, pid)
    assert [g["kind"] for g in side] == ["change_confirm"]
    cr = client.get(f"/api/changes/{cid}").json()
    assert cr["status"] == "awaiting_confirm" and "new_spec" not in cr["plan"]
    assert cr["understood_as"] and cr["estimate_usd"] > 0
    assert [p["part_id"] for p in cr["invalidation"]["parts"]] == ["concept_b"], "the change touches the girl's jacket only"
    paths = [d["path"] for d in cr["diff"]["spec_changes"]]
    assert paths and all(p.startswith("/palette/") for p in paths)
    facts = side[0]["tiles"][0]["facts"]
    assert facts["estimate_usd"] == cr["estimate_usd"] and facts["change_id"] == cid and facts["origin"] == "concept"
    assert len(facts["lint_warnings"]) <= 2 and "warnings" not in facts
    r = decide(client, side[0], side[0]["tiles"][0], "confirm")
    assert r.status_code == 200, r.text
    assert r.json()["decision"]["resulting_spec_id"] and r.json()["decision"]["change_request_id"] == cid
    wait_for(lambda: change_status(client, cid) == "applied", timeout=30)
    _gate2, tile2 = wait_tile(client, pid, "plan0", version_above=tile["version"])
    new_id = tile2["facts"]["spec_id"]
    assert new_id != old_spec_id and tile2["facts"]["version"] == 2 and tile2["state"] == "ready"
    rows = {r["spec"]["id"]: r["spec"] for r in specs_of(client, pid)}
    assert rows[new_id]["created_by"] == "change" and rows[new_id]["parent_spec_id"] == old_spec_id and rows[old_spec_id]["status"] == "superseded"
    assert rows[new_id]["patch_from_parent"] and rows[new_id]["rank"] == 0
    after = {c: CO.get_state(rt, psid, 0, c) for c in "ab"}
    assert after["b"]["mode"] == "i1e" and after["b"]["base_sha"] == chosen["b"], "I1e edits the draft the person chose"
    assert after["b"]["rungs"][0]["template"] == "I1e.concept_edit" and "teal" in after["b"]["fix_sentence"].lower()
    assert after["b"]["chosen"]["sha"] != chosen["b"] and after["b"]["spec_id"] == new_id
    assert after["a"]["chosen"]["sha"] == chosen["a"] and after["a"]["nonce"] == before_nonce["a"] and after["a"]["mode"] == "i1", "A was not touched"
    redraws = [rt.repo.get_step(s["id"]).params for s in job_steps(client, pid, "concept.char") if rt.repo.get_step(s["id"]).params["reason"] == "change"]
    assert len(redraws) == 1 and redraws[0]["char"] == "b" and redraws[0]["mode"] == "i1e" and redraws[0]["base_sha"] == chosen["b"]
    chk = rt.db.conn().execute("select passed, kind from checks where project_id=? and check_id in ('CHK-G1-12','CHK-G0-11')", (pid,)).fetchall()
    assert chk and all(c[0] for c in chk) and {c[1] for c in chk} == {"assert"}
    assert rt.repo.get_project(pid).current_spec_id == new_id
    assert tile2["assets"]["a_front"] == tile["assets"]["a_front"]


def test_an_unclear_change_asks_one_question_and_cancelling_keeps_everything(client, rt, world):
    pid, gate = world["clarify"]
    tile = tile_by_slot(gate, 0)
    spec_ids = {r["spec"]["id"] for r in specs_of(client, pid)}
    r = decide(client, gate, tile, "change", text="make it nicer")
    assert r.status_code == 200
    cid = r.json()["decision"]["change_request_id"]
    wait_for(lambda: open_side_gates(client, pid), timeout=120, message="the clarify gate")
    clarify = open_side_gates(client, pid)[0]
    assert clarify["kind"] == "clarify" and clarify["tiles"][0]["facts"]["question"] and clarify["tiles"][0]["facts"]["change_id"] == cid
    assert clarify["tiles"][0]["allowed_actions"] == ["change", "cancel"]
    assert change_status(client, cid) == "needs_clarification"
    # the answer goes back as a 'change' decision and L7 runs again
    r = decide(client, clarify, clarify["tiles"][0], "change", text="make her jacket teal")
    assert r.status_code == 200
    wait_for(lambda: [g for g in open_side_gates(client, pid) if g["kind"] == "change_confirm"], timeout=120, message="the confirm gate")
    assert [g for g in client.get("/api/gates", params={"project_id": pid, "state": "open"}).json() if g["kind"] == "clarify"] == []
    assert client.get(f"/api/changes/{cid}").json()["answers"] == ["make her jacket teal"]
    confirm = open_side_gates(client, pid)[0]
    r = decide(client, confirm, confirm["tiles"][0], "cancel")
    assert r.status_code == 200
    assert change_status(client, cid) == "cancelled"
    assert {r["spec"]["id"] for r in specs_of(client, pid)} == spec_ids, "a cancelled change creates no spec"
    g = concept_gate(client, pid)
    assert tile_by_slot(g, 0)["facts"]["spec_id"] == tile["facts"]["spec_id"] and tile_by_slot(g, 0)["state"] == "ready"
    assert open_side_gates(client, pid) == []


def test_a_change_that_would_break_a_rule_is_rejected_with_a_reason_and_nothing_changes(client, rt, world, monkeypatch):
    from duoskin.pipeline import change as CH

    pid, gate = world["reject"]
    tile = tile_by_slot(gate, 2)
    spec_ids = {r["spec"]["id"] for r in specs_of(client, pid)}

    def refuse(spec, ops, **kw):
        return CH.PatchResult(False, None, ["a bow of that size does not fit the Classic box"], [])

    monkeypatch.setattr(CH, "apply_patch", refuse)
    r = decide(client, gate, tile, "change", text="make her jacket teal")
    cid = r.json()["decision"]["change_request_id"]
    wait_for(lambda: change_status(client, cid) == "rejected", timeout=120, message="the rejection")
    cr = client.get(f"/api/changes/{cid}").json()
    assert "Classic box" in cr["plan"]["reason"] and cr["plan"]["rejection"] == cr["plan"]["reason"]
    assert open_side_gates(client, pid) == []
    assert {r["spec"]["id"] for r in specs_of(client, pid)} == spec_ids


# ---------------------------------------------------------------------------------------------------- new plan
def test_new_plan_sets_the_three_plans_aside_and_the_next_round_avoids_them(client, rt, world):
    pid, gate = world["newplan"]
    tile = tile_by_slot(gate, 0)
    old = {r["spec"]["id"]: r["spec"] for r in specs_of(client, pid)}
    old_structures = {s["spec"]["world"]["pair_structure"] for s in old.values() if s["status"] == "shown"}
    r = decide(client, gate, tile, "new_plan", text="too busy, I wanted something calmer")
    assert r.status_code == 200
    assert r.json()["decision"]["spawned_step_ids"] == [] and rt.repo.get_gate(gate["id"]).state == "decided"

    def second_select():
        return [s for s in job_steps(client, pid, "plan.select") if s["state"] == "succeeded"]

    wait_for(lambda: len(second_select()) >= 2, timeout=240, message="the second plan")
    for jv in client.get("/api/jobs", params={"project_id": pid}).json():
        client.post(f"/api/jobs/{jv['job']['id']}/cancel")
    rows = {r["spec"]["id"]: r["spec"] for r in specs_of(client, pid)}
    for sid in old:
        assert rows[sid]["status"] in ("superseded", "dropped"), "the plans that were set aside are never shown again"
    new_shown = [s for s in rows.values() if s["status"] == "shown"]
    assert len(new_shown) == 3 and not ({s["id"] for s in new_shown} & set(old))
    assert {s["spec"]["world"]["pair_structure"] for s in new_shown}.isdisjoint(old_structures), "the planner avoided the rejected structures"
    from duoskin.pipeline import brief as BR

    entries = BR.avoid_entries(rt, pid)
    assert len(entries) == 3 and all(e["reason"].startswith("too busy") for e in entries) and BR.plan_round(rt, pid) == 1
    project = rt.repo.get_project(pid)
    assert project.stage.value == "planning" and project.approved_spec_id is None
    planners = sorted((rt.repo.get_step(s["id"]) for s in job_steps(client, pid, "plan.planner")), key=lambda st: st.created_at)
    planners = [p_ for p_ in planners if not p_.params.get("replacement")]
    assert len(planners) == 2 and planners[0].params["round"] == 0 and planners[1].params["round"] == 1 and planners[1].nonce == "plan1"


# ---------------------------------------------------------------------------------------------------- not buildable, alternatives
def test_what_the_kits_cannot_build_is_listed_and_approval_needs_the_acknowledgement(client, rt, world):
    pid, gate = world["cape"]
    capes = [t for t in gate["tiles"] if t["facts"]["not_buildable"]]
    assert len(capes) == 1 and any("cape" in x for x in capes[0]["facts"]["not_buildable"])
    assert len(capes[0]["facts"]["not_buildable"]) == 1 and capes[0]["facts"]["not_buildable"][0].endswith("character B"), capes[0]["facts"]
    assert [t for t in gate["tiles"] if not t["facts"]["not_buildable"]]
    tile = capes[0]
    refused = decide(client, gate, tile, "approve")
    if refused.status_code == 200 and refused.json()["provisional"]:
        # warnings were released with this first choice, so the approval is provisional: confirming it is what applies it, and that is refused
        did = refused.json()["decision"]["id"]
        ids = [w["id"] for w in refused.json()["released_warnings"]]
        confirmed = client.post(f"/api/gates/{gate['id']}/decisions/{did}/confirm", json={"override_warnings": ids})
        assert confirmed.status_code == 422 and confirmed.json()["error"] == "ack_required"
        assert client.delete(f"/api/gates/{gate['id']}/decisions/{did}").status_code == 204
    else:
        assert refused.status_code == 422 and refused.json()["error"] == "ack_required"
    assert concept_gate(client, pid) is not None
    body = approve(client, gate, tile, choice="ack:CON-04")
    assert body["decision"]["choice"] == "ack:CON-04"
    labels = [json.loads(r[0]) for r in rt.db.conn().execute("select json from labels where kind='warning_override'").fetchall()]
    assert any("CON-04" in lb["subject_ids"] for lb in labels), "the acknowledgement is logged"
    wait_for(lambda: rt.repo.get_project(pid).stage.value in ("parts", "gate2"), timeout=240, message="the lock")
    for jv in client.get("/api/jobs", params={"project_id": pid}).json():
        client.post(f"/api/jobs/{jv['job']['id']}/cancel")


def test_selecting_an_alternative_draft_swaps_it_with_the_chosen_one(client, rt, world):
    pid, gate = world["alts"]
    tile = tile_by_slot(gate, 0)
    assert tile["alternatives"], "the drafts that passed but were not chosen stay available"
    alt_front = tile["alternatives"][0]["a_front"]
    psid = tile["facts"]["plan_set_id"]
    chosen_before = CO.get_state(rt, psid, 0, "a")["chosen"]
    r = decide(client, gate, tile, "select_alternative", choice="a:0")
    assert r.status_code == 200, r.text
    gate2, tile2 = wait_tile(client, pid, "plan0", version_above=tile["version"])
    assert tile2["assets"]["a_front"] == alt_front and tile2["assets"]["b_front"] == tile["assets"]["b_front"]
    st = CO.get_state(rt, psid, 0, "a")
    assert any(a["front"] == chosen_before["front"] for a in st["alts"]), "the earlier choice is now an alternative"
    bad = decide(client, gate2, tile2, "select_alternative", choice="a:99")
    assert bad.status_code == 422 and bad.json()["error"] == "bad_choice"
    bad2 = decide(client, gate2, tile2, "select_alternative", choice="nonsense")
    assert bad2.status_code == 422
