"""Gate 2, the part board, against the real gate lifecycle: every part alone, the three choices on every tile, the soft
warning release and "Approve anyway?", Approve all remaining, the tile drawer, Make it myself on Tripo and the brush mask."""
from __future__ import annotations

import io
import json
import re

import pytest
from PIL import Image
from playwright.sync_api import expect

from duoskin.checks.model import CheckResult
from duoskin.engine.cas import make_prov
from duoskin.engine.testkit import wait_for
from duoskin.models.asset import AssetLink
from duoskin.models.common import new_id
from duoskin.models.project import Stage

from . import seed

# the UI is tested against the engine's generic gate lifecycle, not the pipeline's appliers (those are tested by the pipeline track)
pytestmark = pytest.mark.usefixtures("plain_gates")

def setup(live, **kw):
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.GATE2, spent=6.1)
    return p, seed.board_gate(live.rt, p, **kw)


def decisions(live, gate):
    return live.rt.repo.list_decisions(gate.id)


def tile_of(ui, label):
    return ui.page.locator(".tile", has=ui.page.get_by_role("heading", name=label, exact=False)).first


def test_every_part_is_shown_alone_in_two_columns(ui, live):
    p, _gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    expect(ui.page.locator(".tile")).to_have_count(14)
    expect(ui.page.locator(".board-col.char-a .tile")).to_have_count(7)
    expect(ui.page.locator(".board-col.char-b .tile")).to_have_count(7)
    expect(ui.page.get_by_text("0 of 14 parts approved")).to_be_visible()
    a = ui.page.locator(".board-col.char-a")
    # face on the head: 4 expressions x 5 skin tones, with a badge saying it is a 2D preview
    face = a.locator('.tile[data-kind="face"]')
    expect(face.locator(".hero img")).to_have_count(1)                                      # the tone sheet: 4 expressions x 5 skin tones
    assert face.locator(".hero figcaption").inner_text() == "On the head: 4 expressions, 5 skin tones"
    assert face.locator(".strip figcaption").all_inner_texts() == ["Neutral", "Blink", "Mouth open", "Happy"]
    expect(face).to_contain_text("2D preview — no head base")
    # accessory: the front, the four views and the on-body scale render
    acc = a.locator('.tile[data-kind="accessory"]')
    expect(acc.locator("figure")).to_have_count(5)
    assert acc.locator("figcaption").all_inner_texts() == ["Front", "Left", "Back", "Right", "On the body, to scale"]
    assert "Views from GPT" not in acc.inner_text()
    expect(ui.page.locator(".board-col.char-b").locator('.tile[data-kind="accessory"]')).to_contain_text("Views from GPT (lower reliability)")
    # shirt and pants: flat front and flat back
    for kind in ("shirt", "pants"):
        t = a.locator(f'.tile[data-kind="{kind}"]')
        assert t.locator(".pair figcaption").all_inner_texts() == ["Flat front", "Flat back"]
        expect(t.locator("figure")).to_have_count(3)                                          # + the Roblox box preview; the clothing file itself is drawer-only
    expect(a.locator('.tile[data-kind="shirt"]')).to_contain_text("procedural folds")
    expect(a.locator('.tile[data-kind="colours"] figure')).to_have_count(3)
    expect(a.locator('.tile[data-kind="print"]')).to_contain_text("Small size (100 px)")
    expect(a.locator('.tile[data-kind="hair"]')).to_contain_text("Closest kit hair: Bob soft (91% match)")
    # all images actually loaded (the CAS URLs work)
    ui.page.wait_for_timeout(300)
    broken = ui.page.evaluate("[...document.querySelectorAll('.tile img')].filter(i => !i.complete || i.naturalWidth === 0).length")
    assert broken == 0
    # every tile has Approve / Reimagine / Change... (colours has no Reimagine, as the spec says)
    for t in ui.page.locator(".tile").all():
        assert t.get_by_role("button", name="Approve", exact=True).count() == 1
        assert t.get_by_role("button", name="Change…", exact=True).count() == 1
    assert ui.page.locator('.tile[data-kind="colours"]').first.get_by_role("button", name="Reimagine").count() == 0
    ui.shot("board_top")
    ui.page.locator('.tile[data-kind="face"]').first.scroll_into_view_if_needed()
    ui.shot("board_face_tile")
    ui.no_errors()


def test_rows_line_up_a_beside_b(ui, live):
    p, _gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    ui.page.wait_for_selector(".tile")
    ys = ui.page.evaluate("""() => ['face','hair','shirt'].map(k => [...document.querySelectorAll(`.tile[data-kind="${k}"]`)].map(e => Math.round(e.getBoundingClientRect().top)))""")
    for pair in ys:
        assert abs(pair[0] - pair[1]) <= 2, f"A and B tiles are not level: {ys}"


def test_approve_one_tile_updates_the_board_and_stamps_the_part(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    shirt = ui.page.locator('.board-col.char-a .tile[data-kind="shirt"]')
    shirt.get_by_role("button", name="Approve", exact=True).dblclick()                      # a double click must not decide twice
    expect(shirt.get_by_role("button", name="Approved ✓")).to_be_disabled()
    expect(shirt).to_contain_text("Approved")
    expect(ui.page.get_by_text("1 of 14 parts approved")).to_be_visible()
    d = decisions(live, gate)
    assert len(d) == 1 and d[0].action.value == "approve" and d[0].tile_id == "a.shirt"
    part = live.rt.repo.get_part(p.id, "a.shirt")
    assert part.state.value == "approved" and part.approval is not None                     # the Gate 2 stamp was written


def test_stale_tile_conflict_is_explained_not_shown_as_an_error(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    ui.page.wait_for_selector(".tile")
    import httpx

    r = httpx.post(f"{live.url}/api/gates/{gate.id}/decisions", headers={"X-DuoSkin-Token": live.token},
                   json={"tile_id": "a.pants", "action": "approve", "expected_version": 0, "client_decision_id": "other-tab-1"})
    assert r.status_code == 200
    result = ui.page.evaluate("""async (gate) => {
        const { decide } = await import('/web/components/decisions.js');
        const r = await decide({id: gate}, {tile_id: 'a.pants', version: 0}, 'approve');
        return r.status;
    }""", gate.id)
    assert result == "conflict"
    expect(ui.page.locator(".toast", has_text="changed a moment ago")).to_have_count(1)
    expect(ui.page.locator('.board-col.char-a .tile[data-kind="pants"]')).to_contain_text("Approved")     # the board caught up by itself


def test_soft_warnings_appear_only_after_the_first_choice_max_two_with_approve_anyway(ui, live):
    p, gate = setup(live, warnings=3)
    ui.goto(f"/p/{p.id}/board")
    ui.page.wait_for_selector(".tile")
    assert "Heads-up" not in ui.page.locator("main").inner_text()                           # withheld before the first choice
    ui.page.locator('.board-col.char-a .tile[data-kind="face"]').get_by_role("button", name="Approve", exact=True).click()
    dialog = ui.page.get_by_role("dialog")
    expect(dialog).to_contain_text("Approve anyway?")
    expect(dialog.locator(".warning-dialog-list li")).to_have_count(2)                     # three exist, at most two are released
    ui.shot_element(ui.page.locator("dialog[open]").last, "board_approve_anyway")
    d = decisions(live, gate)
    assert len(d) == 1 and d[0].provisional is True                                          # the follow-up has not started
    assert live.rt.repo.get_part(p.id, "a.face").state.value == "ready"
    dialog.get_by_role("button", name="Approve anyway").click()
    expect(ui.page.locator('.board-col.char-a .tile[data-kind="face"]')).to_contain_text("Approved")
    d = decisions(live, gate)[0]
    assert d.provisional is False and len(d.warnings_overridden) == 2
    labels = live.rt.db.conn().execute("SELECT COUNT(*) FROM labels WHERE kind='warning_override'").fetchone()[0]
    assert labels == 2
    expect(ui.page.locator(".heads-up")).to_have_count(2)                                    # the same two, on their tiles, and no more
    # a later approve on the same gate shows no further warnings
    ui.page.locator('.board-col.char-b .tile[data-kind="pants"]').get_by_role("button", name="Approve", exact=True).click()
    expect(ui.page.get_by_role("dialog")).to_have_count(0)
    expect(ui.page.locator('.board-col.char-b .tile[data-kind="pants"]')).to_contain_text("Approved")


def test_go_back_deletes_the_provisional_decision(ui, live):
    p, gate = setup(live, warnings=2)
    ui.goto(f"/p/{p.id}/board")
    t = ui.page.locator('.board-col.char-b .tile[data-kind="hair"]')
    t.get_by_role("button", name="Approve", exact=True).click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Go back").click()
    expect(ui.page.get_by_role("dialog")).to_have_count(0)
    wait_for(lambda: decisions(live, gate) == [], 5, message="the provisional decision to be deleted")
    expect(t).to_contain_text("Ready for you")
    expect(t.get_by_role("button", name="Approve", exact=True)).to_be_enabled()


def test_approve_all_remaining_skips_a_part_with_a_failed_required_check(ui, live):
    p, gate = setup(live, hard_fail_on="a.shirt")
    ui.goto(f"/p/{p.id}/board")
    bad = ui.page.locator('.board-col.char-a .tile[data-kind="shirt"]')
    expect(bad).to_contain_text("Needs fixing first")
    expect(bad).to_contain_text("The shirt colour is too far from the palette.")
    expect(bad.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
    ui.shot("board_failed_check")
    ui.page.get_by_role("button", name="Approve all remaining (13)").click()
    expect(ui.page.get_by_text("13 of 14 parts approved")).to_be_visible()
    expect(bad).not_to_contain_text("Approved")
    assert live.rt.repo.get_gate(gate.id).state == "open"                                    # one tile is still waiting


def test_when_every_part_is_approved_the_build_is_next(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    ui.page.get_by_role("button", name="Approve all remaining (14)").click()
    expect(ui.page.get_by_role("link", name="Watch the build")).to_be_visible()
    assert live.rt.repo.get_gate(gate.id).state == "decided"


def test_reimagine_asks_before_spending_and_the_face_asks_which_part(ui, live, plain_gates):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    hair = ui.page.locator('.board-col.char-a .tile[data-kind="hair"]')
    hair.get_by_role("button", name="Reimagine", exact=True).click()
    dialog = ui.page.get_by_role("dialog")
    expect(dialog).to_contain_text("This can cost a little")
    dialog.get_by_role("button", name="Not now").click()
    assert decisions(live, gate) == []
    hair.get_by_role("button", name="Reimagine", exact=True).click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Reimagine").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    assert decisions(live, gate)[0].action.value == "reimagine" and decisions(live, gate)[0].target is None
    face = ui.page.locator('.board-col.char-b .tile[data-kind="face"]')
    face.get_by_role("button", name="Reimagine", exact=True).click()
    pick = ui.page.get_by_role("dialog")
    expect(pick).to_contain_text("Reimagine which part of the face?")
    pick.get_by_label("Just the lashes").check()
    pick.get_by_role("button", name="Reimagine").click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Reimagine").click()
    wait_for(lambda: len(decisions(live, gate)) == 2, 5, message="the face decision")
    d = decisions(live, gate)[1]
    assert d.target == "lash" and d.tile_id == "b.face"


def test_drawer_has_alternatives_face_parts_and_the_check_summary(ui, live, plain_gates):
    p, gate = setup(live)
    rt = live.rt
    sha = seed.put(rt, seed.part_png("face", variant=3))
    rt.cas.put(seed.part_png("face", variant=3), "png", link=AssetLink(id=new_id("lnk"), asset_sha=sha, project_id=p.id, part_id="a.face", role="final", status="chosen", provenance=make_prov("mock", model="mock-image", cost_usd=0.04)), prov=make_prov("mock"))
    rt.repo.insert_checks([CheckResult(check_id="A_PALETTE", kind="hard", passed=True, subject_sha=sha, evidence="colour distance 3.1"),
                           CheckResult(check_id="A_RIG_NECK", kind="hard", passed=True, ran=True, status="not_applicable", na_reason="no head base", subject_sha=sha, evidence="n/a: no head base"),
                           CheckResult(check_id="TASTE_BUSY", kind="soft", passed=False, subject_sha=sha, evidence="The face feels a little busy.")], project_id=p.id, step_id=None)
    ui.goto(f"/p/{p.id}/board")
    face = ui.page.locator('.board-col.char-a .tile[data-kind="face"]')
    face.get_by_role("button", name="See 1 other version").click()
    drawer = ui.page.locator("dialog.drawer")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".drawer-views img")).to_have_count(6)                                  # tone sheet, 4 expressions, the face-parts layout
    expect(drawer.get_by_role("heading", name="Other versions")).to_be_visible()
    for part in ("eyes (iris)", "lashes", "eyebrows", "closed mouth", "open mouth"):
        assert drawer.locator(".face-parts li", has_text=re.compile(part.split(" (")[0], re.IGNORECASE)).count() == 1
    assert drawer.locator(".face-parts li").get_by_role("button", name="Reimagine").count() == 5
    assert drawer.locator(".face-parts li").get_by_role("button", name="Change…").count() == 5
    expect(drawer.locator(".drawer-detail")).to_contain_text("All 2 required checks passed")      # the hard results; a not-applicable one counts
    expect(drawer.locator(".drawer-detail")).to_contain_text("Does not apply")
    assert "busy" not in drawer.inner_text()                                                      # soft taste notes stay hidden before the first choice
    expect(drawer.locator(".drawer-detail")).to_contain_text("Where each picture came from")
    expect(drawer.locator(".drawer-detail")).to_contain_text("Practice mode")
    ui.shot_element(ui.page.locator("dialog[open]").last, "board_drawer")
    drawer.get_by_role("button", name="Use version 2").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "select_alternative" and d.choice == "0" and d.tile_id == "a.face"
    ui.no_errors()


def test_per_part_face_change_uses_the_part_as_target(ui, live, plain_gates):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    ui.page.locator('.board-col.char-a .tile[data-kind="face"]').get_by_role("button", name="See 1 other version").click()
    drawer = ui.page.locator("dialog.drawer")
    drawer.locator(".face-parts li", has_text="brows").get_by_role("button", name="Change…").click()
    box = ui.page.get_by_role("dialog").last
    expect(box.get_by_label("Just the eyebrows")).to_be_checked()
    box.get_by_label("What should be different?").fill("thicker and straighter")
    box.get_by_role("button", name="Show me the changes").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "change" and d.target == "brow" and d.text == "thicker and straighter"


def test_brush_mask_makes_a_png_the_size_of_the_picture_and_uploads_it(ui, live, plain_gates):
    p, gate = setup(live)
    uploads: list[bytes] = []
    returned = "ab" * 32

    def fake_upload(route):
        uploads.append(route.request.post_data_buffer)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"sha": returned}))

    ui.page.route(re.compile(r"/api/uploads/mask$"), fake_upload)
    ui.goto(f"/p/{p.id}/board")
    t = ui.page.locator('.board-col.char-a .tile[data-kind="print"]')
    t.get_by_role("button", name="Change…", exact=True).click()
    dlg = ui.page.get_by_role("dialog")
    dlg.get_by_text("Only change one area (optional)").click()
    ui.shot_element(ui.page.locator("dialog[open]").last, "board_brush_mask")
    canvas = dlg.locator(".mask-canvas")
    box = canvas.bounding_box()
    ui.page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.25)           # paint a stroke with the pointer
    ui.page.mouse.down()
    ui.page.mouse.move(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.25, steps=8)
    ui.page.mouse.up()
    dlg.get_by_label("What should be different?").fill("make the hexagon teal")
    dlg.get_by_role("button", name="Show me the changes").click()
    ui.wait_until(lambda: uploads and decisions(live, gate), 8, "the upload and the decision")
    body = uploads[0]
    png = body[body.index(b"\x89PNG"):]
    img = Image.open(io.BytesIO(png))
    assert img.mode == "RGBA" and img.size == (300, 300)                                          # exactly the size of the tile picture
    alpha = img.getchannel("A")
    painted = sum(1 for v in alpha.getdata() if v == 0)
    assert 300 * 20 < painted < 300 * 300 * 0.5 and set(alpha.getdata()) <= {0, 255}              # the OpenAI convention: alpha 0 where it may change (a stroke), 255 elsewhere
    d = decisions(live, gate)[0]
    assert d.action.value == "change" and d.mask_sha == returned and d.tile_id == "a.print.top.0"


def test_quick_area_buttons_work_without_a_pointer(ui, live, plain_gates):
    p, _gate = setup(live)
    uploads: list[bytes] = []
    ui.page.route(re.compile(r"/api/uploads/mask$"), lambda route: (uploads.append(route.request.post_data_buffer), route.fulfill(status=200, content_type="application/json", body=json.dumps({"sha": "cd" * 32}))))
    ui.goto(f"/p/{p.id}/board")
    ui.page.locator('.board-col.char-a .tile[data-kind="print"]').get_by_role("button", name="Change…", exact=True).click()
    dlg = ui.page.get_by_role("dialog")
    dlg.get_by_text("Only change one area (optional)").click()
    dlg.get_by_role("button", name="Left half").click()
    dlg.get_by_label("What should be different?").fill("simpler shape")
    dlg.get_by_role("button", name="Show me the changes").click()
    ui.wait_until(lambda: uploads, 8, "the upload")
    img = Image.open(io.BytesIO(uploads[0][uploads[0].index(b"\x89PNG"):]))
    a = img.getchannel("A")
    assert a.getpixel((10, 150)) == 0 and a.getpixel((290, 150)) == 255                              # the left half may change; the right half is kept


def test_make_it_myself_on_tripo_warns_once_then_builds_the_pack(ui, live):
    p, _gate = setup(live)
    sent: list[dict] = []
    answers = [(501, {"error": "not_implemented", "message": "The Tripo pack export is not available in this build yet (3D track)"}),
               (200, {"pack_id": "DS-moon-tea-a-hair-3fa9c1", "folder": "C:\\packs\\DS-moon-tea-a-hair-3fa9c1", "return_dir": "C:\\packs\\DS-moon-tea-a-hair-3fa9c1\\return", "files": []})]

    def pack(route):
        sent.append(route.request.post_data_json)
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    ui.page.route(re.compile(r"/tripo-pack$"), pack)
    ui.goto(f"/p/{p.id}/board")
    hair = ui.page.locator('.board-col.char-a .tile[data-kind="hair"]')
    hair.get_by_role("button", name="Make it myself on Tripo").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("FREE plan")
    expect(dlg).to_contain_text("public")
    dlg.get_by_role("button", name="I understand, make the pack").click()
    expect(ui.page.locator(".toast", has_text="not available yet")).to_have_count(1)             # a route that is not built answers 501: said kindly
    assert sent == [{"acknowledged_free_plan": True}]                                              # the server is told the warning was shown
    ui.clear_toasts()
    hair.get_by_role("button", name="Make it myself on Tripo").click()                            # the warning was shown once; not again
    expect(ui.page).to_have_url(re.compile(rf"#/p/{p.id}/build$"))
    assert sent[1] == {"acknowledged_free_plan": True}


def test_the_server_can_ask_for_the_free_plan_warning_itself(ui, live):
    p, _gate = setup(live)
    sent: list[dict] = []

    def pack(route):
        body = route.request.post_data_json
        sent.append(body)
        if body.get("acknowledged_free_plan") and len(sent) > 1:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"pack_id": "DS-x-a-hair-111111"}))
        else:
            route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "free_plan_warning_required", "message": "Confirm the free-plan warning before the first pack.", "warning": "Tripo FREE plan models are public (CC BY 4.0) and not for commercial use."}))

    ui.page.route(re.compile(r"/tripo-pack$"), pack)
    ui.page.add_init_script("localStorage.setItem('duoskin-free-plan-seen', '1')")               # this browser saw the warning, the server has not
    ui.goto(f"/p/{p.id}/board")
    ui.page.locator('.board-col.char-a .tile[data-kind="hair"]').get_by_role("button", name="Make it myself on Tripo").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Tripo FREE plan models are public (CC BY 4.0) and not for commercial use.")
    dlg.get_by_role("button", name="I understand, make the pack").click()
    expect(ui.page).to_have_url(re.compile(rf"#/p/{p.id}/build$"))
    assert [s.get("acknowledged_free_plan") for s in sent] == [True, True]


def test_back_to_concept_asks_first(ui, live, plain_gates):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    ui.page.get_by_role("button", name="Back to concept").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Nothing is deleted")
    dlg.get_by_role("button", name="Back to concept").click()
    expect(ui.page).to_have_url(re.compile(rf"#/p/{p.id}/gate1$"))
    assert decisions(live, gate)[0].action.value == "back_to_concept"


def test_board_updates_by_itself_when_a_part_finishes(ui, live):
    p, gate = setup(live)
    g = live.rt.repo.get_gate(gate.id)
    tiles = [t.model_copy(update={"state": "generating", "assets": {}}) if t.tile_id == "b.hair" else t for t in g.tiles]
    live.rt.repo.save_gate(g.model_copy(update={"tiles": tiles}))
    ui.goto(f"/p/{p.id}/board")
    hair = ui.page.locator('.board-col.char-b .tile[data-kind="hair"]')
    expect(hair).to_contain_text("Being made now")
    expect(hair.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
    g = live.rt.repo.get_gate(gate.id)
    done = [t.model_copy(update={"state": "ready", "assets": {"front": seed.put(live.rt, seed.part_png("hair", palette="teal"))}, "version": t.version + 1}) if t.tile_id == "b.hair" else t for t in g.tiles]
    live.rt.repo.save_gate(g.model_copy(update={"tiles": done}))
    live.rt.bus.emit("tile.updated", {"gate_id": gate.id, "tile_id": "b.hair", "state": "ready", "version": 1}, p.id)
    expect(hair).to_contain_text("Ready for you")
    expect(hair.get_by_role("button", name="Approve", exact=True)).to_be_enabled()
    expect(hair.locator("img")).to_have_count(1)


def test_dialogs_close_with_escape_and_give_focus_back(ui, live):
    p, gate = setup(live)
    ui.goto(f"/p/{p.id}/board")
    btn = ui.page.locator('.board-col.char-a .tile[data-kind="hair"]').get_by_role("button", name="Reimagine", exact=True)
    btn.focus()
    ui.page.keyboard.press("Enter")
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Draw this again?")
    assert ui.page.evaluate("document.activeElement.closest('dialog') !== null")           # focus is inside the dialog
    ui.page.keyboard.press("Escape")
    expect(ui.page.get_by_role("dialog")).to_have_count(0)
    expect(btn).to_be_focused()                                                              # back where the user was
    assert decisions(live, gate) == []
    # the Approve-anyway step cannot be dismissed by accident: only its two buttons answer it
    p2, _gate2 = setup(live, warnings=2)
    ui.goto(f"/p/{p2.id}/board")
    ui.page.locator('.board-col.char-a .tile[data-kind="face"]').get_by_role("button", name="Approve", exact=True).click()
    expect(ui.page.get_by_role("dialog")).to_contain_text("Approve anyway?")
    ui.page.keyboard.press("Escape")
    expect(ui.page.get_by_role("dialog")).to_have_count(1)
    ui.page.get_by_role("dialog").get_by_role("button", name="Go back").click()
