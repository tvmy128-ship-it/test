"""Manual 3D: the Tripo pack (8 files, an empty return folder, a unique pack id), the inbox watcher, the import wizard's rules, the polish pack."""
from __future__ import annotations

import time

import pytest
from pfix import give_views, make_project

from duoskin.pipeline import manual_mesh, mesh_import, parts, tripo_pack


@pytest.fixture
def board(rt):
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    give_views(rt, p.id, "a.acc.0")
    give_views(rt, p.id, "a.hair", with_hair_only=True)
    return p


def test_pack_has_the_eight_files_and_an_empty_return_folder(rt, board):
    st = manual_mesh.build_pack_for(rt, board.id, rt.repo.get_part(board.id, "a.acc.0"))
    from pathlib import Path

    folder = Path(st["folder"])
    files = sorted(f.name for f in folder.iterdir() if f.is_file())
    assert files == sorted(tripo_pack.PACK_FILES) and len(files) == 8
    assert (folder / "return").is_dir() and not list((folder / "return").iterdir())
    assert not tripo_pack.verify_pack(folder)
    assert st["pack_id"].startswith("DS-") and st["pack_id"] in folder.name and st["pack_id"] in (folder / "SETTINGS.txt").read_text()
    assert rt.repo.kv_get(f"packid:{st['pack_id']}") == {"project_id": board.id, "part_id": "a.acc.0"}


def test_pack_ids_are_unique_per_pack(rt, board):
    a = manual_mesh.build_pack_for(rt, board.id, rt.repo.get_part(board.id, "a.acc.0"))
    b = manual_mesh.build_pack_for(rt, board.id, rt.repo.get_part(board.id, "a.acc.0"))
    assert a["pack_id"] != b["pack_id"]


def test_a_pack_needs_the_four_views(rt):
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    with pytest.raises(ValueError, match="four views"):
        manual_mesh.build_pack_for(rt, p.id, rt.repo.get_part(p.id, "a.acc.0"))


def test_the_hair_pack_uses_the_hair_only_front(rt, board):
    import json
    from pathlib import Path

    st = manual_mesh.build_pack_for(rt, board.id, rt.repo.get_part(board.id, "a.hair"))
    asset = json.loads((Path(st["folder"]) / "asset.json").read_text())
    assert asset["views_variant"] == "hair_only"
    assert "head" in (Path(st["folder"]) / "SETTINGS.txt").read_text().lower()


def test_inbox_watcher_ignores_partial_downloads_and_waits_for_a_stable_size(rt, tmp_path):
    w = manual_mesh.watcher(rt)
    inbox = manual_mesh.inbox_dir(rt)
    (inbox / "model.glb.crdownload").write_bytes(b"glTF" + b"0" * 100)
    (inbox / "x.part").write_bytes(b"glTF")
    f = inbox / "DS-rin-kai-a-acc-0-3fa9c1.glb"
    f.write_bytes(b"glTF" + b"0" * 50)
    assert w.poll_once() == []                                   # first sight: one size only
    f.write_bytes(b"glTF" + b"0" * 80)                           # still growing
    assert w.poll_once() == []
    ids = w.poll_once()                                          # the same size on two polls in a row: stable (and the file opens exclusively)
    assert len(ids) == 1
    entries = manual_mesh.inbox_entries(rt)
    names = {e["name"]: e for e in entries}
    assert "model.glb.crdownload" not in names and "x.part" not in names
    assert names["DS-rin-kai-a-acc-0-3fa9c1.glb"]["state"] == "ready" and names["DS-rin-kai-a-acc-0-3fa9c1.glb"]["pack_id"] == "DS-rin-kai-a-acc-0-3fa9c1"
    assert w.poll_once() == []                                   # a ready file is not announced twice


def test_a_file_in_a_pack_return_folder_is_assigned_to_that_pack(rt, board):
    from pathlib import Path

    st = manual_mesh.build_pack_for(rt, board.id, rt.repo.get_part(board.id, "a.acc.0"))
    # the watcher only watches the return folder of an OPEN manual gate: open one by hand
    from duoskin.models.common import utcnow
    from duoskin.models.gate import Gate, GateAction, GateKind, GateTile, TileState

    tile = GateTile(tile_id="a.acc.0", part_id="a.acc.0", label="x", state=TileState.WAITING_MANUAL, facts={"return_dir": st["return_dir"], "pack_id": st["pack_id"]},
                    allowed_actions=[GateAction.CANCEL])
    rt.repo.insert_gate(Gate(id="gat_t1", project_id=board.id, job_id="job_x", kind=GateKind.MANUAL_IMPORT, tiles=[tile], opened_at=utcnow()))
    (Path(st["return_dir"]) / "whatever.glb").write_bytes(b"glTF" + b"1" * 64)
    w = manual_mesh.watcher(rt)
    for _ in range(4):
        w.poll_once()
    e = next(x for x in manual_mesh.inbox_entries(rt) if x["name"] == "whatever.glb")
    assert e["state"] == "assigned" and e["assigned_part"] == "a.acc.0" and e["pack_id"] == st["pack_id"]


def test_ingest_cleans_the_name_and_refuses_partials(rt):
    e = manual_mesh.ingest_upload(rt, "../../evil name?.glb", b"glTF" + b"2" * 40)
    assert "/" not in e["name"] and ".." not in e["name"] and e["state"] == "ready"
    with pytest.raises(ValueError, match="unfinished"):
        manual_mesh.ingest_upload(rt, "m.glb.crdownload", b"x")


def test_the_licence_flag_and_banner_follow_the_plan():
    assert mesh_import.licence_from_plan("free") == "tripo_free_public_ccby_noncommercial"
    assert mesh_import.licence_from_plan("not_tripo") == "user_made" and mesh_import.licence_from_plan("paid") == "tripo_paid_private_commercial"
    assert "FREE plan" in mesh_import.licence_banner("tripo_free_public_ccby_noncommercial") and mesh_import.licence_banner("user_made") is None
    assert not mesh_import.sell_ready_allowed("tripo_free_public_ccby_noncommercial")


def test_assign_refuses_an_unknown_plan_and_entry(rt, board):
    with pytest.raises(KeyError):
        manual_mesh.assign_import(rt, "ib_nope", board.id, "a.acc.0", tripo_plan="free")
    e = manual_mesh.ingest_upload(rt, "m.glb", b"glTF" + b"3" * 40)
    with pytest.raises(ValueError, match="tripo_plan"):
        manual_mesh.assign_import(rt, e["id"], board.id, "a.acc.0", tripo_plan="gold")


def test_polish_pack_contents(rt, board):
    from pathlib import Path

    from duoskin.pipeline import polish

    glb = rt.cas.put(open("duoskin/providers/fixtures/meshes/hair_with_grey_head.glb", "rb").read(), "glb", prov=__import__("duoskin.pipeline.common", fromlist=["prov"]).prov("code"))
    rt.repo.mutate_part(board.id, "a.hair", lambda p: setattr(p, "build_assets", {"glb_archive": glb.sha256}))
    st = polish.build_polish_pack(rt, board.id, "a.hair")
    folder = Path(st["folder"])
    names = sorted(f.name for f in folder.iterdir())
    assert {"hair_fitted.glb", "head_guide.glb", "POLISH.txt", "return", "view_front.png", "view_left.png", "view_back.png", "view_right.png"} <= set(names)
    assert not list((folder / "return").iterdir())
    assert "3600" in (folder / "POLISH.txt").read_text() and "head_guide" in (folder / "POLISH.txt").read_text()
    assert time.time() > 0
