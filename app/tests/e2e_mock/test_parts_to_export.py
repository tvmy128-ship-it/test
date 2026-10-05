"""Mock-mode end to end, PARTS to EXPORT (APP_SPEC 17.4): Gate 2, BUILD (API mode with the mock Tripo), DUO, the Gate 3 pick and the export kit.

The mock sources are blocked at CHK-E01 for real; ``export.allow_mock()`` (a module flag, never a setting) lets one test write the kit, and then
every file name carries MOCK.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pfix import dump_steps, part_states, post_decision, wait_for, wait_gate, wait_gate_checked

from duoskin.pipeline import export


def to_gate3(rt, client, project_id: str):
    """Approve every tile at Gate 2 and wait for the Gate 3 gate (BUILD and DUO run in between)."""
    gate = wait_gate(rt, project_id, "part_board", timeout=30)
    r = post_decision(client, gate.id, "a.colours", "approve_all")
    assert r.status_code == 200, r.text
    try:
        return wait_gate_checked(rt, project_id, "final_pick", timeout=600)
    except AssertionError:
        print(dump_steps(rt, project_id, states=("failed", "waiting_user", "waiting_remote")))
        raise


def pick(client, gate, note: str = "checked by the test"):
    r = post_decision(client, gate.id, "c1", "pick", text=note)
    assert r.status_code == 200, r.text
    return r


def export_status(client, project_id: str, want: tuple[str, ...], timeout: float = 240.0) -> dict:
    return wait_for(lambda: (lambda d: d if d["status"] in want else None)(client.get(f"/api/exports/{project_id}").json()), timeout=timeout,
                    message=f"the export to be one of {want}")


@pytest.mark.timeout(900)
def test_gate2_to_gate3_builds_every_part_and_the_duo(board):
    rt, client, p = board
    t0 = time.time()
    g3 = to_gate3(rt, client, p.id)
    print("gate3 in", round(time.time() - t0), "s")
    states = part_states(rt, p.id)
    assert {s for pid, s in states.items() if pid != "duo"} == {"built"}, states
    assert rt.repo.get_project(p.id).stage.value == "gate3"
    tile = g3.tiles[0]
    assert tile.state.value == "ready" and tile.tile_id == "c1"
    assert any("DEMO" in b for b in tile.badges), tile.badges                  # mock sources: nothing here can be exported
    assert {"sheet", "phone_strip", "face_poses", "a.front", "b.front"} <= set(tile.assets)
    assert tile.facts["blocking"] == []
    assert tile.facts["ip"] is not None and tile.facts["checks"] is not None


@pytest.mark.timeout(900)
def test_pick_then_export_is_blocked_for_mock_sources_and_allowed_in_tests(board):
    rt, client, p = board
    g3 = to_gate3(rt, client, p.id)
    r = client.post(f"/api/projects/{p.id}/export")
    assert r.status_code == 409 and r.json()["error"] == "pick_first"        # the pick comes first
    pick(client, g3)
    wait_for(lambda: rt.repo.get_project(p.id).stage.value == "gate3" and rt.repo.kv_get(f"duo:{p.id}") is not None, timeout=30, message="the pick to settle")

    # 1. the real gate: the mock sources block the export at CHK-E01
    r = client.post(f"/api/projects/{p.id}/export")
    assert r.status_code == 200, r.text
    blocked = export_status(client, p.id, ("blocked", "failed", "done"))
    assert blocked["status"] == "blocked", blocked
    assert "CHK-E01" in blocked["checks"], blocked
    assert "nothing here can be exported" in (blocked["reason"] or "") or "mock" in (blocked["reason"] or "").lower()
    assert not Path(rt.effective_settings().paths.exports_root).exists() or not list(Path(rt.effective_settings().paths.exports_root).glob("*/*Upload*"))

    # 2. the test-only flag: the kit is written, every file name carries MOCK
    with export.allow_mock():
        wait_for(lambda: rt.repo.get_project(p.id).stage.value in ("gate3", "exported"), timeout=30, message="the stage to return")
        r = client.post(f"/api/projects/{p.id}/export")
        assert r.status_code == 200, r.text
        done = export_status(client, p.id, ("done", "failed", "blocked"), timeout=420)
    assert done["status"] == "done", done
    assert done["mock"] is True
    kit = Path(done["kit_dir"])
    files = [f for f in kit.rglob("*") if f.is_file()]
    assert files
    assert all("MOCK" in f.name for f in files), [f.name for f in files if "MOCK" not in f.name]
    names = {f.name for f in files}
    assert any(n.endswith("provenance.json") for n in names) and any(n.endswith("manifest.json") for n in names)
    assert any("checklist" in n.lower() for n in names)
    prov = json.loads(next(f for f in files if f.name.endswith("provenance.json")).read_text(encoding="utf-8"))
    assert prov["schema"] == "duoskin.provenance/1" and any("mock" in n for n in prov["notes"])
    assert all(it["lineage"] and it["build_hash"] and it["approval_hash"] and it["license"] for it in prov["items"])
    assert {it["item_id"] for it in prov["items"]} >= {"a.shirt", "a.pants", "a.face", "a.hair", "a.acc.0", "a.colours", "b.shirt", "b.hair", "b.acc.0"}
    manifest = json.loads(next(f for f in files if f.name.endswith("manifest.json")).read_text(encoding="utf-8"))
    assert manifest["schema"] == "duoskin.manifest/1" and manifest["mock"] is True
    assert {it["type"] for it in manifest["items"]} >= {"Shirt", "Pants", "FaceLayers", "Hair", "Waist", "Shoulder", "Body"} and "Head" not in {it["type"] for it in manifest["items"]}
    # no head base: the face is the layer pack (a head texture is not made), and there is no Head item
    assert not any("head" in f.name.lower() and f.suffix in (".fbx", ".glb", ".gltf") for f in files)
    assert any("layer" in f.name.lower() or "face" in f.name.lower() for f in files)
    assert any("face_layers" in f.parts for f in files) and not any(f.suffix == ".fbx" for f in files)     # no Blender: no FBX, said in the manifest
    assert (rt.repo.get_project(p.id).stage.value) == "exported"
    assert done["zip"] and Path(done["zip"]).is_file()

    # the checklist: the item-type rows, a step that is not locked can be ticked, one that is locked cannot (APP_SPEC 10.13)
    cl = json.loads(next(f for f in files if f.name.endswith("checklist.json")).read_text(encoding="utf-8"))
    assert {it["type"] for it in cl["items"]} >= {"Shirt", "Pants", "Hair", "Waist", "Shoulder"}
    assert any("DEMO" in b or "demo" in b for b in cl["banners"]), cl["banners"]
    live = client.get(f"/api/exports/{p.id}").json()
    open_step = next((it, s) for it in live["checklist"]["items"] for s in it["steps"] if not s["locked"] and not s["ticked"])
    r = client.patch(f"/api/exports/{p.id}/checklist", json={"item_id": open_step[0]["item_id"], "step_id": open_step[1]["step_id"], "ticked": True,
                                                              "expected_version": live["version"]})
    assert r.status_code == 200, r.text
    locked = next(((it, s) for it in r.json()["checklist"]["items"] for s in it["steps"] if s["locked"]), None)
    if locked is not None:
        r2 = client.patch(f"/api/exports/{p.id}/checklist", json={"item_id": locked[0]["item_id"], "step_id": locked[1]["step_id"], "ticked": True,
                                                                   "expected_version": r.json()["version"]})
        assert r2.status_code == 422 and r2.json()["error"] == "locked"


# ---------------------------------------------------------------------------------------------------- Gate 2 changes
def snapshot(rt, project_id: str) -> dict[str, dict]:
    return {x.id: {"assets": dict(x.board_assets), "state": x.state.value} for x in rt.repo.list_parts(project_id)}


def settle(rt, project_id: str, part_ids, timeout: float = 240.0):
    """Wait until each part is READY or APPROVED again after a redo."""
    def done():
        st = {pid: rt.repo.get_part(project_id, pid).state.value for pid in part_ids}
        return st if all(v in ("ready", "approved") for v in st.values()) else None

    return wait_for(done, timeout=timeout, message=f"{list(part_ids)} to settle")


@pytest.mark.timeout(600)
def test_a_change_at_gate_2_redoes_only_the_affected_tiles(board):
    rt, client, p = board
    gate = wait_gate(rt, p.id, "part_board", timeout=30)
    before = snapshot(rt, p.id)
    old_spec = rt.repo.get_spec(rt.repo.get_project(p.id).approved_spec_id)
    cost_before = rt.repo.get_project(p.id).spent_usd
    old_ids = {s.id for s in rt.repo.list_steps(project_id=p.id, limit=5000)}

    r = post_decision(client, gate.id, "b.shirt", "change", text="make B's jacket teal")
    assert r.status_code == 200, r.text
    cg = wait_gate(rt, p.id, "change_confirm", timeout=90)
    facts = cg.tiles[0].facts
    redo = {e["part_id"]: e["effect"] for e in facts["redo"]}
    assert redo.get("b.shirt") == "recompose", redo
    assert set(redo.values()) <= {"recompose", "recheck"}, redo           # a colour change costs nothing: no tile is regenerated
    assert not any(pid in redo for pid in ("a.hair", "b.hair", "a.acc.0", "b.acc.0", "a.pants", "b.pants")), redo
    assert facts["estimate_usd"] == 0.0 and facts["diff"], facts

    r = post_decision(client, cg.id, cg.tiles[0].tile_id, "confirm")
    assert r.status_code == 200, r.text
    new_rec = wait_for(lambda: (lambda s: s if s.version > old_spec.version else None)(rt.repo.get_spec(rt.repo.get_project(p.id).approved_spec_id)), timeout=30,
                       message="the new spec")
    assert new_rec.parent_spec_id == old_spec.id and new_rec.created_by == "change"
    settle(rt, p.id, list(redo))
    after = snapshot(rt, p.id)
    redone = {pid for pid in before if after[pid]["assets"] != before[pid]["assets"]}
    assert "b.shirt" in redone, redone
    assert redone <= set(redo), (redone, redo)                            # nothing else was touched
    for pid in before:
        if pid not in redo:
            assert after[pid]["assets"] == before[pid]["assets"] and after[pid]["state"] == before[pid]["state"], pid
    paid_kinds = {s.kind for s in rt.repo.list_steps(project_id=p.id, limit=5000) if s.id not in old_ids and s.paid}
    assert paid_kinds <= {"partchange.interpret"}, paid_kinds              # only the question to the planner (L7) cost anything: no image was made
    assert rt.repo.get_project(p.id).spent_usd - cost_before < 0.2


@pytest.mark.timeout(600)
def test_reimagine_uses_a_new_nonce_and_regenerates_only_that_tile(board):
    rt, client, p = board
    gate = wait_gate(rt, p.id, "part_board", timeout=30)
    before = snapshot(rt, p.id)
    old = rt.repo.list_steps(project_id=p.id, limit=5000)
    old_steps = {s.id for s in old}
    nonces_before = {s.nonce for s in old if s.part_id == "b.hair"}
    r = post_decision(client, gate.id, "b.hair", "reimagine")
    assert r.status_code == 200, r.text
    wait_for(lambda: rt.repo.get_part(p.id, "b.hair").state.value in ("generating", "stale", "composing", "checking", "recheck", "ready"), timeout=30, message="start")
    settle(rt, p.id, ["b.hair"], timeout=300)
    new_steps = [s for s in rt.repo.list_steps(project_id=p.id, limit=5000) if s.id not in old_steps]
    nonces = {s.nonce for s in new_steps if s.nonce}
    assert nonces and not (nonces & nonces_before), (nonces_before, nonces)         # a new nonce: the cache cannot return the old picture
    assert {s.part_id for s in new_steps if s.part_id} == {"b.hair"}, {s.part_id for s in new_steps}
    after = snapshot(rt, p.id)
    assert after["b.hair"]["assets"].get("front") != before["b.hair"]["assets"].get("front")
    for pid in before:
        if pid != "b.hair":
            assert after[pid]["assets"] == before[pid]["assets"], pid
