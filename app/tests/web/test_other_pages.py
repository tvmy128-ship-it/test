"""Costs, Library, Calibration, Learning and the project hub. Routes other tracks are still finishing are faked with page.route
(and also answered with 404/501 to prove the 'not available yet' states)."""
from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from duoskin.models.common import utcnow
from duoskin.models.cost import CostEntry
from duoskin.models.project import Stage

from . import seed

NOT_BUILT = json.dumps({"error": "http_404", "message": "Not Found"})


def fulfill(data, status=200):
    return lambda route: route.fulfill(status=status, content_type="application/json", body=json.dumps(data))


# ------------------------------------------------------------------------------------------------------------- costs
def test_costs_page_totals_by_service_estimate_vs_actual_and_orphans(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)
    q = seed.project(live.rt, "Moon Tea", "gg", Stage.GATE2)
    add = live.rt.budget.add_entry
    add(CostEntry(ts=utcnow(), project_id=p.id, provider="anthropic", operation="messages.stream:L3", usd=0.84, basis="usage", state="committed", step_id="s1"))
    add(CostEntry(ts=utcnow(), project_id=p.id, provider="openai", operation="images.edit:I1", usd=1.20, basis="usage", state="committed", step_id="s2"))
    add(CostEntry(ts=utcnow(), project_id=q.id, provider="tripo", operation="multiview_to_model", usd=1.10, credits=110, basis="credits", state="committed", step_id="s3", balance_after=640))
    add(CostEntry(ts=utcnow(), project_id=q.id, provider="openai", operation="images.generate:I2", usd=0.25, basis="orphan", state="orphan", step_id="s4"))
    ui.goto("/costs")
    cards = ui.page.locator(".stat-card")
    expect(cards.nth(0)).to_contain_text("$3.39")                                           # spent, orphan included so the cap stays honest
    expect(cards.nth(3)).to_contain_text("640")                                              # Tripo credits left
    expect(ui.page.locator(".bars").first).to_contain_text("OpenAI (pictures)")
    expect(ui.page.locator(".bars").first).to_contain_text("$1.45")
    expect(ui.page.locator(".note.warn")).to_contain_text("1 call may have been charged without a result")
    table = ui.page.locator("table")
    expect(table).to_contain_text("Actual (credits)")
    expect(table).to_contain_text("Possibly charged")
    assert "{" not in ui.page.locator("main").inner_text()
    ui.shot("costs")
    ui.page.get_by_label("Show costs for").select_option(label="Plush Koi")
    expect(cards.nth(0)).to_contain_text("$2.04")
    ui.no_errors()


# ------------------------------------------------------------------------------------------------------------- library
def test_library_shows_availability_and_requires_origin_and_licence_for_a_kit(ui, live):
    posted: list[dict] = []
    lib = {"manifest_sha": "ab" * 32, "demo": False, "flags": {"head_base_present": False, "body_base_present": False, "hair_kit_empty": False, "dreamsim_present": False, "makeup": "unavailable"},
           "hair": ["hair_bob_03", "hair_spiky_05"], "fabrics": ["fleece_a", "twill_b"], "fold_sets": [], "registries": {"registry_face": 12, "registry_print": 3},
           "labels": {"head_base": "2D preview - no head base", "blender": "FBX not produced"}}
    ui.page.route(re.compile(r"/api/library$"), fulfill(lib))
    ui.page.route(re.compile(r"/api/library/kits$"), lambda r: (posted.append(r.request.post_data_json), r.fulfill(status=200, content_type="application/json", body="{}")))
    ui.goto("/library")
    expect(ui.page.locator(".kit-list")).to_contain_text("Head base")
    expect(ui.page.locator(".kit-list")).to_contain_text("Not added")
    expect(ui.page.locator("main")).to_contain_text("Hair bob 03")
    expect(ui.page.locator("main")).to_contain_text("2D preview - no head base")
    assert "abababab" not in ui.page.locator("main").inner_text()                            # no hashes in the user's face
    expect(ui.page.get_by_role("heading", name="Collections")).to_be_visible()
    assert "{" not in ui.page.locator("main").inner_text()
    ui.shot("library")
    ui.page.get_by_label("Folder", exact=True).fill("C:\\kits\\my hair")
    ui.page.get_by_role("button", name="Add this kit").click()
    expect(ui.page.get_by_role("alert")).to_contain_text("where it came from")              # origin and licence are required
    assert posted == []
    ui.page.get_by_label("Where it came from").select_option("own")
    ui.page.get_by_label("Licence").select_option("own_work")
    ui.page.get_by_role("button", name="Add this kit").click()
    ui.wait_until(lambda: posted, 5, "the add-kit call")
    assert posted[0] == {"folder_path": "C:\\kits\\my hair", "kind": "hair", "origin": "own", "license": "own_work"}


def test_library_calibration_learning_not_built_yet_are_friendly(ui, live):
    for route in ("library", "calibration/session", "learning/report"):
        ui.page.route(re.compile(rf"/api/{route}(\?.*)?$"), fulfill({"error": "http_404", "message": "Not Found"}, 404))
    for path in ("/library", "/calibration", "/learning"):
        ui.goto(path)
        expect(ui.page.get_by_role("heading", name="Not available yet").first).to_be_visible()
        text = ui.page.locator("main").inner_text()
        assert "404" not in text and "Not Found" not in text and "http_404" not in text
    ui.no_errors()


# ------------------------------------------------------------------------------------------------------------- calibration
def test_calibration_is_locked_until_five_duos_then_takes_blind_labels(ui, live):
    """The page against canned answers (the real API has its own browser test in tests/learning/test_ui_pages.py)."""
    labels: list[dict] = []
    ui.page.route(re.compile(r"/api/calibration/session\?kind=drill$"), fulfill({"locked": True, "approved_duos": 2, "needed": 5}))
    ui.goto("/calibration")
    expect(ui.page.locator("main")).to_contain_text("Calibration needs at least 5 approved duos")
    expect(ui.page.locator("main")).to_contain_text("You have 2")
    sha = seed.put(live.rt, seed.figure_png("koi", "front"))
    ui.page.unroute(re.compile(r"/api/calibration/session\?kind=drill$"))
    ui.page.route(re.compile(r"/api/calibration/session\?kind=drill$"), fulfill({"items": [{"item_id": "it1", "images": [sha]}, {"item_id": "it2", "images": [sha]}],
                                                                                 "session": {"id": "s1", "answered": 0, "total": 20}, "counts": {"drill_share": 0.2, "cap": 0.25, "by_source": {"gate": 31}}}))
    ui.page.route(re.compile(r"/api/calibration/labels$"), lambda r: (labels.append(r.request.post_data_json), r.fulfill(status=204)))
    ui.page.reload()
    ui.page.wait_for_selector("main h1")
    expect(ui.page.locator("main")).to_contain_text("What is this?")
    item = ui.page.locator("main .panel").first.inner_text().lower()
    assert "clone" not in item.split("what is this?")[0] and "real duo" not in item.split("what is this?")[0]    # blind: nothing says which it is
    ui.page.get_by_role("button", name="A real duo").click()
    ui.page.get_by_role("button", name="Like", exact=True).click()
    ui.page.get_by_role("button", name="Save and next").click()
    ui.wait_until(lambda: labels, 5, "the label")
    assert labels[0] == {"item_id": "it1", "label": "real_duo", "like": True}, "an answer and a like are one request"
    expect(ui.page.locator("main")).to_contain_text("capped at a quarter")


# ------------------------------------------------------------------------------------------------------------- learning
def test_learning_report_and_the_regression_asks_before_it_starts(ui, live):
    runs: list[dict] = []
    rep = {"week": {"label": "2026-W41", "start": "2026-10-05", "end": "2026-10-11"}, "approved_duos": 8, "decisions": {"approved": 20, "rejected": 5},
           "gate1_first_try_approval": 0.62, "gate1": {"duos": 8}, "wildcard_pick_rate": 0.25, "wildcard": {"duos": 8}, "cost_per_duo": 7.4,
           "per_check": [{"check_id": "taste_busy", "flag_rate_on_approved": 0.12, "catch_rate_on_rejected": 0.4, "flagged_approved": 2, "flagged_rejected": 2,
                          "hidden": False, "kind": "soft"}], "structure_use": {"complement": 5, "mirror": 2}, "labels": {"effective_total": 120, "target": 200},
           "regression": {"versions": {"roles": [], "baseline": None}, "runs": [], "sample": 10, "briefs": 40, "estimate": {"text": "About $20 to $45."}}}
    ui.page.route(re.compile(r"/api/learning/report(\?.*)?$"), fulfill(rep))
    ui.page.route(re.compile(r"/api/regression/estimate.*$"), fulfill({"text": "About $20 to $45 for 10 briefs.", "needs_confirmation": True}))
    ui.page.route(re.compile(r"/api/regression/run$"), lambda r: (runs.append(r.request.post_data_json), r.fulfill(status=200, content_type="application/json", body="{}")))
    ui.goto("/learning")
    main = ui.page.locator("main")
    expect(main).to_contain_text("62%")                                                       # a rate, as a percent
    expect(main).to_contain_text("$7.40")
    expect(main).to_contain_text("Taste busy")
    assert "{" not in main.inner_text()
    ui.page.get_by_role("button", name="Start the regression test").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("About $20 to $45 for 10 briefs.")
    dlg.get_by_role("button", name="Start the test").click()
    ui.wait_until(lambda: runs, 5, "the regression call")
    assert runs[0]["stage"] == "plan" and runs[0]["sample"] == 10 and runs[0]["candidate_versions"] == {}
    expect(ui.page.locator(".toast", has_text="Started")).to_have_count(1)


# ------------------------------------------------------------------------------------------------------------- project hub
def test_project_hub_shows_the_brief_next_action_and_manage_buttons(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1, spent=2.35)
    seed.concept_gate(live.rt, p)
    ui.goto(f"/p/{p.id}")
    expect(ui.page.locator("main h1")).to_have_text("Plush Koi")
    expect(ui.page.locator(".next-card")).to_contain_text("Waiting for you to pick a concept")
    expect(ui.page.locator(".next-card").get_by_role("link", name="Pick a concept")).to_have_attribute("href", f"#/p/{p.id}/gate1")
    expect(ui.page.locator(".brief-quote")).to_contain_text("calm koi pond duo")
    expect(ui.page.locator("main")).to_contain_text("koi on the back")
    expect(ui.page.locator("main")).to_contain_text("Compare with my reference")
    ui.page.get_by_role("button", name="Pause this duo").click()
    expect(ui.page.get_by_role("button", name="Resume this duo")).to_be_visible()
    assert live.rt.repo.get_project(p.id).paused is True
    ui.shot("project_hub")
    ui.no_errors()


def test_calibration_round_has_a_quiet_timer(ui, live):
    sha = seed.put(live.rt, seed.figure_png("koi", "front"))
    ui.page.route(re.compile(r"/api/calibration/session\?kind=drill$"), fulfill({"items": [{"item_id": "it1", "images": [sha]}], "session": {"id": "s1", "answered": 0, "total": 1}}))
    ui.goto("/calibration")
    expect(ui.page.locator("main")).to_contain_text("a round takes 10 to 15 minutes")
    expect(ui.page.locator("main")).to_contain_text("Time so far: 00:0")


def test_rename_a_duo_with_a_dialog_and_hide_it(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    ui.goto(f"/p/{p.id}")
    ui.page.get_by_role("button", name="Rename").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg.get_by_label("Name")).to_be_focused()
    dlg.get_by_label("Name").fill("Koi Pond")
    dlg.get_by_role("button", name="Save the name").click()
    expect(ui.page.locator("main h1")).to_have_text("Koi Pond")
    assert live.rt.repo.get_project(p.id).name == "Koi Pond"
    ui.page.get_by_role("button", name="Hide this duo").click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Hide it").click()
    expect(ui.page).to_have_url(re.compile(r"#/$"))
    expect(ui.page.locator(".project-card")).to_have_count(0)
    assert live.rt.repo.get_project(p.id).archived is True
