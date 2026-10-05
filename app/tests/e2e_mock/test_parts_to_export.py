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
    return wait_for(lambda: (d if (d := client.get(f"/api/exports/{project_id}").json())["status"] in want else None), timeout=timeout,
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
    mem = wait_for(lambda: next((s for s in rt.repo.list_steps(project_id=p.id, limit=3000) if s.kind == "duo.memory" and s.state.value == "succeeded"), None),
                   timeout=60, message="the duo to be remembered")
    assert mem.result["registered"] == 2                                              # both faces are remembered for the next duos; mock prints never enter a registry
    assert rt.db.conn().execute("SELECT COUNT(*) FROM registry_face").fetchone()[0] == 2
    assert rt.db.conn().execute("SELECT COUNT(*) FROM registry_print").fetchone()[0] == 0
    assert rt.db.conn().execute("SELECT COUNT(*) FROM duo_memory WHERE project_id=?", (p.id,)).fetchone()[0] == 1

    # 1. the real gate: the mock sources block the export at CHK-E01
    r = client.post(f"/api/projects/{p.id}/export")
    assert r.status_code == 200, r.text
    blocked = export_status(client, p.id, ("blocked", "failed", "done"))
    assert blocked["status"] == "blocked", blocked
    assert "CHK-E01" in blocked["checks"], blocked
    assert blocked["mock"] is True                                                       # the block says WHY: practice (mock) sources
    preview = blocked["preview"]                                                          # demo mode: the block still shows what a real run would hand over
    assert preview and {i["type"] for i in preview["items"]} >= {"Shirt", "Pants", "Hair", "Waist", "Shoulder"}
    assert preview["checklist"]["items"] and all(it["steps"] for it in preview["checklist"]["items"])
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
    # the preview lists the items the kit really has (the face layer pack and a body without a body base are not uploaded)
    assert {i["item_id"] for i in preview["items"]} == {it["item_id"] for it in manifest["items"] if it["type"] not in ("FaceLayers", "Body")}
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
    new_rec = wait_for(lambda: (sp if (sp := rt.repo.get_spec(rt.repo.get_project(p.id).approved_spec_id)).version > old_spec.version else None), timeout=30,
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


# ---------------------------------------------------------------------------------------------------- manual 3D
def tripo_glb(rt, project_id: str, part_id: str, seed: int = 11) -> bytes:
    """What the user would download from Tripo's website for this tile: the mock Tripo builds the model from the tile's own multiview task."""
    from duoskin.pipeline import common, itemspec
    from duoskin.providers import registry as pr
    from duoskin.providers import tripo as T

    _, spec = common.load_spec(rt, project_id)
    item = itemspec.item_of(spec, part_id)
    ad = pr.get("tripo")
    tid = ad.multiview_to_model(rt.repo.kv_get(f"mvtask:{project_id}:{part_id}")["task_id"], T.P2Params.for_seed(item.face_limit, seed))
    st = wait_for(lambda: (st if (st := ad.task(tid)).done else None), timeout=60, message="the mock model")
    return ad.download_files(tid, ["model_url"], status=st)["model_url"].data


def manual_gates(rt, project_id: str) -> dict:
    return {g.tiles[0].part_id: g for g in rt.repo.list_gates(project_id, "open") if g.kind.value == "manual_import"}


@pytest.mark.timeout(900)
def test_manual_3d_pack_inbox_wizard_import_and_free_plan_banner(board):
    from duoskin.pipeline import tripo_pack

    rt, client, p = board
    rt.repo.mutate_project(p.id, lambda x: setattr(x.settings, "mesh_mode", "manual"))
    gate = wait_gate(rt, p.id, "part_board", timeout=30)
    assert post_decision(client, gate.id, "a.colours", "approve_all").status_code == 200
    gates = wait_for(lambda: (d if {"a.acc.0", "b.acc.0"} <= set(d := manual_gates(rt, p.id)) else None), timeout=300, message="the two manual gates")
    assert set(gates) == {"a.acc.0", "b.acc.0"}                      # the kit hair, the slab and the shirts need no 3D service
    fa, fb = gates["a.acc.0"].tiles[0].facts, gates["b.acc.0"].tiles[0].facts

    # the pack: 8 files, an empty return folder, a pack id that is in the folder name, the SETTINGS text and the file name for the inbox
    folder = Path(fa["folder"])
    assert sorted(f.name for f in folder.iterdir() if f.is_file()) == sorted(tripo_pack.PACK_FILES) and len(tripo_pack.PACK_FILES) == 8
    assert (folder / "return").is_dir() and not list((folder / "return").iterdir())
    assert fa["pack_id"].startswith("DS-") and fa["pack_id"] in folder.name and fa["pack_id"] != fb["pack_id"]
    assert "FREE plan" in fa["free_plan_warning"] or "FREE" in fa["free_plan_warning"]
    assert gates["a.acc.0"].tiles[0].state.value == "waiting_manual"

    # a partial download is not a model; the Tripo model dropped in the inbox under the pack id is picked up once its size is stable
    inbox = Path(client.get("/api/inbox").json()["inbox_dir"])
    (folder / "return" / "model.glb.crdownload").write_bytes(b"glTF" + b"0" * 100)
    (inbox / "model.glb.crdownload").write_bytes(b"glTF" + b"0" * 100)
    (inbox / f"{fa['pack_id']}.glb").write_bytes(tripo_glb(rt, p.id, "a.acc.0"))
    entry = wait_for(lambda: next((e for e in client.get("/api/inbox").json()["entries"] if e["name"].startswith(fa["pack_id"]) and e["state"] in ("ready", "assigned")),
                                  None), timeout=30, message="the model in the inbox")
    assert entry["assigned_part"] == "a.acc.0"                     # the pack id in the file name says which tile it is for
    assert not [e for e in client.get("/api/inbox").json()["entries"] if e["name"].endswith(".crdownload")]

    # the wizard: a file from Tripo's FREE plan is accepted with a loud flag; it is public and not licensed for commercial use
    r = client.post(f"/api/imports/{entry['id']}/assign", json={"project_id": p.id, "part_id": "a.acc.0", "tripo_plan": "free"})
    assert r.status_code == 200, r.text
    assert r.json()["licence"] == "tripo_free_public_ccby_noncommercial" and "FREE" in r.json()["banner"]
    assert "licence_free_plan" in rt.repo.get_part(p.id, "a.acc.0").flags

    # a model the user made themselves ("not Tripo"): accepted as user_made, dropped straight on the tile
    up = client.post("/api/imports", files={"file": ("my_plush.glb", tripo_glb(rt, p.id, "b.acc.0"), "model/gltf-binary")},
                     data={"project_id": p.id, "part_id": "b.acc.0"})
    assert up.status_code == 200, up.text
    r = client.post(f"/api/imports/{up.json()['id']}/assign", json={"project_id": p.id, "part_id": "b.acc.0", "tripo_plan": "not_tripo"})
    assert r.status_code == 200 and r.json()["licence"] == "user_made", r.text

    try:
        g3 = wait_gate_checked(rt, p.id, "final_pick", timeout=600)
    except AssertionError:
        print(dump_steps(rt, p.id, states=("failed", "waiting_user", "waiting_remote")))
        raise
    states = part_states(rt, p.id)
    assert states["a.acc.0"] == "built" and states["b.acc.0"] == "built", states
    pa, pb = rt.repo.get_part(p.id, "a.acc.0"), rt.repo.get_part(p.id, "b.acc.0")
    assert pa.license == "tripo_free_public_ccby_noncommercial" and pb.license == "user_made", (pa.license, pb.license)
    assert any("FREE" in w or "free" in w for w in g3.tiles[0].badges + [b for b in g3.tiles[0].facts["banners"]]), g3.tiles[0].badges

    # the free-plan flag follows the model into the export kit
    pick(client, g3)
    wait_for(lambda: rt.repo.kv_get(f"duo:{p.id}") is not None, timeout=30, message="the pick")
    with export.allow_mock():
        assert client.post(f"/api/projects/{p.id}/export").status_code == 200
        done = export_status(client, p.id, ("done", "failed", "blocked"), timeout=420)
    assert done["status"] == "done", done
    assert any("FREE" in b or "free plan" in b.lower() for b in done["banners"]), done["banners"]
    manifest = json.loads(next(Path(done["kit_dir"]).glob("*manifest.json")).read_text(encoding="utf-8"))
    lic = {it["item_id"]: it["license"] for it in manifest["items"]}
    assert lic["a.acc.0"] == "tripo_free_public_ccby_noncommercial" and lic["b.acc.0"] == "user_made"


def lopsided_glb() -> bytes:
    """A textured body with a large lobe on its +X side and a horn on the other (chiral: no rotation gives its mirror image), as GLB bytes."""
    import trimesh

    from duoskin.providers.mock import meshes as M

    body = M.grid_box(9, (0.3, 0.5, 0.2), 0.2)
    lobe = M.uv_sphere(16, 12, 0.3, (0.55, 0.15, 0.3))
    horn = M.uv_sphere(12, 8, 0.16, (-0.3, 0.55, -0.2))          # off every mirror plane: no rotation turns the model into its mirror image
    v, f, uv = M._merge([body, lobe, horn])
    mat = trimesh.visual.material.PBRMaterial(name="m", baseColorTexture=M.make_texture("rounded_box", (200, 120, 60), 7), metallicFactor=0.0, roughnessFactor=1.0)
    return bytes(trimesh.Trimesh(vertices=v, faces=f, visual=trimesh.visual.TextureVisuals(uv=uv, material=mat), process=False).export(file_type="glb"))


@pytest.mark.timeout(600)
def test_a_mirrored_model_offers_the_flip_and_is_never_flipped_by_itself(make_runtime):
    import io

    import numpy as np
    import trimesh
    from pfix import make_project
    from PIL import Image

    from duoskin.pipeline import common, manual_mesh, multiview, parts
    from duoskin.providers.mock import meshes

    rt, client = make_runtime()
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    glb = lopsided_glb()                                                           # a body with a big lobe on one side: left and right differ clearly
    assets = {}
    for name, png in meshes.render_glb_views(glb, 384).items():
        buf = io.BytesIO()
        multiview.subject_alpha(Image.open(io.BytesIO(png))).save(buf, "PNG")
        assets[f"view.{name}"] = rt.cas.put(buf.getvalue(), "png", prov=common.prov("mock", params={"test": name})).sha256
    assets["front"] = assets["view.front"]
    parts.set_board_assets(rt, p.id, "a.acc.0", assets)
    scene = trimesh.load(io.BytesIO(glb), file_type="glb", force="scene")
    m = next(iter(scene.geometry.values()))
    m.apply_transform(np.diag([-1.0, 1.0, 1.0, 1.0]))
    m.invert()                                                                     # a mirror image with outward faces: what a wrong export looks like
    mirrored = bytes(m.export(file_type="glb"))

    manual_mesh.start_manual(rt, p.id, "a.acc.0", reason="test")
    wait_for(lambda: manual_gates(rt, p.id).get("a.acc.0"), timeout=60, message="the manual gate")
    entry = manual_mesh.ingest_upload(rt, "mirrored.glb", mirrored, project_id=p.id, part_id="a.acc.0")
    r = client.post(f"/api/imports/{entry['id']}/assign", json={"project_id": p.id, "part_id": "a.acc.0", "tripo_plan": "not_tripo"})
    assert r.status_code == 200, r.text

    def offered():
        g = manual_gates(rt, p.id).get("a.acc.0")
        return g if g is not None and "flip_mirrored" in [a.value for a in g.tiles[0].allowed_actions] else None

    try:
        g = wait_for(offered, timeout=180, message="the flip offer")
    except AssertionError:
        print(dump_steps(rt, p.id))
        raise
    part = rt.repo.get_part(p.id, "a.acc.0")
    assert "mirrored" in part.flags and part.state.value == "waiting_manual" and not part.build_assets
    assert "left" in g.tiles[0].facts["reason"].lower() or "mirror" in g.tiles[0].facts["reason"].lower(), g.tiles[0].facts["reason"]
    assert not [s for s in rt.repo.list_steps(project_id=p.id, limit=500) if s.kind == "mesh.flip"]       # nothing is flipped without the click

    r = post_decision(client, g.id, "a.acc.0", "flip_mirrored")
    assert r.status_code == 200, r.text
    wait_for(lambda: rt.repo.get_part(p.id, "a.acc.0").build_assets.get("gltf"), timeout=180, message="the flipped model to pass")
    flips = [s for s in rt.repo.list_steps(project_id=p.id, limit=500) if s.kind == "mesh.flip"]
    assert len(flips) == 1 and flips[0].state.value == "succeeded"
    done = rt.repo.get_part(p.id, "a.acc.0")
    assert "mirrored" not in done.flags and "flip_applied" in done.flags           # the click is logged on the tile


# ---------------------------------------------------------------------------------------------------- budget gate and crash recovery
def tripo_submits(op: str = "multiview_to_model") -> list:
    from duoskin.providers import registry as pr

    return [r for r in pr.get("tripo").requests if r.operation == op]


@pytest.mark.timeout(900)
def test_budget_gate_stop_fails_the_step_with_budget_and_opens_the_tripo_pack(board):
    rt, client, p = board
    rt.repo.mutate_project(p.id, lambda x: setattr(x.settings, "ask_above_usd", 0.5))       # a Tripo model (about $1.1) now needs a yes
    submits_before = len(tripo_submits())
    gate = wait_gate(rt, p.id, "part_board", timeout=30)
    assert post_decision(client, gate.id, "a.colours", "approve_all").status_code == 200

    def budget_gates():
        return {g.tiles[0].part_id: g for g in rt.repo.list_gates(p.id, "open") if g.kind.value == "budget"}

    gates = wait_for(lambda: (d if {"a.acc.0", "b.acc.0"} <= set(d := budget_gates()) else None), timeout=300, message="the two budget gates")
    ga, gb = gates["a.acc.0"], gates["b.acc.0"]
    ta, tb = ga.tiles[0], gb.tiles[0]
    assert ta.facts["reason"] == "ask" and ta.facts["step_kind"] == "tripo.model" and ta.facts["estimate_usd"] > 0.5
    assert len(tripo_submits()) == submits_before                              # nothing was sent before the yes

    assert post_decision(client, ga.id, ta.tile_id, "stop").status_code == 200
    assert post_decision(client, gb.id, tb.tile_id, "continue").status_code == 200
    stopped = rt.repo.get_step(ta.tile_id)
    assert stopped.state.value == "failed" and stopped.error.code == "budget" and stopped.message == "budget"
    assert rt.repo.get_step(tb.tile_id).state.value != "failed"

    # the stopped tile is not left hanging: the Tripo pack opens with the reason; the other tile goes on through the API
    mg = wait_for(lambda: manual_gates(rt, p.id).get("a.acc.0"), timeout=60, message="the Tripo pack for the stopped tile")
    assert "budget" in mg.tiles[0].facts["reason"] and mg.tiles[0].state.value == "waiting_manual"
    wait_for(lambda: rt.repo.get_part(p.id, "b.acc.0").state.value == "built", timeout=300, message="the continued tile to be built")
    assert len(tripo_submits()) == submits_before + 1                          # one model was paid for, the stopped one never was

    entry = __import__("duoskin.pipeline.manual_mesh", fromlist=["x"]).ingest_upload(rt, "my_bag.glb", tripo_glb(rt, p.id, "a.acc.0"), project_id=p.id, part_id="a.acc.0")
    r = client.post(f"/api/imports/{entry['id']}/assign", json={"project_id": p.id, "part_id": "a.acc.0", "tripo_plan": "paid"})
    assert r.status_code == 200 and r.json()["licence"] == "tripo_paid_private_commercial", r.text
    g3 = wait_gate_checked(rt, p.id, "final_pick", timeout=600, allow_failed=("tripo.model",))
    assert g3.tiles[0].state.value == "ready"
    assert rt.repo.get_part(p.id, "a.acc.0").state.value == "built"


@pytest.mark.timeout(900)
def test_a_restart_while_tripo_builds_resumes_the_poll_and_never_submits_twice(board_home, app_opener):
    from duoskin.providers import registry as pr

    home, project_id = board_home
    rt, client, close = app_opener(home)
    ad = pr.get("tripo")
    normal_polls = ad.running_polls
    ad.running_polls = 10_000                                                   # every new task stays "running" until the test lets it finish
    before = len(tripo_submits())
    gate = wait_gate(rt, project_id, "part_board", timeout=30)
    assert post_decision(client, gate.id, "a.colours", "approve_all").status_code == 200

    def in_flight():
        ss = [s for s in rt.repo.list_steps(project_id=project_id, limit=2000) if s.kind == "tripo.model"]
        return ss if len(ss) == 2 and all(s.remote_ref and s.state.value == "waiting_remote" for s in ss) else None

    flying = wait_for(in_flight, timeout=300, message="both Tripo tasks to be in flight")
    refs = {s.id: s.remote_ref for s in flying}
    assert len(tripo_submits()) == before + 2

    close()                                                                      # the app stops while Tripo is still working
    rt2, _, _ = app_opener(home)                                                 # ... and starts again on the same folder
    ad.running_polls = normal_polls                                              # (the mock is shared by the module: the next test must not see the stall)
    for t in ad._tasks.values():
        t.running_polls = 0                                                      # Tripo finishes while the app was away
    g3 = wait_gate_checked(rt2, project_id, "final_pick", timeout=600)
    assert g3.tiles[0].state.value == "ready"
    steps = {s.id: s for s in rt2.repo.list_steps(project_id=project_id, limit=2000) if s.kind == "tripo.model"}
    assert set(steps) == set(refs)                                               # no new tripo.model step, no new task
    assert all(steps[i].state.value == "succeeded" and steps[i].remote_ref == refs[i] and steps[i].attempt == 1 for i in refs), {i: (s.state.value, s.attempt) for i, s in steps.items()}
    assert len(tripo_submits()) == before + 2                                    # the poll resumed from the stored task id: nothing was sent twice
    assert {s.state.value for s in rt2.repo.list_steps(project_id=project_id, limit=2000)} <= {"succeeded", "skipped", "waiting_user", "cancelled"}


# ---------------------------------------------------------------------------------------------------- missing kits and keys
def run_to_gate2(rt, project_id: str, timeout: float = 400.0):
    from pfix import wait_gate_checked, wait_tiles_settled

    from duoskin.pipeline import parts

    parts.start_parts_job(rt, project_id)
    gate = wait_gate_checked(rt, project_id, "part_board", timeout=timeout)
    return wait_tiles_settled(rt, project_id, gate.id, timeout=timeout)


@pytest.mark.timeout(900)
def test_no_head_base_and_an_empty_hair_kit_build_custom_hair_through_tripo(make_runtime):
    from pfix import make_project

    from duoskin.pipeline import kits

    kits.DEMO_OVERRIDE = False                                                   # no demo kits and no user kits: the hair kit is empty
    rt, client = make_runtime()
    p, _ = make_project(rt, "spec_empty_bb", mesh_mode="api")
    assert kits.load_context(rt).flags.get("head_base_present") in (False, None)
    gate = run_to_gate2(rt, p.id)
    states = {t.tile_id: t.state.value for t in gate.tiles}
    for t in gate.tiles:
        if t.state.value != "ready":
            print(t.tile_id, t.facts.get("report"), t.facts.get("checks", {}).get("hard_failures"))
    assert all(v == "ready" for v in states.values()), states
    for h in ("a.hair", "b.hair"):
        hp = rt.repo.get_part(p.id, h)
        assert "hair_custom_no_kit" in hp.flags or "hair_custom" in hp.flags, (h, hp.flags)
        assert {f"view.{v}" for v in ("front", "left", "back", "right")} <= set(hp.board_assets)        # no kit style: the four views go to Tripo
    r = post_decision(client, gate.id, "a.colours", "approve_all")
    assert r.status_code == 200, r.text
    try:
        g3 = wait_gate_checked(rt, p.id, "final_pick", timeout=600)
    except AssertionError:
        print(dump_steps(rt, p.id, states=("failed", "waiting_user", "waiting_remote")))
        raise
    steps = rt.repo.list_steps(project_id=p.id, limit=3000)
    for h in ("a.hair", "b.hair"):
        kinds = [s.kind for s in steps if s.part_id == h and s.state.value == "succeeded"]
        assert "tripo.model" in kinds and "hair.register" in kinds and "hair.kit_match" not in kinds, (h, kinds)
        reg = next(s for s in steps if s.part_id == h and s.kind == "hair.register")
        assert reg.result["ok"] is True, reg.result.get("reason")
        m21 = [c for c in reg.result["checks"] if c["id"] == "CHK-M21"]
        assert m21 and m21[0]["passed"], m21
        hr = reg.result["facts"]["hair_register"]                                  # the registration itself: the grey cube head was found and cut out
        assert hr["head_found"] and hr["mode"] == "fiducial" and hr["watertight_after_cut"], hr
        assert hr["cut"]["tris_after"] < hr["cut"]["tris_before"] and hr["head_lo"] == [-0.6, -1.193, -0.6]
        assert rt.repo.get_part(p.id, h).state.value == "built"
    assert g3.tiles[0].state.value == "ready"


@pytest.mark.timeout(900)
def test_without_recraft_gemini_and_tripo_keys_the_pipeline_still_works(make_runtime):
    from pfix import make_project

    from duoskin.engine import registry as eng_registry
    from duoskin.pipeline import common
    from duoskin.providers.mock import meshes

    rt, _ = make_runtime(providers_mode="anthropic:mock,openai:mock,recraft:disabled,gemini:disabled,tripo:disabled")
    assert common.available_providers(rt) == {"anthropic", "openai"}
    p, _ = make_project(rt, mesh_mode="api")
    gate = run_to_gate2(rt, p.id)
    states = {t.tile_id: t.state.value for t in gate.tiles}
    assert states["a.face"] == "ready" and states["b.face"] == "ready", states               # no Recraft: the face parts come from the next route
    tiles = {t.tile_id: t for t in gate.tiles}
    for pid in ("a.acc.0", "b.acc.0"):
        assert "views_from_gpt" in rt.repo.get_part(p.id, pid).flags, rt.repo.get_part(p.id, pid).flags     # no Tripo: the views come from GPT, and say so
        assert "Views from GPT (lower reliability)" in tiles[pid].badges
    # what the mock GPT drawings cannot do is said with the check id (a real GPT set may pass); nothing fails for a routing reason
    for t in gate.tiles:
        if t.state.value != "ready":
            assert t.state.value == "needs_human" and any(c in (t.facts.get("report") or "") for c in ("A_VIEWS", "A_PALETTE")), (t.tile_id, t.facts.get("report"))
    used = {eng_registry.get(s.kind).provider for s in rt.repo.list_steps(project_id=p.id, limit=3000)} - {None}
    assert used <= {"anthropic", "openai"}, used                                  # nothing reached Recraft, Gemini or Tripo
    _ = meshes


@pytest.mark.timeout(900)
def test_without_a_tripo_key_the_build_goes_straight_to_the_tripo_packs(board_home, app_opener):
    from pfix import part_states

    home, project_id = board_home
    rt, client, _ = app_opener(home, "anthropic:mock,openai:mock,recraft:mock,gemini:mock,tripo:disabled")
    gate = wait_gate(rt, project_id, "part_board", timeout=30)
    assert post_decision(client, gate.id, "a.colours", "approve_all").status_code == 200
    gates = wait_for(lambda: (d if {"a.acc.0", "b.acc.0"} <= set(d := manual_gates(rt, project_id)) else None), timeout=300, message="the Tripo packs")
    for g in gates.values():
        assert "no Tripo key" in g.tiles[0].facts["reason"], g.tiles[0].facts["reason"]
        assert len([f for f in Path(g.tiles[0].facts["folder"]).iterdir() if f.is_file()]) == 8
    steps = rt.repo.list_steps(project_id=project_id, limit=3000)
    assert not [s for s in steps if s.kind == "tripo.model"]                                         # nothing was sent to Tripo
    states = part_states(rt, project_id)
    assert states["a.acc.0"] == "waiting_manual" and states["b.acc.0"] == "waiting_manual", states
    wait_for(lambda: rt.repo.get_part(project_id, "a.hair").state.value == "built", timeout=300, message="the kit hair (code) to be built without Tripo")
