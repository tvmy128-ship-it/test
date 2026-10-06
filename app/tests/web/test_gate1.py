"""Gate 1 (concept) against the real gate lifecycle: cards, wildcard, DNA card, not-buildable acknowledgement, the choices
(Approve / Reimagine A, B or both / Change... / New plan) and the change flow (CLARIFY, CHANGE_CONFIRM, rejected)."""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from duoskin.engine.gates import allowed_actions_for
from duoskin.engine.testkit import wait_for
from duoskin.models.common import utcnow
from duoskin.models.gate import Gate, GateKind, GateTile
from duoskin.models.project import Stage

from . import seed

# the UI is tested against the engine's generic gate lifecycle, not the pipeline's appliers (those are tested by the pipeline track)
pytestmark = pytest.mark.usefixtures("plain_gates")

def setup(live, **kw):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1, spent=2.35)
    g = seed.concept_gate(live.rt, p, **kw)
    return p, g


def decisions(live, gate):
    return live.rt.repo.list_decisions(gate.id)


def follow_up_gate(live, p, kind, facts, label="Change"):
    j = seed.job(live.rt, p.id)
    tile = GateTile(tile_id="chg_tile", label=label, facts=facts, allowed_actions=allowed_actions_for(kind))
    return live.rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=kind, tiles=[tile], opened_at=utcnow()))


def test_three_cards_with_wildcard_dna_and_must_include(ui, live):
    p, _gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    cards = ui.page.locator(".plan-tile")
    expect(cards).to_have_count(3)
    assert cards.nth(1).locator(".badge.wild").inner_text() == "Wildcard"
    expect(ui.page.locator(".badge.wild")).to_have_count(1)                                 # exactly one card is the wildcard
    for i in range(3):
        expect(cards.nth(i).locator(".sheet-4up figure")).to_have_count(4)               # A front, A back, B front, B back
    first = cards.nth(0)
    expect(first.locator(".sheet-4up")).to_contain_text("front")
    expect(first.locator(".dna-mini")).to_contain_text("koi pond at dusk")
    expect(first.locator(".dna-mini")).to_contain_text("Round soft")
    expect(ui.page.locator(".gate-summary")).to_contain_text("Complement, Mirror, Same club")      # "Your brief was read as ..."
    expect(cards.nth(2).locator(".coverage")).to_contain_text("Missing")
    expect(first.locator(".coverage")).to_contain_text("Included")
    for i in range(3):                                                                        # every card has the three choices
        for name in ("Approve", "Reimagine", "Change…"):
            expect(cards.nth(i).get_by_role("button", name=name, exact=True)).to_be_visible()
    ui.shot("gate1_cards")
    ui.no_errors()


def test_a_plan_whose_character_could_not_be_drawn_is_explained_and_cannot_be_approved(ui, live):
    p, _gate = setup(live, failed_plan=2)
    ui.goto(f"/p/{p.id}/gate1")
    card = ui.page.locator(".plan-tile").nth(2)
    expect(card.locator(".sheet-4up figure")).to_have_count(2)                                # only A was drawn
    expect(card.locator(".hard-fails")).to_contain_text("Character B: None of the drawings passed the required checks.")
    assert "A_LEAK" not in card.inner_text()                                                   # plain words, not check codes
    expect(card.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
    expect(card.get_by_role("button", name="Reimagine", exact=True)).to_be_enabled()
    ui.shot("gate1_failed_character")


def test_no_soft_warnings_before_the_first_choice(ui, live):
    p, _gate = setup(live, warnings=True, not_buildable=False)
    ui.goto(f"/p/{p.id}/gate1")
    ui.page.wait_for_selector(".plan-tile")
    assert "Heads-up" not in ui.page.locator("main").inner_text()                           # the API withholds them until a choice is made
    assert "same orange" not in ui.page.content()


def test_not_buildable_panel_needs_the_tick_before_approve(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    first = ui.page.locator(".plan-tile").nth(0)
    panel = first.locator(".not-buildable")
    expect(panel).to_contain_text("Not buildable as drawn")
    expect(panel).to_contain_text("A's skirt flares out past the leg boxes")
    first.get_by_role("button", name="Approve", exact=True).click()
    expect(first.get_by_role("alert")).to_contain_text("tick the box")                     # not approved yet
    assert decisions(live, gate) == []
    first.get_by_label("I understand this part is built differently").check()
    first.get_by_role("button", name="Approve", exact=True).click()
    expect(ui.page).to_have_url(re.compile(rf"#/p/{p.id}/board$"))
    d = decisions(live, gate)
    assert len(d) == 1 and d[0].action.value == "approve" and d[0].tile_id == "plan0" and d[0].choice == "ack:CON-04"
    assert live.rt.repo.get_gate(gate.id).state == "decided"


def test_approve_a_card_without_not_buildable_items_needs_no_tick(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    second = ui.page.locator(".plan-tile").nth(1)
    expect(second.locator(".not-buildable")).to_have_count(0)
    second.get_by_role("button", name="Approve", exact=True).click()
    expect(ui.page).to_have_url(re.compile(rf"#/p/{p.id}/board$"))
    assert decisions(live, gate)[0].tile_id == "plan1" and decisions(live, gate)[0].choice is None


def test_reimagine_lets_you_keep_one_character(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    ui.page.locator(".plan-tile").nth(2).get_by_role("button", name="Reimagine", exact=True).click()
    dialog = ui.page.get_by_role("dialog")
    expect(dialog).to_contain_text("The one you keep stays exactly as it is")
    expect(dialog.get_by_label("Both characters")).to_be_checked()                          # default is both
    dialog.get_by_label("Character A only").check()
    ui.shot_element(ui.page.locator("dialog[open]").last, "gate1_reimagine_scope")
    dialog.get_by_role("button", name="Reimagine").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "reimagine" and d.target == "a" and d.tile_id == "plan2"
    assert live.rt.repo.get_gate(gate.id).state == "open"                                   # the gate stays open for another look


def test_new_plan_with_a_reason(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    ui.page.get_by_role("button", name="None of these: make a new plan").click()
    dialog = ui.page.get_by_role("dialog")
    dialog.get_by_label("What was wrong with these? (optional)").fill("too busy, I want calm")
    dialog.get_by_role("button", name="Make three new plans").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "new_plan" and d.text == "too busy, I want calm"
    expect(ui.page.get_by_role("heading", name="Nothing to pick right now")).to_be_visible()


def test_select_another_draft(ui, live):
    p, gate = setup(live)
    g = live.rt.repo.get_gate(gate.id)
    tiles = [t.model_copy(update={"alternatives": [{"a_front": seed.put(live.rt, seed.figure_png("plum", "front"))}]}) if t.tile_id == "plan0" else t for t in g.tiles]
    live.rt.repo.save_gate(g.model_copy(update={"tiles": tiles}))
    ui.goto(f"/p/{p.id}/gate1")
    first = ui.page.locator(".plan-tile").nth(0)
    first.get_by_text("Other drafts (1)").click()
    first.get_by_role("button", name="Use for A").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "select_alternative" and d.choice == "a:0"


def test_design_card_highlights_what_the_picture_uses_and_change_opens(ui, live):
    p, _gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    ui.page.locator(".plan-tile").nth(0).get_by_role("button", name="See the full design card").click()
    dialog = ui.page.get_by_role("dialog")
    expect(dialog.locator(".dna-char")).to_have_count(2)                                    # A and B side by side
    expect(dialog.locator(".dna-story")).to_contain_text("metadata only (never drawn)")
    used = dialog.locator(".dna-row.used")
    labels = sorted(" ".join(t.split()) for t in used.locator("dt").all_inner_texts())
    assert labels == sorted(["Shape style used for the picture", "Signature object used for the picture"] * 2)
    expect(dialog.locator(".swatches li")).to_have_count(4)
    ui.shot_element(ui.page.locator("dialog[open]").last, "gate1_design_card")
    dialog.get_by_label("Change Shape style").first.click()
    change = ui.page.get_by_role("dialog").last
    expect(change).to_contain_text("Describe the visible result; the app will show you the changes before anything runs.")
    expect(change.get_by_label("Both characters")).to_be_checked()
    ui.no_errors()


def test_change_flow_clarify_then_confirm_shows_the_diff_and_estimate(ui, live):
    p, gate = setup(live)
    chg = {"id": "chg_1", "project_id": p.id, "gate_id": gate.id, "text": "make her jacket teal", "status": "awaiting_confirm",
           "plan": {}, "invalidation": {"parts": [{"part_id": "a.print.top.0", "effect": "regenerate"}, {"part_id": "a.shirt", "effect": "recompose"}, {"part_id": "b.face", "effect": "recheck"}]},
           "estimate_usd": 0.42}
    ui.page.route(re.compile(r"/api/changes/chg_1$"), lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(chg)))
    ui.goto(f"/p/{p.id}/gate1")
    first = ui.page.locator(".plan-tile").nth(1)
    first.get_by_role("button", name="Change…", exact=True).click()
    dialog = ui.page.get_by_role("dialog")
    dialog.get_by_role("button", name="Show me the changes").click()
    expect(dialog.get_by_role("alert")).to_contain_text("say what you would like")             # empty text is refused kindly
    dialog.get_by_label("What should be different?").fill("make her jacket teal")
    dialog.get_by_label("Character A only").check()
    dialog.get_by_role("button", name="Show me the changes").click()
    expect(ui.page.get_by_role("dialog")).to_contain_text("Working out your change")
    wait_for(lambda: decisions(live, gate), 5, message="the change decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "change" and d.text == "make her jacket teal" and d.target == "a" and d.tile_id == "plan1"
    # L7 needs one more answer: a CLARIFY gate opens and the question is shown
    clar = follow_up_gate(live, p, GateKind.CLARIFY, {"question": "Do you mean the jacket on A or the hood on B?"}, "Question")
    q = ui.page.get_by_role("dialog")
    expect(q).to_contain_text("Do you mean the jacket on A or the hood on B?")
    q.get_by_label("Your answer").fill("the jacket on A")
    q.get_by_role("button", name="Send my answer").click()
    wait_for(lambda: len(decisions(live, clar)) == 1, 5, message="the answer")
    assert decisions(live, clar)[0].text == "the jacket on A" and decisions(live, clar)[0].action.value == "change"
    # then the confirm step: spec diff, parts to redo with their effect in words, the estimate
    confirm = follow_up_gate(live, p, GateKind.CHANGE_CONFIRM, {"change_id": "chg_1", "diff": {"spec_changes": [{"path": "/a/top/base_ref", "old": "#E4572E", "new": "#1B998B"}],
                                                                                           "dna_changes": []}, "lint_warnings": [{"id": "ws1", "text": "The teal is close to B's trousers."}]})
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Confirm your change")
    expect(dlg).to_contain_text("#E4572E")
    expect(dlg).to_contain_text("#1B998B")
    expect(dlg).to_contain_text("Draw again")
    expect(dlg).to_contain_text("Rebuild")
    expect(dlg).to_contain_text("Re-check")
    expect(dlg).to_contain_text("Estimated cost: $0.42")
    expect(dlg).to_contain_text("The teal is close to B's trousers.")
    assert "{" not in dlg.inner_text() and "regenerate" not in dlg.inner_text()           # no raw JSON, no code words
    ui.shot_element(ui.page.locator("dialog[open]").last, "gate1_change_confirm")
    dlg.get_by_role("button", name="Confirm the change").click()
    wait_for(lambda: len(decisions(live, confirm)) == 1, 5, message="the confirm")
    assert decisions(live, confirm)[0].action.value == "confirm"
    expect(ui.page.locator(".toast", has_text="Change confirmed")).to_have_count(1)
    ui.no_errors()


def test_change_flow_cancel_and_rejected(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    first = ui.page.locator(".plan-tile").nth(0)
    first.get_by_role("button", name="Change…", exact=True).click()
    ui.page.get_by_label("What should be different?").fill("give her a very long cape")
    ui.page.get_by_role("button", name="Show me the changes").click()
    wait_for(lambda: decisions(live, gate), 5, message="the change decision")
    confirm = follow_up_gate(live, p, GateKind.CHANGE_CONFIRM, {"diff": {"spec_changes": [{"path": "/a/top/hem", "old": "waist_tucked", "new": "hip_untucked"}]}})
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("waist tucked")
    expect(dlg).to_contain_text("hip untucked")                                              # enum words are spoken, not coded
    dlg.get_by_role("button", name="Cancel, keep it as it is").click()
    wait_for(lambda: len(decisions(live, confirm)) == 1, 5, message="the cancel")
    assert decisions(live, confirm)[0].action.value == "cancel"
    # a change that breaks a hard rule is shown with its reason and nothing is applied
    rejected = {"id": "chg_9", "status": "rejected", "plan": {"reason": "A cape that long would clip through the legs."}}
    ui.page.route(re.compile(r"/api/changes/chg_9$"), lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(rejected)))
    from duoskin.api import projects  # noqa: F401  (the real route stays untouched; only the change lookup is faked)
    from duoskin.engine.gates import ApplyResult
    first.get_by_role("button", name="Change…", exact=True).click()
    ui.page.get_by_label("What should be different?").fill("a cape down to the floor")
    live.rt.gates.register_applier(GateKind.CONCEPT, lambda ac: ApplyResult(change_request_id="chg_9", close_gate=False))
    ui.page.get_by_role("button", name="Show me the changes").click()
    expect(ui.page.get_by_role("dialog")).to_contain_text("That change cannot be made", timeout=15000)
    expect(ui.page.get_by_role("dialog")).to_contain_text("A cape that long would clip through the legs.")
    ui.no_errors()


def test_palette_confirm_dialog_defaults_to_the_picture_and_answers_the_gate(ui, live):
    p, _gate = setup(live)
    ui.goto(f"/p/{p.id}/gate1")
    ui.page.wait_for_selector(".plan-tile")
    # C3 moved two colours far from the plan: a small gate opens and the question appears wherever the user is
    facts = {"palette_confirm": [{"id": "p1", "name": "Koi orange", "planned_hex": "#E4572E", "picture_hex": "#C8482A", "delta_e": 12.4},
                                 {"id": "p3", "name": "Sunny yellow", "planned_hex": "#FFB627", "picture_hex": "#F2A100", "delta_e": 10.8}]}
    pal = follow_up_gate(live, p, GateKind.SETUP_APPROVAL, facts, "Colours")
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("A few colours moved")
    expect(dlg).to_contain_text("#C8482A")
    expect(dlg.get_by_label("Use the colours from the picture (recommended)")).to_be_checked()
    ui.shot_element(ui.page.locator("dialog[open]").last, "gate1_palette_confirm")
    dlg.get_by_label("Keep the planned colours").check()
    dlg.get_by_role("button", name="Continue").click()
    wait_for(lambda: decisions(live, pal), 5, message="the palette answer")
    d = decisions(live, pal)[0]
    assert d.action.value == "approve" and d.choice == "planned"
