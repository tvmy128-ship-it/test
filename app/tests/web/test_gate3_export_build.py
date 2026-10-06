"""Gate 3 (final pick), Export and Build (manual 3D import wizard)."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from duoskin.engine.cas import make_prov
from duoskin.engine.gates import allowed_actions_for
from duoskin.engine.testkit import wait_for
from duoskin.models.common import utcnow
from duoskin.models.gate import Gate, GateKind, GateTile
from duoskin.models.job import JobKind, Step, StepState
from duoskin.models.project import Stage

from . import seed

MESHES = Path(__file__).resolve().parents[2] / "duoskin" / "providers" / "fixtures" / "meshes"


# the UI is tested against the engine's generic gate lifecycle, not the pipeline's appliers (those are tested by the pipeline track)
pytestmark = pytest.mark.usefixtures("plain_gates")

def decisions(live, gate):
    return live.rt.repo.list_decisions(gate.id)


def webgl_ok(page) -> bool:
    return page.evaluate("(() => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl2') || c.getContext('webgl')); } catch (e) { return false; } })()")


# ------------------------------------------------------------------------------------------------------------- gate 3
def test_candidate_shows_sides_phone_strip_poses_3d_and_the_reviews(ui, live, plain_gates):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3, spent=9.4)
    glb = live.rt.cas.put((MESHES / "plush_pet.glb").read_bytes(), "glb", prov=make_prov("mock")).sha256
    seed.final_gate(live.rt, p, candidates=1, glb_sha=glb)
    ui.goto(f"/p/{p.id}/gate3")
    cand = ui.page.locator(".candidate")
    expect(cand).to_have_count(1)
    expect(cand.locator(".sides.char-a figure")).to_have_count(5)                           # front, back, left, right, three-quarter
    expect(cand.locator(".sides.char-b figure")).to_have_count(5)
    assert cand.locator(".sides.char-a figcaption").all_inner_texts() == ["Front", "Back", "Left", "Right", "Three-quarter"]
    strip = cand.locator(".phone-strip img").first
    ui.page.wait_for_timeout(300)
    sizes = strip.evaluate("i => [i.naturalWidth, i.getBoundingClientRect().width, getComputedStyle(i).imageRendering]")
    assert sizes[1] == sizes[0] and sizes[2] == "pixelated"                                    # shown at its own size (the pipeline already made it 2x), nearest-neighbour
    expect(cand.locator("section", has_text="five poses").locator("img")).to_have_count(1)    # the face in 5 poses, one picture
    expect(cand.locator(".reviews")).to_contain_text("The pair reads as a set from across the room.")
    expect(cand.locator(".reviews")).to_contain_text("Clone band: well clear of the nearest duo")
    expect(cand.locator(".reviews")).to_contain_text("no close match in your earlier duos")
    expect(cand.locator("details", has_text="How the judge scored it")).to_contain_text("Belong together")
    assert cand.locator(".candidate-head .badge").count() == 0 or "Reference-similarity" not in cand.locator(".candidate-head").inner_text()
    expect(cand.locator(".reviews")).to_contain_text("Looks original")
    expect(cand.locator(".reviews")).to_contain_text("Reference-similarity check is off")      # the toggle is off: said, not hidden
    if webgl_ok(ui.page):
        expect(cand.locator(".viewer-stats")).to_contain_text("triangles", timeout=20000)   # both characters in one viewer
    ui.page.wait_for_timeout(500)
    ui.shot("gate3_candidate")
    ui.no_errors()


def test_similarity_result_replaces_the_banner_when_the_check_is_on(ui, live, plain_gates):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3, reference_similarity_check=True)
    seed.final_gate(live.rt, p, similarity_on=True)
    ui.goto(f"/p/{p.id}/gate3")
    expect(ui.page.locator(".reviews")).not_to_contain_text("Reference-similarity check is off")
    expect(ui.page.locator(".reviews")).to_contain_text("Not checked yet")


def test_pick_then_export(ui, live, plain_gates):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    gate = seed.final_gate(live.rt, p)
    ui.goto(f"/p/{p.id}/gate3")
    export = ui.page.get_by_role("button", name="Export the upload kit")
    expect(export).to_be_disabled()                                                           # pick first
    ui.page.get_by_role("button", name="Pick this duo").click()
    expect(ui.page.get_by_role("button", name="Picked ✓")).to_be_disabled()
    assert decisions(live, gate)[0].action.value == "pick"
    expect(export).to_be_enabled()
    export.click()
    expect(ui.page).to_have_url(re.compile(rf"#/p/{p.id}/export$"))
    assert [d.action.value for d in decisions(live, gate)] == ["pick", "export"]


def test_change_one_part_from_the_final_pick(ui, live, plain_gates):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    seed.board_gate(live.rt, p)
    gate = seed.final_gate(live.rt, p)
    ui.goto(f"/p/{p.id}/gate3")
    ui.page.get_by_role("button", name="Change one part…").click()
    dlg = ui.page.get_by_role("dialog")
    dlg.get_by_label("A · Hair").check()
    dlg.get_by_label("What should be different?").fill("a little shorter")
    dlg.get_by_role("button", name="Show me the changes").click()
    wait_for(lambda: decisions(live, gate), 5, message="the decision")
    d = decisions(live, gate)[0]
    assert d.action.value == "change" and d.text == "[A · Hair] a little shorter"


# ------------------------------------------------------------------------------------------------------------- export
def checklist_state(confirmed=False, uploaded=False):
    def item(item_id, char, typ, channel_text, fee):
        return {"item_id": item_id, "character": char, "type": typ, "channel": "creator_dashboard", "channel_text": channel_text, "fee_robux": fee, "fee_note": "paid once per upload" if fee else "",
                "requirements": ["585 x 559 PNG, no text"], "notes": [],
                "steps": [{"step_id": "confirm_final", "text": f"Confirm the final renders for the {typ.lower()}", "kind": "confirm", "requires": [], "ticked": confirmed, "locked": False},
                          {"step_id": "upload", "text": f"Upload the {typ.lower()}", "kind": "upload", "requires": ["confirm_final"], "ticked": uploaded, "locked": not confirmed}]}
    return {"version": 1, "banners": [], "notes": ["Items cannot be edited after upload."], "context": {},
            "items": [item("a.shirt", "a", "Shirt", "Upload on the Creator Dashboard.", 0), item("a.hair", "a", "Hair", "Upload through Roblox Studio.", 80)]}


def export_state(**kw):
    return {"status": "built", "reason": None, "kit_dir": "C:\\Users\\you\\DuoSkin Exports\\night-market", "zip": None, "mock": False, "checks": [],
            "banners": ["FBX not produced: if Studio rejects the glTF, install Blender and re-export", "a.hair: This file came from Tripo's FREE plan, so the model is public."],
            "manifest": {"schema": "duoskin.manifest/1", "project": "Night Market", "mock": False,
                         "items": [{"item_id": "a.hair", "character": "a", "type": "Hair", "license": "tripo_free_public_ccby_noncommercial", "lineage": [{"source": "tripo_manual"}], "tris": 3800, "fbx": "not produced"},
                                   {"item_id": "a.shirt", "character": "a", "type": "Shirt", "license": "n/a", "lineage": [{"source": "code"}], "tris": None, "fbx": None}],
                         "files": [{"path": "meshes/a_hair.glb", "bytes": 120000}, {"path": "meshes/a_hair.gltf", "bytes": 3000}, {"path": "textures/Shirt_a.png", "bytes": 90000},
                                   {"path": "manifest.json", "bytes": 2100}, {"path": "CHECKLIST.html", "bytes": 4000}]},
            "checklist": checklist_state(**kw), "version": 4 + int(kw.get("confirmed", False))}


def test_export_page_tree_banners_checklist_lock_and_provenance(ui, live):
    p = seed.project(live.rt, "Night Market", "bg", Stage.EXPORTED)
    patches: list[dict] = []
    state = {"confirmed": False, "uploaded": False}

    def exports(route):
        req = route.request
        if req.method == "PATCH":
            body = req.post_data_json
            patches.append(body)
            if body["step_id"] == "confirm_final":
                state["confirmed"] = body["ticked"]
            else:
                state["uploaded"] = body["ticked"]
            data = export_state(**state)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"checklist": data["checklist"], "version": data["version"]}))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(export_state(**state)))

    ui.page.route(re.compile(r"/api/exports/prj_[^/]+(/checklist)?$"), exports)
    opened: list[dict] = []
    ui.page.route(re.compile(r"/api/os/open-folder$"), lambda r: (opened.append(r.request.post_data_json), r.fulfill(status=204)))
    ui.goto(f"/p/{p.id}/export")
    tree = ui.page.locator(".tree").first
    expect(tree).to_contain_text("meshes/")
    expect(tree).to_contain_text("manifest.json")
    expect(ui.page.get_by_text("FBX not produced")).to_be_visible()
    expect(ui.page.get_by_text("Character A, hair: This file came from Tripo's FREE plan")).to_be_visible()      # "a.hair:" is a code, not words
    expect(ui.page.get_by_text("model is public")).to_be_visible()
    shirt = ui.page.locator('.check-item[data-item-id="a.shirt"]')
    expect(shirt).to_contain_text("Upload on the Creator Dashboard.")
    expect(shirt.get_by_label("Upload the shirt")).to_be_disabled()                           # locked until the step before it is ticked
    expect(ui.page.locator(".checklist li.locked")).to_have_count(2)
    expect(ui.page.locator('.check-item[data-item-id="a.hair"]')).to_contain_text("Upload fee: 80 Robux")
    ui.shot("export_locked")
    shirt.get_by_label("Confirm the final renders for the shirt").check()
    expect(shirt.get_by_label("Upload the shirt")).to_be_enabled()
    assert patches == [{"item_id": "a.shirt", "step_id": "confirm_final", "ticked": True, "expected_version": 4}]
    shirt.get_by_label("Upload the shirt").check()
    ui.wait_until(lambda: len(patches) == 2, 5, "the second tick")
    assert patches[1] == {"item_id": "a.shirt", "step_id": "upload", "ticked": True, "expected_version": 5}
    ui.page.get_by_role("button", name="Open the folder").click()
    ui.wait_until(lambda: opened, 5, "the open-folder call")
    assert opened == [{"kind": "export", "id": p.id}]
    prov = ui.page.locator("table")
    expect(prov).to_contain_text("Tripo FREE plan: public, CC BY 4.0, no commercial use")
    expect(prov).to_contain_text("3,800")
    assert "{" not in ui.page.locator("main").inner_text()
    ui.no_errors()


def test_export_blocked_and_not_started_states_are_plain(ui, live):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    ui.page.route(re.compile(r"/api/exports/"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
        {"status": "blocked", "reason": "CHK-E01: a part still comes from a practice (mock) source", "mock": True, "banners": ["DEMO: this kit was made with mock providers and cannot be uploaded"]})))
    ui.goto(f"/p/{p.id}/export")
    expect(ui.page.get_by_role("heading", name="The kit is not ready to upload")).to_be_visible()
    expect(ui.page.locator("main")).to_contain_text("cannot be uploaded")
    expect(ui.page.locator("main")).to_contain_text("practice (demo) services")
    assert "CHK-E01" not in ui.page.locator("main").inner_text()                                 # no internal check codes in front of a non-technical user
    ui.page.unroute(re.compile(r"/api/exports/"))
    ui.page.route(re.compile(r"/api/exports/"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "none", "banners": [], "checklist": None, "version": 0})))
    ui.page.reload()
    expect(ui.page.get_by_role("button", name="Make the upload kit")).to_be_enabled()           # the duo is at the final pick


def test_export_not_built_yet_says_so_kindly(ui, live):
    p = seed.project(live.rt, "Night Market", "bg", Stage.GATE3)
    not_built = {"error": "not_implemented", "message": "The export kit is not available in this build yet (export track)"}
    ui.page.route(re.compile(r"/api/exports/"), lambda route: route.fulfill(status=501, content_type="application/json", body=json.dumps(not_built)))
    ui.goto(f"/p/{p.id}/export")
    expect(ui.page.get_by_role("heading", name="Not available yet")).to_be_visible()          # the route answers 501
    assert "501" not in ui.page.locator("main").inner_text() and "not_implemented" not in ui.page.content()


# ------------------------------------------------------------------------------------------------------------- build
def manual_gate(live, p, part_id="a.hair", reason="made by hand"):
    j = seed.job(live.rt, p.id)
    facts = {"pack_id": "DS-moon-tea-a-hair-3fa9c1", "folder": "C:\\Users\\you\\DuoSkin Exports\\TripoPacks\\moon-tea\\DS-moon-tea-a-hair-3fa9c1",
             "return_dir": "C:\\Users\\you\\DuoSkin Exports\\TripoPacks\\moon-tea\\DS-moon-tea-a-hair-3fa9c1\\return", "inbox": "C:\\inbox",
             "settings_text": "FREE PLAN WARNING\n1. Pick the newest model.\n2. Triangle mesh, face limit about 4000.", "reason": reason, "status": "waiting"}
    tile = GateTile(tile_id=part_id, part_id=part_id, label="A · Hair", facts=facts, allowed_actions=allowed_actions_for(GateKind.MANUAL_IMPORT))
    return live.rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=GateKind.MANUAL_IMPORT, tiles=[tile], opened_at=utcnow()))


def test_manual_import_panel_pack_folder_dropzone_and_wizard(ui, live, tmp_path):
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.BUILDING)
    seed.board_gate(live.rt, p)
    manual_gate(live, p)
    posts: list[tuple[str, bytes | None]] = []

    def importer(route):
        req = route.request
        posts.append((req.url.split("/api/")[1], req.post_data_buffer if "assign" not in req.url else req.post_data.encode()))
        if req.url.endswith("/assign"):
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "inb_1"}))

    ui.page.route(re.compile(r"/api/imports(/inb_1/assign)?$"), importer)
    opened: list[dict] = []
    ui.page.route(re.compile(r"/api/os/open-folder$"), lambda r: (opened.append(r.request.post_data_json), r.fulfill(status=204)))
    ui.goto(f"/p/{p.id}/build")
    expect(ui.page.locator("main")).to_contain_text("you will be asked how to make them when that step starts")      # the project's 3D mode, in words
    panel = ui.page.locator(".gate-panel.manual")
    expect(panel).to_contain_text("Make A · Hair on Tripo's website")
    expect(panel).to_contain_text("On Tripo's FREE plan your model becomes public")           # the licence warning first
    expect(panel).to_contain_text("DS-moon-tea-a-hair-3fa9c1.glb")                              # the rename instruction
    expect(panel.get_by_text("What to choose on Tripo's website")).to_be_visible()
    panel.get_by_text("What to choose on Tripo's website").click()
    expect(panel.locator(".settings-text")).to_contain_text("Pick the newest model")
    ui.shot("build_manual_import")
    panel.get_by_role("button", name="Open the pack folder").click()
    ui.wait_until(lambda: opened, 5, "the open-folder call")
    assert opened == [{"kind": "tripo_pack", "id": "DS-moon-tea-a-hair-3fa9c1"}]
    # a file of the wrong type is refused before anything is uploaded
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    panel.locator('input[type="file"]').set_input_files(str(bad))
    expect(panel.locator(".dz-status")).to_contain_text("not a type we can open")
    assert posts == []
    # a GLB: preview, the plan question, the free-plan banner, then upload and assign
    panel.locator('input[type="file"]').set_input_files(str(MESHES / "plush_pet.glb"))
    wizard = panel.locator(".import-wizard")
    expect(wizard).to_contain_text("Import plush_pet.glb")
    expect(wizard.get_by_text("Which Tripo plan made this file?")).to_be_visible()
    expect(wizard.locator("p.note.warn")).to_be_hidden()
    wizard.get_by_label("Tripo, the free plan").check()
    expect(wizard.locator("p.note.warn")).to_contain_text("never be marked ready to sell")
    wizard.get_by_label("Tripo task link (optional)").fill("https://example.com/task/123")
    if webgl_ok(ui.page):
        expect(wizard.locator(".viewer-stats")).to_contain_text("triangles", timeout=20000)    # a local preview before anything is sent
    ui.shot("build_import_wizard")
    wizard.get_by_role("button", name="Import this file").click()
    ui.wait_until(lambda: len(posts) == 2, 8, "both import calls")
    body = posts[0][1]
    assert posts[0][0] == "imports" and b'name="project_id"' in body and p.id.encode() in body and b'name="part_id"' in body
    assert b'filename="plush_pet.glb"' in body
    assign = json.loads(posts[1][1])
    assert assign == {"project_id": p.id, "part_id": "a.hair", "tripo_plan": "free", "task_link": "https://example.com/task/123"}
    expect(ui.page.locator(".toast", has_text="Imported")).to_have_count(1)
    ui.no_errors()


def test_cancel_a_manual_import_asks_first(ui, live, plain_gates):
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.BUILDING)
    gate = manual_gate(live, p)
    ui.goto(f"/p/{p.id}/build")
    ui.page.get_by_role("button", name="Cancel and go back").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Stop waiting for this file?")
    dlg.get_by_role("button", name="Cancel and go back").click()
    wait_for(lambda: decisions(live, gate), 5, message="the cancel")
    assert decisions(live, gate)[0].action.value == "cancel"
    expect(ui.page.locator(".gate-panel.manual")).to_have_count(0)


def test_unassigned_imports_are_matched_to_a_part(ui, live):
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.BUILDING)
    seed.board_gate(live.rt, p)
    calls: list[dict] = []
    ui.page.route(re.compile(r"/api/inbox$"), lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"entries": [], "inbox_dir": "C:\\inbox", "unassigned": [{"id": "inb_7", "name": "tripo_model.glb", "size": 52000, "state": "ready", "assigned_part": None}]})))
    ui.page.route(re.compile(r"/api/imports/inb_7/assign$"), lambda r: (calls.append(r.request.post_data_json), r.fulfill(status=200, content_type="application/json", body="{}")))
    ui.goto(f"/p/{p.id}/build")
    row = ui.page.locator("li.row", has_text="tripo_model.glb")
    expect(row).to_contain_text("51 KB")
    row.get_by_role("button", name="Match it").click()
    expect(ui.page.locator(".toast", has_text="Choose which part")).to_have_count(1)
    row.get_by_label("Part for tripo_model.glb").select_option(label="A · Hair")
    row.get_by_role("button", name="Match it").click()
    ui.wait_until(lambda: calls, 5, "the assign call")
    assert calls[0]["part_id"] == "a.hair" and calls[0]["project_id"] == p.id and calls[0]["tripo_plan"] == "not_tripo"


def test_slow_tripo_step_reads_as_normal_and_failures_have_a_hint(ui, live):
    p = seed.project(live.rt, "Moon Tea", "gg", Stage.BUILDING)
    seed.board_gate(live.rt, p)
    j = seed.job(live.rt, p.id, JobKind.BUILD)
    now = utcnow()
    live.rt.repo.insert_steps([
        Step(id="stp_slow", job_id=j.id, project_id=p.id, part_id="a.hair", kind="tripo.model", pool="api", state=StepState.WAITING_REMOTE, remote_state="slow", paid=True, progress=0.6, created_at=now,
             not_before=now + timedelta(hours=1)),
        Step(id="stp_ok", job_id=j.id, project_id=p.id, part_id="a.shirt", kind="clothing.compose", state=StepState.SUCCEEDED, progress=1.0, created_at=now, finished_at=now),
    ])
    ui.goto(f"/p/{p.id}/build")
    slow = ui.page.locator(".build-part", has_text="A · Hair")
    expect(slow).to_contain_text("Making the 3D model")
    expect(slow).to_contain_text("Taking a bit longer than usual. That is normal for 3D models")
    assert "slow" not in slow.inner_text().lower().replace("slowly", "")                        # the code word is never shown
    expect(ui.page.locator(".build-part", has_text="A · Shirt")).to_contain_text("Done")
    expect(ui.page.get_by_text("1 of 2 steps done")).to_be_visible()
    ui.shot("build_progress")
    ui.no_errors()
