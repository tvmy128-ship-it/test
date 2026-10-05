"""The whole product, clicked through in a real browser, brief to export, in demo mode (mock providers, no keys): APP_SPEC 16 and 17.4.

Nothing here is seeded: the server is the real app (``create_app``, scheduler, every pipeline step) and every stage is reached the way a person
reaches it: Home, New duo, Plan, Gate 1 (Reimagine, "Change...", Approve), the part board (approve, Reimagine, Change, Approve all), Build (Tripo
through the mock API and one part by hand with a sample GLB), the duo, Gate 3 and Export. After every stage the page is checked for what a person
must never see (a stack trace, raw JSON, an internal id) and a screenshot of the stage is saved to ``tests/e2e_ui/screenshots/``.

The file also runs the same flow with faults injected into the mock providers (``DUOSKIN_MOCK_FAULTS``, APP_SPEC 16): each fault must end in a
friendly state a person can recover from.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

MESHES = Path(__file__).resolve().parents[2] / "duoskin" / "providers" / "fixtures" / "meshes"

BRIEF = "two friends sharing a rainy-day picnic"
MUST = "a teal ribbon"
JUNK = [
    (re.compile(r"Traceback|\bFile \"|\.py\b|\bat 0x"), "a stack trace"),
    (re.compile(r"[{]\s*\""), "raw JSON"),
    (re.compile(r"\b(prj|spc|gat|job|stp|chg|dec|ast|pls|cli)_[0-9a-z]{16,}\b"), "an internal id"),
    (re.compile(r"\b[0-9a-f]{40,64}\b"), "a content hash"),
    (re.compile(r"\bundefined\b|\[object |\bNaN\b|\bnull\b"), "a JavaScript leftover"),
    (re.compile(r"\b(hard_failures|HARD|A_LEAK|A_PALETTE|F_LINE_SKIN|CHK-[A-Z]\d+)\b"), "a check code"),
]


class Api:
    """The app's HTTP API, as the page itself calls it (the per-launch token goes in every request)."""

    def __init__(self, live) -> None:
        self.c = httpx.Client(base_url=live.url, headers={"X-DuoSkin-Token": live.rt.token}, timeout=60)

    def get(self, path: str, **params):
        r = self.c.get(path, params=params)
        assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text[:300]}"
        return r.json()

    def post(self, path: str, body=None, expect_status: int = 200):
        r = self.c.post(path, json=body)
        assert r.status_code == expect_status, f"POST {path}: {r.status_code} {r.text[:300]}"
        return r.json() if r.content else None

    def project(self, pid: str):
        return self.get(f"/api/projects/{pid}")

    def gates(self, pid: str, kind: str | None = None):
        gs = self.get("/api/gates", project_id=pid, state="open")
        return [g for g in gs if kind is None or g["kind"] == kind]

    def gate(self, pid: str, kind: str):
        found = self.gates(pid, kind)
        return found[0] if found else None

    def tile(self, pid: str, kind: str, tile_id: str):
        g = self.gate(pid, kind)
        return next((t for t in g["tiles"] if t["tile_id"] == tile_id), None) if g else None


def clean(ui, where: str, selector: str = "body") -> None:
    """The page says nothing a person must never see."""
    text = ui.page.locator(selector).inner_text()
    for rx, what in JUNK:
        m = rx.search(text)
        assert m is None, f"{where}: the page shows {what}: ...{text[max(0, m.start() - 60):m.end() + 60]!r}..."


def stage_is(api: Api, pid: str, *stages: str) -> bool:
    return api.project(pid)["project"]["stage"] in stages


def dismiss_toasts(ui) -> None:
    ui.page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")


def dialog(ui):
    return ui.page.locator("dialog[open]").last


@pytest.mark.timeout(2400)
def test_the_whole_product_works_in_demo_mode_from_brief_to_export_preview(start_app, open_page):
    live = start_app()
    api = Api(live)
    ui = open_page(live)

    # ------------------------------------------------------------------------------------------------- 1. Home and the brief
    ui.goto("/")
    expect(ui.page.locator("#demo-banner")).to_be_visible()
    expect(ui.page.locator("#demo-banner")).to_contain_text("Nothing here can be exported")
    expect(ui.page.get_by_role("heading", name="No duos yet")).to_be_visible()
    clean(ui, "home")
    ui.shot("01_home_empty")
    ui.page.get_by_role("link", name="Start your first duo").click()
    ui.page.wait_for_selector(".brief-form")
    ui.page.get_by_label("Name this duo").fill("Rainy Picnic")
    expect(ui.page.get_by_label("Boy + Girl")).to_be_checked()                                 # the default pair: A is the boy, B is the girl
    ui.page.get_by_label("What is the idea?").fill(BRIEF)
    ui.page.get_by_label("Must-include line 1").fill(MUST)
    ui.page.get_by_label("Let Tripo make them automatically").check()                          # API mode for the 3D parts (the mock Tripo)
    ui.shot("02_brief_filled")
    clean(ui, "brief")
    ui.page.get_by_role("button", name="Start the plan").click()
    ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/plan$"))
    pid = re.search(r"/p/(prj_[^/]+)/plan", ui.page.url).group(1)
    expect(ui.page.get_by_role("heading", name="The plan")).to_be_visible()
    assert api.project(pid)["project"]["combo"] == "bg" and api.project(pid)["project"]["must_include"] == [MUST]

    # ------------------------------------------------------------------------------------------------- 2. The plan: three plans, one wildcard
    expect(ui.page.locator(".plan-card")).to_have_count(3, timeout=300000)
    ui.shot("03_plan_running")
    expect(ui.page.get_by_role("heading", name="Your concepts are ready")).to_be_visible(timeout=600000)
    cards = ui.page.locator(".plan-card")
    expect(cards.locator(".badge.wild")).to_have_count(1)
    styles = [t.split("Pair style:")[1].split("\n")[0].strip() for t in cards.all_inner_texts()]
    assert len(set(styles)) == 3, f"an open brief gives three different pair structures, got {styles}"
    assert all("Rules check passed" in t for t in cards.all_inner_texts())
    assert [t.split("\n")[0] for t in cards.all_inner_texts()] == ["Plan 1", "Plan 2", "Plan 3"] or "Plan 1" in cards.all_inner_texts()[0]
    clean(ui, "plan")
    ui.shot("04_plan_done")
    ui.page.get_by_role("link", name="Pick a concept").click()

    # ------------------------------------------------------------------------------------------------- 3. Gate 1
    expect(ui.page.get_by_role("heading", name="Pick a concept")).to_be_visible()
    tiles = ui.page.locator(".plan-tile")
    expect(tiles).to_have_count(3)
    expect(ui.page.locator(".plan-tile .sheet-4up figure")).to_have_count(12, timeout=300000)    # front and back of both characters, three plans
    expect(ui.page.locator(".badge.wild")).to_have_count(1)
    assert "Wildcard" in tiles.nth(2).inner_text() or sum("Wildcard" in t for t in tiles.all_inner_texts()) == 1
    for i in range(3):
        sheet = tiles.nth(i).locator(".sheet-4up")
        assert sheet.inner_text().lower().count("front") == 2 and sheet.inner_text().lower().count("back") == 2
        assert tiles.nth(i).locator(".sheet-4up .char-chip").all_inner_texts() == ["A", "A", "B", "B"]
        expect(tiles.nth(i).locator(".dna-mini")).to_contain_text("Pair style")
        for name in ("Approve", "Reimagine", "Change…"):
            expect(tiles.nth(i).get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(ui.page.locator(".plan-tile .coverage").first).to_contain_text(MUST)
    assert not ui.page.locator(".heads-up").count(), "no suggestion is shown before the first choice"
    clean(ui, "gate 1")
    ui.shot("05_gate1_three_plans")

    gate = api.gate(pid, "concept")
    wild_index = next(i for i, t in enumerate(gate["tiles"]) if "Wildcard" in t["badges"])
    work = next(i for i in range(3) if i != wild_index)                                       # the plan we play with (not the wildcard)
    work_id = gate["tiles"][work]["tile_id"]

    # the design card opens with both characters side by side
    tiles.nth(work).get_by_role("button", name="See the full design card").click()
    expect(dialog(ui).locator(".dna-char")).to_have_count(2)
    ui.shot("06_gate1_design_card", full=False)
    clean(ui, "design card", "dialog[open]")
    ui.page.keyboard.press("Escape")

    # Reimagine character A only: B's drawings stay exactly as they are
    before = api.tile(pid, "concept", work_id)
    tiles.nth(work).get_by_role("button", name="Reimagine", exact=True).click()
    dialog(ui).get_by_label("Character A only").check()
    dialog(ui).get_by_role("button", name="Reimagine").click()
    ui.wait_until(lambda: api.tile(pid, "concept", work_id)["version"] > before["version"], 60, "the reimagine decision to be stored")
    ui.wait_until(lambda: api.tile(pid, "concept", work_id)["state"] == "ready", 400, "the reimagined plan to be ready again")
    after = api.tile(pid, "concept", work_id)
    assert after["assets"]["b_front"] == before["assets"]["b_front"] and after["assets"]["b_back"] == before["assets"]["b_back"], "B is untouched"
    assert after["assets"]["a_front"] != before["assets"]["a_front"], "A was drawn again"
    expect(ui.page.locator(".plan-tile .sheet-4up figure")).to_have_count(12, timeout=60000)
    clean(ui, "gate 1 after reimagine")
    ui.shot("07_gate1_after_reimagine")
    dismiss_toasts(ui)

    # Change... "make her jacket teal" (B only): the confirm step shows the diff and the estimate, then only B's drawing is redone
    before = api.tile(pid, "concept", work_id)
    tiles.nth(work).get_by_role("button", name="Change…", exact=True).click()
    dialog(ui).get_by_label("What should be different?").fill("make her jacket teal")
    dialog(ui).get_by_label("Character B only").check()
    ui.shot("08_gate1_change_box", full=False)
    dialog(ui).get_by_role("button", name="Show me the changes").click()
    expect(dialog(ui)).to_contain_text("Confirm your change", timeout=180000)
    d = dialog(ui)
    expect(d).to_contain_text("Parts that will be redone")
    expect(d).to_contain_text("Character B's concept picture")
    expect(d).to_contain_text("Estimated cost")
    clean(ui, "change confirm", "dialog[open]")
    ui.shot("09_gate1_change_confirm", full=False)
    d.get_by_role("button", name="Confirm the change").click()
    ui.wait_until(lambda: api.tile(pid, "concept", work_id)["version"] > before["version"] and api.tile(pid, "concept", work_id)["state"] == "ready", 400,
                  "the changed plan to be ready")
    changed = api.tile(pid, "concept", work_id)
    assert changed["assets"]["a_front"] == before["assets"]["a_front"], "the change names B only: A's drawing is untouched"
    clean(ui, "gate 1 after change")
    ui.shot("10_gate1_after_change")
    dismiss_toasts(ui)

    # Approve it: the choice may show a heads-up first ("Approve anyway?"), and a small colour question may follow
    tiles.nth(work).get_by_role("button", name="Approve", exact=True).click()
    ui.page.wait_for_timeout(1500)
    if dialog(ui).count() and "Approve anyway" in dialog(ui).inner_text():
        warned = dialog(ui).locator(".warning-dialog-list li").count()
        assert 1 <= warned <= 2, "at most two heads-ups"
        ui.shot("11_gate1_approve_anyway", full=False)
        dialog(ui).get_by_role("button", name="Approve anyway").click()
    ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/board$"), timeout=60000)

    # ------------------------------------------------------------------------------------------------- 4. The part board
    def palette_question() -> None:
        dl = ui.page.locator("dialog[open]")
        if dl.count() and "colours moved" in dl.last.inner_text():
            ui.shot("12_palette_question", full=False)
            dl.last.get_by_role("button", name="Continue").click()

    ui.wait_until(lambda: (palette_question() or True) and api.gate(pid, "part_board") is not None, 900, "the part board (Gate 2)")
    ui.wait_until(lambda: all(t["state"] not in ("generating", "planned") for t in api.gate(pid, "part_board")["tiles"]), 900, "every tile to settle")
    board = api.gate(pid, "part_board")
    states = {t["tile_id"]: t["state"] for t in board["tiles"]}
    assert set(states.values()) == {"ready"}, f"every tile of a duo made from the mock planner reaches READY: {states}"
    kinds = sorted({t["tile_id"].split(".")[1] for t in board["tiles"] if "." in t["tile_id"]})
    assert {"colours", "face", "hair", "acc", "print", "shirt", "pants"} <= set(kinds), kinds
    ui.page.reload()
    expect(ui.page.locator(".tile")).to_have_count(len(board["tiles"]), timeout=60000)
    expect(ui.page.locator(".board-col")).to_have_count(2)
    expect(ui.page.locator(".tile .tile-media img").first).to_be_visible()
    assert not ui.page.locator(".heads-up").count(), "no suggestion before the first choice on the board"
    clean(ui, "part board")
    ui.shot("13_board")
    assert ui.page.locator(".cost-value").first.inner_text().startswith("$")
