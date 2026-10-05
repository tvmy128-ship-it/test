"""Regression tests for what clicking through the real app (brief to export, tests/e2e_ui) turned up in the web pages:

* a failed step must be visible (and retryable) on the pages that wait for it, instead of "the parts are being made" for ever;
* a change that is waiting for the user's OK must have a way back after a reload (the dialogs of changeflow.js only open from the click);
* the confirm dialog names a colour by its role and a concept picture by its character, never by palette position or id;
* Plan 1 is the same plan on the plan page and at Gate 1; the parts page does not say "no parts" right after a concept was approved.
"""
from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from duoskin.engine.gates import allowed_actions_for
from duoskin.engine.testkit import wait_for
from duoskin.models.common import new_id, sha256_of, utcnow
from duoskin.models.gate import Gate, GateKind, GateTile
from duoskin.models.project import Stage
from duoskin.models.spec_record import SpecRecord

from . import seed
from .test_brief_plan_jobs import api


def waiting_change_gate(live, p, kind: GateKind, facts: dict):
    j = seed.job(live.rt, p.id)
    tile = GateTile(tile_id="chg_tile", label="Change", facts=facts, allowed_actions=allowed_actions_for(kind))
    return live.rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=kind, tiles=[tile], opened_at=utcnow()))


def test_a_failed_step_is_shown_on_the_parts_page_and_can_be_retried(ui, live, fake_plan):
    fake_plan.fail_lint = True
    fake_plan.release.set()
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    job = api(live, "POST", f"/api/projects/{p.id}/plan").json()
    wait_for(lambda: api(live, "GET", f"/api/jobs/{job['id']}").json()["job"]["state"] == "failed", 10, message="the job to fail")
    live.rt.repo.set_project_stage(p.id, Stage.PARTS, bus=live.rt.bus)
    ui.goto(f"/p/{p.id}/board")
    panel = ui.page.locator(".failed-steps")
    expect(panel).to_contain_text("One step did not work")
    expect(panel).to_contain_text("The rules check could not run. Try again in a moment.")           # the hint, not the exception
    assert "lint blew up" not in panel.inner_text() and "Traceback" not in ui.page.locator("main").inner_text()
    ui.shot("review_failed_step_panel")
    fake_plan.fail_lint = False
    panel.get_by_role("button", name="Try again").click()
    expect(ui.page.locator(".failed-steps")).to_have_count(0, timeout=15000)                          # the job finished: nothing failed any more
    ui.no_errors()


def test_the_parts_page_does_not_say_there_are_no_parts_right_after_the_concept_was_approved(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)            # the concept gate is decided, the palette lock is still running
    ui.goto(f"/p/{p.id}/board")
    expect(ui.page.get_by_role("heading", name="Locking in your concept")).to_be_visible()
    assert "No parts yet" not in ui.page.locator("main").inner_text()
    assert "Pick a concept" not in ui.page.locator("main").inner_text()                                 # the choice was made; no button back to it


def test_a_change_waiting_for_the_user_can_be_reopened_after_a_reload(ui, live, plain_gates):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)
    seed.concept_gate(live.rt, p)
    chg = {"id": "chg_7", "project_id": p.id, "status": "awaiting_confirm", "plan": {"origin": {"spec_id": "spc_none"}}, "estimate_usd": 0.14,
           "invalidation": {"parts": [{"part_id": "concept_b", "effect": "regenerate"}]}}
    ui.page.route(re.compile(r"/api/changes/chg_7$"), lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(chg)))
    gate = waiting_change_gate(live, p, GateKind.CHANGE_CONFIRM, {"change_id": "chg_7", "understood_as": "Change the top colour of character b to teal.",
                                                                  "diff": {"spec_changes": [{"path": "/palette/2/hex", "old": "#9FE0F4", "new": "#1E9696"}]},
                                                                  "estimate_usd": 0.14, "lint_warnings": []})
    ui.goto(f"/p/{p.id}/gate1")                                                                         # a fresh page: no dialog is open
    waiting = ui.page.locator(".change-waiting")
    expect(waiting).to_contain_text("Your change is waiting for your OK")
    expect(waiting).to_contain_text("We understood it as: Change the top colour of character b to teal.")
    ui.shot("review_change_waiting_panel")
    waiting.get_by_role("button", name="Review the change").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Confirm your change")
    expect(dlg).to_contain_text("Character B's concept picture")                                         # not "Concept b"
    assert "concept_b" not in dlg.inner_text() and "Colour p3" not in dlg.inner_text()
    dlg.get_by_role("button", name="Cancel, keep it as it is").click()
    wait_for(lambda: live.rt.repo.list_decisions(gate.id), 5, message="the cancel")
    assert live.rt.repo.list_decisions(gate.id)[0].action.value == "cancel"
    expect(ui.page.locator(".change-waiting")).to_have_count(0, timeout=10000)
    ui.no_errors()


def test_a_waiting_question_can_be_reopened_too(ui, live, plain_gates):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)
    seed.concept_gate(live.rt, p)
    waiting_change_gate(live, p, GateKind.CLARIFY, {"question": "Do you mean the jacket on A or the hood on B?", "change_id": "chg_8"})
    ui.goto(f"/p/{p.id}/gate1")
    waiting = ui.page.locator(".change-waiting")
    expect(waiting).to_contain_text("A quick question about your change")
    expect(waiting).to_contain_text("Do you mean the jacket on A or the hood on B?")
    waiting.get_by_role("button", name="Answer the question").click()
    expect(ui.page.get_by_role("dialog")).to_contain_text("Do you mean the jacket on A or the hood on B?")


def test_diff_labels_name_a_colour_by_its_role(ui, live):
    ui.goto("/")
    palette = [{"id": f"p{i + 1}", "role": r} for i, r in enumerate(["a_main", "a_second", "b_main", "b_second", "accent", "neutral_light", "neutral_dark", "hair_a", "hair_b", "modesty"])]
    got = ui.page.evaluate("""async (palette) => {
        const { labelForPath } = await import('/web/components/diff.js');
        return [labelForPath('/palette/2/hex', palette), labelForPath('/palette/p10', palette), labelForPath('/palette/2/hex'), labelForPath('/a/dna/shape_language', palette)];
    }""", palette)
    assert got[0] == "Character B's main colour"
    assert got[1] == "The under-layer colour"
    assert got[2] == "Colour 3"                                   # without the palette: a number the user can count, never a zero-based index
    assert got[3].startswith("Character A")


def test_the_plan_page_numbers_the_plans_the_way_gate_1_does(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.PLANNING)
    for plan_index, rank, theme in [(0, 2, "third best"), (1, 0, "best of all"), (2, 1, "second best")]:
        live.rt.repo.add_spec(SpecRecord(id=new_id("spc"), project_id=p.id, plan_set_id="set1", plan_index=plan_index,
                                         spec={"is_wildcard": False, "world": {"theme": theme}}, sha256=sha256_of({"i": plan_index, "p": p.id}), rank=rank))
    ui.goto(f"/p/{p.id}/plan")
    cards = ui.page.locator(".plan-card")
    expect(cards).to_have_count(3)
    assert [c.split("\n")[0] for c in cards.all_inner_texts()] == ["Plan 1", "Plan 2", "Plan 3"]
    assert [ui.page.locator(".plan-card .plan-theme").nth(i).inner_text() for i in range(3)] == ["best of all", "second best", "third best"]


def test_a_plan_with_no_usable_drawing_does_not_claim_the_drawings_are_still_being_made(ui, live, plain_gates):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)
    gate = seed.concept_gate(live.rt, p, failed_plan=2)
    g = live.rt.repo.get_gate(gate.id)                                                                  # both characters failed: nothing was drawn at all
    live.rt.repo.save_gate(g.model_copy(update={"tiles": [t.model_copy(update={"assets": {}}) if t.tile_id == "plan2" else t for t in g.tiles]}))
    ui.goto(f"/p/{p.id}/gate1")
    card = ui.page.locator(".plan-tile").nth(2)
    expect(card.locator(".hard-fails")).to_contain_text("Needs fixing first")
    expect(card.locator(".tile-wait")).to_contain_text("nothing to look at")
    assert "still being made" not in card.inner_text()


def test_a_failed_required_check_is_said_in_words_not_as_a_check_code_or_a_measurement(ui, live):
    ui.goto("/")
    got = ui.page.evaluate("""async () => {
        const { hardFailuresOf } = await import('/web/components/tile.js');
        const f = (list) => hardFailuresOf({ facts: { hard_failures: list } });
        return [
          f([{ id: 'A_PALETTE', evidence: 'clusters off palette: #70b5e6(13.8), #375a74(27.3)' }]),
          f([{ id: 'F_LINE_SKIN', evidence: 'lash on tone_1: dE 8.6 < 20' }, { id: 'CHK-A17', evidence: 'F_LID_COVERS: cover 1.0000' }]),
          f([{ id: 'A_PALETTE', evidence: 'The shirt colour is too far from the palette.' }]),          // evidence that is already a sentence stays
          f([{ id: 'SOME_NEW_CHECK', evidence: 'something specific' }]),                                      // an unknown check keeps its own words
          f([{ id: 'SOME_NEW_CHECK' }]),
        ];
    }""")
    assert got[0] == ["Only the colours of the plan"]
    assert got[1] == ["Face lines stand out on every skin tone", "The 2D face is complete"]
    assert got[2] == ["The shirt colour is too far from the palette."]
    assert got[3] == ["something specific"] and got[4] == ["A required check did not pass"]
    for line in [line for lines in got for line in lines]:
        assert "#" not in line and "dE" not in line and "_" not in line.replace("A required check did not pass", "")


def test_gate_3_says_the_measured_facts_in_words_whatever_shape_the_pipeline_sends(ui, live, plain_gates):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    seed.final_gate(live.rt, p, candidates=1, facts_extra={
        "checks": {"total": 12, "passed": 11, "hard_failures": [{"id": "F_LINE_SKIN", "evidence": "lash on tone_1: dE 8.6 < 20"}]},
        "clone_evidence": "tripped: none; nearest=0.31; degraded=true",
        "ip": {"ok": False, "unsure": ["R_TEXT"], "fails": ["R_LOGO"]}})
    ui.goto(f"/p/{p.id}/gate3")
    reviews = ui.page.locator(".candidate .reviews").inner_text()
    assert "11 of 12 required checks passed" in reviews and "Not passed: Face lines stand out on every skin tone" in reviews
    assert "The two characters look different enough from each other." in reviews and "lighter version" in reviews
    assert "Something in the pictures may look like a brand, a known character or writing." in reviews
    for raw in ("tripped", "nearest=", "dE ", "F_LINE_SKIN", "R_LOGO", "hard_failures", "{", "}"):
        assert raw not in reviews, raw
    ui.shot("review_gate3_facts_in_words", full=False)
    ui.no_errors()


def _blocked_export(preview: bool) -> dict:
    step = {"step_id": "confirm_final", "text": "Check the final pictures", "ticked": False, "locked": False}
    item = {"item_id": "a_shirt", "character": "a", "type": "Shirt", "channel_text": "Upload as a shirt.", "steps": [step]}
    return {"status": "blocked", "reason": "CHK-E01: a part still comes from a practice (mock) source", "mock": True, "banners": [], "version": 0,
            "preview": {"checklist": {"items": [item]}, "items": [{"item_id": "a_shirt", "character": "a", "type": "Shirt"}]} if preview else None}


def test_the_export_preview_lists_the_kit_in_demo_mode_and_says_why_it_cannot_be_uploaded(ui, live):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    ui.page.route(re.compile(r"/api/exports/"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(_blocked_export(True))))
    ui.page.route(re.compile(r"/api/health$"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "demo": True})))
    ui.goto(f"/p/{p.id}/export")
    expect(ui.page.get_by_role("heading", name="Export preview: nothing was written")).to_be_visible()
    main = ui.page.locator("main")
    expect(main).to_contain_text("practice (demo) services")
    expect(main).to_contain_text("Demo mode is on")
    expect(main.get_by_role("heading", name="What the kit would contain")).to_be_visible()
    expect(main.locator(".check-item")).to_have_count(1)
    expect(main.locator(".check-item input[type=checkbox]")).to_be_disabled()                       # nothing was written: nothing can be ticked
    expect(main.get_by_role("link", name="Back to the final pick")).to_be_visible()
    expect(main.get_by_role("link", name="Back to the duo")).to_be_visible()
    assert "CHK-E01" not in main.inner_text() and "a_shirt" not in main.inner_text()
    ui.shot("review_export_preview_demo", full=False)


def test_the_blocked_export_in_normal_mode_has_no_demo_wording_and_no_preview(ui, live):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    ui.page.route(re.compile(r"/api/exports/"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(_blocked_export(True))))
    ui.page.route(re.compile(r"/api/health$"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "demo": False})))
    ui.goto(f"/p/{p.id}/export")
    expect(ui.page.get_by_role("heading", name="The kit is not ready to upload")).to_be_visible()
    main = ui.page.locator("main")
    expect(main).to_contain_text("practice stand-ins")
    assert "Demo mode is on" not in main.inner_text() and ui.page.locator(".check-item").count() == 0


def test_the_cost_bar_and_the_costs_page_call_demo_prices_pretend(ui, live):
    ui.goto("/")
    got = ui.page.evaluate("""async () => {
        const { renderCostBar } = await import('/web/components/costbar.js');
        const text = (demo) => { const host = document.createElement('div'); renderCostBar(host, { demo, todayUsd: 1.5, project: { name: 'x', spent_usd: 2, settings: { budget_usd: 15 } } }); return host.innerText || host.textContent; };
        return [text(true), text(false)];
    }""")
    assert "Pretend spend on this duo" in got[0] and "Pretend spend today" in got[0] and "Spent on this duo" not in got[0]
    assert "Spent on this duo" in got[1] and "Today, all duos" in got[1] and "Pretend" not in got[1]


def test_a_finished_build_step_shows_no_log_line_and_a_thin_model_is_explained(ui, live):
    from datetime import timedelta

    from duoskin.models.job import JobKind, Step, StepState
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.BUILDING)
    seed.board_gate(live.rt, p)
    j = seed.job(live.rt, p.id, JobKind.BUILD)
    now = utcnow()
    live.rt.repo.insert_steps([
        Step(id="stp_done", job_id=j.id, project_id=p.id, part_id="a.shirt", kind="clothing.compose", state=StepState.SUCCEEDED, progress=1.0, message="4 draft(s) by I5", created_at=now, finished_at=now),
        Step(id="stp_wait", job_id=j.id, project_id=p.id, part_id="a.hair", kind="manual.wait", state=StepState.WAITING_USER, message="waiting_user: inbox", created_at=now, not_before=now + timedelta(hours=1))])
    ui.goto(f"/p/{p.id}/build")
    main = ui.page.locator("main").inner_text()
    assert "4 draft(s) by I5" not in main and "waiting_user" not in main
    expect(ui.page.locator(".build-part", has_text="A · Hair")).to_contain_text("Waiting for your 3D file.")


def test_gate_3_does_not_scroll_sideways_on_a_narrow_window(ui, live, plain_gates):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    seed.final_gate(live.rt, p, candidates=1, strip_width=600)             # shown at twice its size: 1200 px, wider than the window
    ui.page.set_viewport_size({"width": 700, "height": 900})
    ui.goto(f"/p/{p.id}/gate3")
    expect(ui.page.locator(".candidate")).to_have_count(1)
    ui.page.wait_for_timeout(400)
    widths = ui.page.evaluate("[document.documentElement.scrollWidth, window.innerWidth, document.querySelector('.phone-strip').scrollWidth, document.querySelector('.phone-strip').clientWidth]")
    assert widths[0] <= widths[1], f"the page scrolls sideways: {widths}"
    assert widths[3] <= widths[1]
