"""The page shell: home, banners, stage bar, cost bar, live updates, security contract, accessibility basics."""
from __future__ import annotations

import re

import httpx
from playwright.sync_api import expect

from duoskin.models.common import utcnow
from duoskin.models.cost import CostEntry
from duoskin.models.project import Stage

from . import seed


def api(live, method: str, path: str, **kw):
    headers = {"X-DuoSkin-Token": live.token, **kw.pop("headers", {})}
    return httpx.request(method, live.url + path, headers=headers, timeout=10, **kw)


def test_home_loads_with_the_promise_and_no_errors(ui):
    ui.goto("/")
    expect(ui.page.locator("main h1")).to_have_text("Your duos")
    expect(ui.page.get_by_role("link", name="New duo")).to_be_visible()
    # the core promise is written on the page and in the cost bar area
    assert "nothing expensive runs before you approve" in ui.page.locator("main").inner_text().lower()
    assert "Nothing costs money until you approve it" in ui.page.locator("#stagebar").inner_text()
    ui.shot("home_empty")
    ui.no_errors()


def test_demo_banner_is_persistent_on_every_page(ui):
    for path in ("/", "/settings", "/jobs", "/costs", "/new", "/library"):
        ui.goto(path)
        banner = ui.page.locator("#demo-banner")
        expect(banner).to_be_visible()
        assert "DEMO" in banner.inner_text() and "nothing here can be exported" in banner.inner_text().lower()
    ui.no_errors()


def test_home_lists_projects_with_the_next_action(ui, live):
    p1 = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1, spent=2.35)
    seed.concept_gate(live.rt, p1)
    seed.project(live.rt, "Night Market", "bb", Stage.BRIEF)
    ui.goto("/")
    cards = ui.page.locator(".project-card")
    expect(cards).to_have_count(2)
    waiting = ui.page.locator(".project-card.needs-you")
    expect(waiting).to_have_count(1)
    assert "1 waiting on you" in waiting.inner_text() and "Boy + Girl" in waiting.inner_text() and "$2.35" in waiting.inner_text()
    waiting.get_by_role("link", name="Pick a concept").click()
    expect(ui.page).to_have_url(f"{live.url}/#/p/{p1.id}/gate1")
    ui.no_errors()


def test_stage_bar_and_cost_bar_follow_the_open_project(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1, spent=2.35)
    seed.concept_gate(live.rt, p)
    ui.goto(f"/p/{p.id}/gate1")
    ui.page.wait_for_selector(".stagebar")
    names = ui.page.locator(".stagebar .stage-name").all_inner_texts()
    assert names == ["Brief", "Plan", "Concept", "Parts", "Build", "Duo", "Export"]
    current = ui.page.locator(".stagebar .stage.current")
    assert current.locator(".stage-name").inner_text() == "Concept"
    assert current.locator("a").get_attribute("aria-current") == "step"
    assert "Your turn" in current.inner_text()                       # a decision is waiting: said in words
    expect(ui.page.locator("#costbar")).to_contain_text("$2.35")
    expect(ui.page.locator("#costbar")).to_contain_text("of $15.00 cap")
    # a cost arrives while the page is open: the bar updates by itself (SSE), no reload
    live.rt.budget.add_entry(CostEntry(ts=utcnow(), project_id=p.id, provider="mock", operation="test", usd=1.25, basis="usage", state="committed"))
    expect(ui.page.locator("#costbar")).to_contain_text("$3.60")
    ui.shot("stage_and_cost_bar")
    ui.no_errors()


def test_cost_bar_warns_in_words_near_the_cap(ui, live):
    p = seed.project(live.rt, "Pricey", "gg", Stage.GATE2, spent=13.0)
    ui.goto(f"/p/{p.id}")
    expect(ui.page.locator("#costbar")).to_contain_text("Close to the cap")
    live.rt.budget.add_entry(CostEntry(ts=utcnow(), project_id=p.id, provider="mock", operation="more", usd=2.5, basis="usage", state="committed"))
    expect(ui.page.locator("#costbar")).to_contain_text("Cap reached")


def test_unknown_route_is_friendly(ui):
    ui.page.goto(f"{ui.live.url}/#/nowhere/at/all")
    expect(ui.page.locator("main h1")).to_have_text("That page does not exist")
    assert "Traceback" not in ui.page.content()


def test_missing_project_shows_a_plain_message_not_json(ui):
    ui.goto("/p/prj_missing")
    text = ui.page.locator("main").inner_text()
    assert "not found" in text.lower() or "could not be found" in text.lower()
    assert "{" not in text and "Traceback" not in text
    ui.no_errors()


def test_every_api_request_carries_the_token(ui, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)
    seed.concept_gate(live.rt, p)
    seen: list[tuple[str, str | None]] = []
    ui.page.on("request", lambda r: seen.append((r.url, r.headers.get("x-duoskin-token"))) if "/api/" in r.url and "/api/events" != r.url.split("?")[0][len(live.url):] else None)
    ui.goto("/")
    ui.goto(f"/p/{p.id}/gate1")
    ui.goto("/settings")
    ui.page.wait_for_timeout(300)
    assert seen, "the UI made no API calls?"
    missing = [u for u, t in seen if t != live.token]
    assert not missing, f"requests without the token: {missing}"


def test_token_is_read_from_the_meta_tag_and_the_served_page_has_no_inline_code(ui, live):
    ui.goto("/")
    token = ui.page.locator('meta[name="duoskin-token"]').get_attribute("content")
    assert token == ui.live.token and "__DUOSKIN" not in token
    html = httpx.get(live.url + "/").text
    assert not re.search(r"\sstyle\s*=", html), "inline style attributes are blocked by the CSP"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handlers are blocked by the CSP"
    inline = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*importmap)[^>]*>", html)
    assert inline == [], "the only inline script allowed is the import map (its hash is in the CSP)"
    assert "<style" not in html
    # visit the pages, open dialogs and the viewer: the browser must report no policy violations
    for path in ("/new", "/settings", "/jobs", "/costs", "/setup", "/library"):
        ui.goto(path)
    ui.no_errors()


def test_two_tabs_share_one_event_stream_and_both_update(ui_factory, live):
    (a, ctx) = ui_factory()
    b_page = ctx.new_page()
    sse = []
    for pg in (a.page, b_page):
        pg.on("request", lambda r: sse.append(r.url) if "/api/events" in r.url and "/poll" not in r.url else None)
    a.goto("/")
    b_page.goto(f"{live.url}/#/")
    b_page.wait_for_selector("main h1")
    a.page.wait_for_timeout(500)
    resp = api(live, "POST", "/api/projects", json={"name": "Live Duo", "combo": "gg"})
    assert resp.status_code == 201
    expect(a.page.locator(".project-card")).to_have_count(1)
    expect(b_page.locator(".project-card")).to_have_count(1)
    assert len(sse) == 1, f"only the leader tab may hold an EventSource, saw {sse}"
    a.no_errors()


def test_stream_reconnects_and_replays_what_was_missed(ui, live):
    streams: list[dict] = []
    ui.page.on("request", lambda r: streams.append(r.headers) if "/api/events" in r.url and "/poll" not in r.url else None)
    ui.goto("/")
    ui.page.wait_for_timeout(600)
    assert len(streams) == 1
    live.rt.bus.emit("toast", {"message": "First, seen live", "level": "info"})
    expect(ui.page.locator(".toast", has_text="First, seen live")).to_have_count(1)
    live.rt.bus.close()                                  # the server ends every stream, as on a restart
    live.rt.bus.emit("toast", {"message": "Missed while reconnecting", "level": "info"})
    expect(ui.page.locator(".toast", has_text="Missed while reconnecting")).to_have_count(1, timeout=20000)
    assert len(streams) >= 2
    assert streams[1].get("last-event-id"), "the reconnect must carry Last-Event-ID so nothing is lost or repeated"
    expect(ui.page.locator(".toast", has_text="First, seen live")).to_have_count(1)     # the replay did not repeat what was already shown
    expect(ui.page.locator("#conn")).to_be_hidden(timeout=10000)


def test_keyboard_skip_link_and_focus_after_navigation(ui):
    ui.goto("/")
    assert ui.page.evaluate("document.activeElement.tagName") == "H1"      # a navigation lands on the page heading
    ui.page.evaluate("document.querySelector('.skip-link').focus()")       # what the first Tab of a fresh page reaches
    box = ui.page.locator(".skip-link").bounding_box()
    assert box and box["y"] >= 0                                           # it slides into view when focused
    ui.page.keyboard.press("Enter")
    assert ui.page.evaluate("document.activeElement.id") == "main"
    ui.page.get_by_role("link", name="Costs").click()
    expect(ui.page.locator("main h1")).to_have_text("Costs")
    expect(ui.page.locator("main h1")).to_be_focused()      # focus moves to the page heading


def test_actions_are_real_buttons_with_names(ui, live):
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.GATE2)
    seed.board_gate(live.rt, p)
    ui.goto(f"/p/{p.id}/board")
    ui.page.wait_for_selector(".tile")
    bad = ui.page.evaluate("[...document.querySelectorAll('[role=button], [onclick]')].filter(e => e.tagName !== 'BUTTON' && e.tagName !== 'A').length")
    assert bad == 0
    unnamed = ui.page.evaluate("[...document.querySelectorAll('button')].filter(b => !(b.innerText || b.getAttribute('aria-label') || b.title || '').trim()).length")
    assert unnamed == 0
    noalt = ui.page.evaluate("[...document.querySelectorAll('main img')].filter(i => !i.hasAttribute('alt')).length")
    assert noalt == 0


def test_dark_mode_follows_the_system_and_no_page_scrolls_sideways(ui_factory, live):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.GATE1)
    seed.concept_gate(live.rt, p)
    (dark, _ctx) = ui_factory(color_scheme="dark")
    dark.goto("/")
    bg = dark.page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg != "rgb(245, 244, 249)" and bg.startswith("rgb(18")          # the dark token, not the light one
    dark.goto(f"/p/{p.id}/gate1")
    dark.page.wait_for_selector(".plan-tile")
    dark.shot("gate1_dark")
    for path in ("/", "/new", "/settings", "/jobs", "/costs", f"/p/{p.id}/gate1", "/setup"):
        dark.goto(path)
        dark.page.wait_for_timeout(150)
        overflow = dark.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, f"{path} scrolls sideways by {overflow}px at 1280"
    dark.no_errors()


def test_home_shows_the_setup_check_and_the_cost_bar_shows_work_in_progress(ui, live, fake_plan):
    ui.goto("/")
    expect(ui.page.locator(".doctor-line")).to_contain_text("Setup check:")
    expect(ui.page.locator(".doctor-line")).to_contain_text("not run yet")
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    assert api(live, "POST", f"/api/projects/{p.id}/plan").status_code == 200
    expect(ui.page.locator("#costbar .pill", has_text="Working: 1 step")).to_be_visible(timeout=15000)       # the queue status, live
    fake_plan.release.set()
    expect(ui.page.locator("#costbar .pill", has_text="Working")).to_have_count(0, timeout=20000)
