"""Settings (keys with masked status only, modes, budgets) and the setup wizard."""
from __future__ import annotations

import contextlib

import httpx
from playwright.sync_api import expect

SECRET = "sk-ant-api03-SUPERSECRETVALUE0123456789-wxyz"


def api(live, method: str, path: str, **kw):
    return httpx.request(method, live.url + path, headers={"X-DuoSkin-Token": live.token}, timeout=10, **kw)


def test_keys_tab_shows_status_without_echoing_values(ui, live):
    bodies: list[str] = []

    def grab(resp):
        if "/api/keys" in resp.url or "/api/settings" in resp.url:
            with contextlib.suppress(Exception):
                bodies.append(resp.text())

    ui.page.on("response", grab)
    ui.goto("/settings")
    rows = ui.page.locator(".key-row")
    expect(rows).to_have_count(6)
    anth = ui.page.locator('.key-row[data-provider="anthropic"]')
    expect(anth).to_contain_text("Claude")
    expect(anth).to_contain_text("Needed")                                           # required, said in words
    expect(anth.locator(".key-status")).to_have_text("Not set")
    expect(ui.page.locator('.key-row[data-provider="recraft"]')).to_contain_text("Recommended")
    ui.shot("settings_keys")
    # save a key: the box is a password field, is emptied at once, and only the masked tail comes back
    assert ui.page.locator('.key-row[data-provider="anthropic"] input').get_attribute("type") == "password"
    ui.page.locator('.key-row[data-provider="anthropic"] input').fill(SECRET)
    anth.get_by_role("button", name="Save key").click()
    expect(anth.locator(".key-status")).to_contain_text("wxyz")
    expect(anth.locator(".key-status")).to_contain_text("saved on this computer")
    assert ui.page.locator('.key-row[data-provider="anthropic"] input').input_value() == ""
    assert SECRET not in ui.page.content() and "SUPERSECRETVALUE" not in ui.page.locator("body").inner_text()
    ui.page.reload()
    ui.page.wait_for_selector(".key-row")
    assert "SUPERSECRETVALUE" not in ui.page.content()
    assert not any("SUPERSECRETVALUE" in b for b in bodies), "a response echoed the key"
    # removing it asks first
    ui.page.locator('.key-row[data-provider="anthropic"]').get_by_role("button", name="Remove").click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Remove").click()
    expect(ui.page.locator('.key-row[data-provider="anthropic"] .key-status')).to_have_text("Not set")
    ui.no_errors()


def test_test_key_button_reports_in_plain_words_and_warns_about_cost(ui, live):
    ui.goto("/settings")
    ui.page.locator('.key-row[data-provider="openai"]').get_by_role("button", name="Test key").click()
    dialog = ui.page.get_by_role("dialog")
    expect(dialog).to_contain_text("$0.006")                                         # the cost note comes first
    dialog.get_by_role("button", name="Run the test").click()
    result = ui.page.locator('.key-row[data-provider="openai"] .key-result')
    expect(result).to_contain_text("It works")                                       # mock mode: nothing to test, no call made
    ui.page.locator('.key-row[data-provider="anthropic"]').get_by_role("button", name="Test key").click()    # no cost note: runs at once
    expect(ui.page.locator('.key-row[data-provider="anthropic"] .key-result')).to_contain_text("Mock provider")
    ui.no_errors()


def test_provider_modes_and_demo_mode_save_at_once(ui, live):
    ui.goto("/settings/providers")
    sel = ui.page.get_by_label("fal (optional extras) mode")
    sel.select_option("mock")
    expect(ui.page.locator(".toast", has_text="Saved")).to_have_count(1)
    assert api(live, "GET", "/api/settings").json()["providers"]["modes"]["fal"] == "mock"
    ui.page.get_by_role("switch", name="Demo mode (no keys needed)").check()
    expect(ui.page.locator(".toast", has_text="Saved")).to_have_count(2)
    assert api(live, "GET", "/api/settings").json()["demo_mode"] is True
    expect(ui.page.locator("#demo-banner")).to_be_visible()
    ui.shot("settings_providers")
    ui.no_errors()


def test_budget_and_check_defaults(ui, live):
    ui.goto("/settings/checks")
    sw = ui.page.get_by_role("switch", name="Check similarity to my reference pictures")
    assert not sw.is_checked(), "the reference-similarity check is OFF by default"
    sw.check()
    expect(ui.page.locator(".toast", has_text="Saved")).to_have_count(1)
    assert api(live, "GET", "/api/settings").json()["checks"]["reference_similarity_default"] is True
    ui.goto("/settings/budgets")
    ui.clear_toasts()
    ui.page.get_by_label("Budget cap per duo ($)").fill("22")
    ui.page.get_by_label("Budget cap per duo ($)").press("Tab")
    expect(ui.page.locator(".toast", has_text="Saved")).to_have_count(1)
    s = api(live, "GET", "/api/settings").json()
    assert s["budgets"]["per_duo_usd"] == 22 and s["budgets"]["daily_cap_usd"] is None
    ui.clear_toasts()
    ui.page.get_by_label("Daily limit across all duos ($)").fill("40")
    ui.page.get_by_label("Daily limit across all duos ($)").press("Tab")
    expect(ui.page.locator(".toast", has_text="Saved")).to_have_count(1)
    assert api(live, "GET", "/api/settings").json()["budgets"]["daily_cap_usd"] == 40


def test_model_candidate_alias_is_refused_in_plain_words(ui, live):
    ui.goto("/settings/models")
    box = ui.page.get_by_label("Candidate for Planner")
    box.fill("claude-opus-latest")
    box.press("Tab")
    expect(ui.page.locator(".toast")).to_contain_text("dated version")
    assert api(live, "GET", "/api/settings").json()["models"]["candidates"] == {}
    box.fill("claude-opus-5-20260901")
    box.press("Tab")
    expect(ui.page.locator(".toast", has_text="Candidate saved")).to_have_count(1)
    assert api(live, "GET", "/api/settings").json()["models"]["candidates"] == {"planner": "claude-opus-5-20260901"}


def test_three_d_blender_and_diagnostics(ui, live):
    ui.goto("/settings/3d")
    field = ui.page.get_by_label("Blender program (optional)")
    expect(field).to_have_value("")
    assert "Without it, FBX and Blender files cannot be opened" in ui.page.locator("main").inner_text()
    ui.goto("/settings/diagnostics")
    ui.page.get_by_role("button", name="Make a diagnostics file").click()
    expect(ui.page.locator(".settings-body")).to_contain_text("Saved as:", timeout=30000)
    expect(ui.page.locator(".settings-body")).to_contain_text("no keys in it")


def test_tabs_work_with_the_keyboard(ui):
    ui.goto("/settings")
    ui.page.locator("#tab-keys").focus()
    ui.page.keyboard.press("ArrowRight")
    expect(ui.page.locator("#tab-budgets")).to_have_attribute("aria-selected", "true")
    expect(ui.page.get_by_label("Budget cap per duo ($)")).to_be_visible()
    assert "#/settings/budgets" in ui.page.url


def test_setup_wizard_walks_through_seven_skippable_steps(ui, live):
    ui.goto("/setup")
    steps = ui.page.locator(".setup-list li")
    expect(steps).to_have_count(7)
    expect(ui.page.locator("main")).to_contain_text("1. Your keys")
    ui.shot("setup_keys")
    ui.page.get_by_role("button", name="Next").click()
    expect(ui.page.locator("main")).to_contain_text("2. Check this computer")
    body = ui.page.locator(".setup-body")
    body.get_by_role("button", name="Check this computer").click()
    expect(body.locator(".row .badge").first).to_be_visible(timeout=60000)                              # the doctor finished (SSE)
    assert "{" not in ui.page.locator(".setup-body").inner_text()
    ui.page.get_by_role("button", name="Skip this step").click()
    expect(ui.page.locator("main")).to_contain_text("3. Blender (optional)")
    assert "winget install BlenderFoundation.Blender" in ui.page.content()
    ui.page.get_by_role("button", name="Next").click()
    expect(ui.page.locator("main")).to_contain_text("4. Kits")
    assert "Without it:" in ui.page.locator("main").inner_text()                                       # what happens without each kit
    for title in ("5. Your taste", "6. House style", "7. Test day"):
        ui.page.get_by_role("button", name="Next").click()
        expect(ui.page.locator("main")).to_contain_text(title)
    expect(ui.page.get_by_role("link", name="Start my first duo")).to_be_visible()
    ui.no_errors()
