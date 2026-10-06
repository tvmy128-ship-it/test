"""The ways back, clicked through in a real browser (demo mode, the real app): "None of these: make a new plan" at Gate 1, and "Back to concept" on the
part board. A button that does nothing, a stage that does not move, or an approval that is lost without a reason shows up here.

Nothing is seeded: a brief is typed, the plan is made, and every step is a click.
"""
from __future__ import annotations

import re

import pytest
from e2e_helpers import Api, clean, dialog, dismiss_toasts, where_things_stand
from playwright.sync_api import expect


def start_duo(ui, api: Api) -> str:
    ui.goto("/new")
    ui.page.get_by_label("Name this duo").fill("Second Thoughts")
    ui.page.get_by_label("What is the idea?").fill("two cooks racing to open their food stall")
    ui.page.get_by_role("button", name="Start the plan").click()
    ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/plan$"))
    return re.search(r"/p/(prj_[^/]+)/plan", ui.page.url).group(1)


def settled(api: Api, pid: str, kind: str):
    """The open gate of this kind once none of its tiles is still being made."""
    g = api.gate(pid, kind)
    return g if g and g["tiles"] and not any(t["state"] in ("generating", "planned") for t in g["tiles"]) else None


@pytest.mark.timeout(1500)
def test_none_of_these_makes_three_new_plans_and_back_to_concept_keeps_the_approvals_that_still_hold(start_app, open_page):
    live = start_app()
    api = Api(live)
    ui = open_page(live)
    pid = start_duo(ui, api)

    # ------------------------------------------------------------------------------------------------- "None of these: make a new plan"
    first = ui.wait_until(lambda: settled(api, pid, "concept"), 600, "the first three plans", explain=lambda: where_things_stand(api, pid))
    first_sets = {t["facts"]["plan_set_id"] for t in first["tiles"]}
    ui.goto(f"/p/{pid}/gate1")
    expect(ui.page.locator(".plan-tile")).to_have_count(3, timeout=60000)
    themes_before = ui.page.locator(".plan-tile .dna-mini dd:first-of-type").all_inner_texts()
    ui.page.get_by_role("button", name="None of these: make a new plan").click()
    dialog(ui).get_by_label("What was wrong with these? (optional)").fill("too busy, I wanted something calmer")
    ui.shot("30_gate1_none_of_these", full=False)
    clean(ui, "new plan dialog", "dialog[open]")
    dialog(ui).get_by_role("button", name="Make three new plans").click()
    expect(ui.page.locator(".toast", has_text="Making three new plans")).to_be_visible(timeout=30000)
    second = ui.wait_until(lambda: (g := settled(api, pid, "concept")) and g["id"] != first["id"] and g, 600, "three NEW plans",
                           explain=lambda: where_things_stand(api, pid))
    assert {t["facts"]["plan_set_id"] for t in second["tiles"]}.isdisjoint(first_sets), "the new plans are a new set, not the old one drawn again"
    assert api.project(pid)["project"]["stage"] == "gate1"
    ui.goto(f"/p/{pid}/gate1")
    expect(ui.page.locator(".plan-tile")).to_have_count(3, timeout=60000)
    expect(ui.page.locator(".plan-tile .sheet-4up figure")).to_have_count(12, timeout=300000)
    expect(ui.page.locator(".badge.wild")).to_have_count(1)
    themes_after = ui.page.locator(".plan-tile .dna-mini dd:first-of-type").all_inner_texts()
    assert len(themes_after) == 3 and len(set(themes_after)) == 3, themes_after
    assert themes_after != themes_before, "the planner tried again"
    clean(ui, "the new plans")
    ui.shot("31_gate1_new_plans")
    dismiss_toasts(ui)

    # ------------------------------------------------------------------------------------------------- approve one, then change your mind on the board
    ui.page.locator(".plan-tile").first.get_by_role("button", name="Approve", exact=True).click()
    ui.page.wait_for_timeout(1500)
    if dialog(ui).count() and "Approve anyway" in dialog(ui).inner_text():
        dialog(ui).get_by_role("button", name="Approve anyway").click()
    ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/board$"), timeout=60000)
    board = ui.wait_until(lambda: settled(api, pid, "part_board"), 900, "the part board", explain=lambda: where_things_stand(api, pid))
    ui.page.locator('.tile[data-tile-id="b.colours"]').get_by_role("button", name="Approve", exact=True).click()
    ui.page.wait_for_timeout(800)
    if dialog(ui).count() and "Approve anyway" in dialog(ui).inner_text():
        dialog(ui).get_by_role("button", name="Approve anyway").click()
    ui.wait_until(lambda: api.tile(pid, "part_board", "b.colours")["state"] == "approved", 60, "B's colours to be approved")
    dismiss_toasts(ui)
    ui.page.get_by_role("button", name="Back to concept").click()
    dialog(ui).get_by_role("button", name="Back to concept").click()
    ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/gate1$"), timeout=60000)
    again = ui.wait_until(lambda: settled(api, pid, "concept"), 300, "the concept gate to come back", explain=lambda: where_things_stand(api, pid))
    assert api.project(pid)["project"]["stage"] == "gate1"
    assert again["tiles"], "the plans are still there to choose from"
    expect(ui.page.locator(".plan-tile")).to_have_count(len(again["tiles"]), timeout=60000)
    expect(ui.page.locator(".plan-tile .sheet-4up figure").first).to_be_visible()
    clean(ui, "gate 1 after back to concept")
    ui.shot("32_gate1_after_back_to_concept")

    # the same plan again (the first one, as before): the part that was approved is still approved, nothing is stuck, and the board is back
    chosen = again["tiles"][0]
    ui.page.locator(f'.plan-tile[data-tile-id="{chosen["tile_id"]}"]').get_by_role("button", name="Approve", exact=True).click()
    ui.page.wait_for_timeout(1500)
    if dialog(ui).count() and "Approve anyway" in dialog(ui).inner_text():
        dialog(ui).get_by_role("button", name="Approve anyway").click()
    ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/board$"), timeout=60000)
    board = ui.wait_until(lambda: settled(api, pid, "part_board"), 900, "the part board again", explain=lambda: where_things_stand(api, pid))
    states = {t["tile_id"]: t["state"] for t in board["tiles"]}
    assert states["b.colours"] == "approved", f"the approval of a part that the same plan does not change was lost: {states}"
    assert not {s for s in states.values() if s in ("failed", "needs_human", "waiting_manual")}, states
    assert api.project(pid)["project"]["stage"] in ("gate2", "parts"), api.project(pid)["project"]["stage"]
    expect(ui.page.locator(".tile")).to_have_count(len(states), timeout=60000)
    clean(ui, "the board again")
    ui.shot("33_board_after_second_approval", full=False)
    ui.no_errors()
