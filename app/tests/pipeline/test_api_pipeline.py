"""The routes of the pipeline lanes (APP_SPEC 13): mask upload, manual imports, library, exports, the Tripo pack."""
from __future__ import annotations

import io
import json
import shutil

import pytest
from PIL import Image
from pfix import give_views, make_project

from duoskin.pipeline import kits, parts


def png_bytes(mode: str = "RGBA", size=(32, 32), alpha: int | None = 255) -> bytes:
    im = Image.new(mode, size, (10, 20, 30, alpha) if mode == "RGBA" else (10, 20, 30))
    if mode == "RGBA" and alpha == 255:
        im.paste((0, 0, 0, 0), (4, 4, 12, 12))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


# ---------------------------------------------------------------------------------------------------- uploads
def test_a_brush_mask_is_stored_and_its_sha_returned(client, rt):
    r = client.post("/api/uploads/mask", files={"file": ("mask.png", png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    assert rt.cas.find_asset(r.json()["sha"]) is not None


@pytest.mark.parametrize("data, status", [(b"not an image", 415), (png_bytes("RGB"), 422), (png_bytes("RGBA", alpha=128), 422)])
def test_a_bad_mask_is_refused_with_a_reason(client, data, status):
    r = client.post("/api/uploads/mask", files={"file": ("mask.png", data, "image/png")})
    assert r.status_code == status and r.json()["error"] == "bad_mask", r.text


def test_an_oversized_mask_is_refused(client, monkeypatch):
    from duoskin.api import uploads

    monkeypatch.setattr(uploads, "MAX_MASK_BYTES", 100)
    r = client.post("/api/uploads/mask", files={"file": ("mask.png", png_bytes(size=(64, 64)), "image/png")})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


# ---------------------------------------------------------------------------------------------------- imports and the inbox
def test_an_empty_or_unfinished_or_oversized_import_is_refused(client, monkeypatch):
    from duoskin.api import imports

    r = client.post("/api/imports", files={"file": ("m.glb", b"", "model/gltf-binary")})
    assert r.status_code == 422 and r.json()["error"] == "empty"
    r = client.post("/api/imports", files={"file": ("m.glb.crdownload", b"glTF1234", "application/octet-stream")})
    assert r.status_code == 422 and r.json()["error"] == "bad_file"
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 10)
    r = client.post("/api/imports", files={"file": ("m.glb", b"glTF" + b"0" * 20, "model/gltf-binary")})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_a_dropped_file_lands_in_the_inbox_and_the_wizard_checks_the_plan(client, rt):
    r = client.post("/api/imports", files={"file": ("my model (1).glb", b"glTF" + b"0" * 40, "model/gltf-binary")})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["name"] == "my_model__1_.glb" and entry["state"] == "ready" and not entry["assigned_part"]       # the name is cleaned
    assert any(e["id"] == entry["id"] for e in client.get("/api/inbox").json()["unassigned"])
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    bad = client.post(f"/api/imports/{entry['id']}/assign", json={"project_id": p.id, "part_id": "a.acc.0", "tripo_plan": "lifetime"})
    assert bad.status_code == 422                                                   # only free / paid / not_tripo
    nope = client.post("/api/imports/ib_missing/assign", json={"project_id": p.id, "part_id": "a.acc.0", "tripo_plan": "free"})
    assert nope.status_code == 404 and nope.json()["error"] == "not_found"


# ---------------------------------------------------------------------------------------------------- library
def test_the_library_summary_names_the_reduced_modes(client):
    d = client.get("/api/library").json()
    assert d["labels"]["head_base"] == "2D preview - no head base" and {"flags", "hair", "fabrics", "registries"} <= set(d)


def test_add_kit_validates_and_rebuilds_the_manifest(client, tmp_path):
    dst = tmp_path / "wavy"
    shutil.copytree(kits.DEMO_DIR / "hair" / "hair_bob_03", dst)
    meta = json.loads((dst / "style.json").read_text())
    meta.update({"id": "wavy"})
    meta.pop("origin", None)
    (dst / "style.json").write_text(json.dumps(meta))
    r = client.post("/api/library/kits", json={"folder_path": str(dst), "kind": "hair", "origin": "user_made", "license": "user_made"})
    assert r.status_code == 200 and r.json()["added"] == "wavy", r.text
    assert "wavy" in client.get("/api/library").json()["hair"]
    bad = client.post("/api/library/kits", json={"folder_path": str(dst), "kind": "hair", "origin": "found_online", "license": "n/a"})
    assert bad.status_code == 422 and bad.json()["error"] == "bad_kit" and "origin" in bad.json()["message"]
    again = client.post("/api/library/rebuild-manifest").json()
    assert again["schema_smoke_test"]["ok"] is True


def test_the_head_base_build_needs_blender(client, rt):
    if kits.blender_present(rt):
        pytest.skip("Blender is installed here")
    r = client.post("/api/library/head-base/build", json={"source_path": "x.blend", "variant": "classic"})
    assert r.status_code == 409 and r.json()["error"] == "blender_missing"


# ---------------------------------------------------------------------------------------------------- the Tripo pack and the export route
@pytest.fixture
def board(rt):
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    give_views(rt, p.id, "a.acc.0")
    return p


def test_the_first_tripo_pack_needs_the_free_plan_acknowledgement(client, rt, board):
    url = f"/api/projects/{board.id}/parts/a.acc.0/tripo-pack"
    r = client.post(url)
    assert r.status_code == 409 and r.json()["error"] == "free_plan_warning_required" and "FREE" in r.json()["message"] + json.dumps(r.json())
    ok = client.post(url, json={"acknowledged_free_plan": True})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["files"]) == 8 and ok.json()["pack_id"].startswith("DS-")
    again = client.post(f"/api/projects/{board.id}/parts/a.hair/tripo-pack")        # the acknowledgement is remembered for the project
    assert again.status_code == 409 and again.json()["error"] == "no_views"          # ... so the next complaint is about the views


def test_only_mesh_tiles_have_a_tripo_pack(client, rt, board):
    r = client.post(f"/api/projects/{board.id}/parts/a.shirt/tripo-pack", json={"acknowledged_free_plan": True})
    assert r.status_code == 422 and r.json()["error"] == "not_a_mesh_part"


def test_the_export_waits_for_the_stage_and_the_pick(client, rt, board):
    r = client.post(f"/api/projects/{board.id}/export")
    assert r.status_code == 409 and r.json()["error"] in ("wrong_stage", "conflict")
    assert client.get(f"/api/exports/{board.id}").json()["status"] == "none"
    assert client.patch(f"/api/exports/{board.id}/checklist", json={"item_id": "a.shirt", "step_id": "x", "ticked": True, "expected_version": 0}).status_code == 404
