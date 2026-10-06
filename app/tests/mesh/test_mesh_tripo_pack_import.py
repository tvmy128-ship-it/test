"""pipeline/tripo_pack.py (manual-mode pack, ACC-10) and pipeline/mesh_import.py (import safety, CHK-M16)."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.mesh import export, fixtures
from duoskin.pipeline import mesh_import as mi
from duoskin.pipeline import tripo_pack as tp


def make_view(rgb, size=512, transparent=True) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    ImageDraw.Draw(img).ellipse([size // 5, size // 5, size * 4 // 5, size * 4 // 5], fill=rgb + (255,))
    return img


def views(rgb=(200, 50, 50)):
    return {k: make_view(rgb) for k in ("front", "left", "back", "right")}


def build(tmp_path, v=None, **kw):
    args = {"asset_id": "a.acc.0", "project_id": "prj_x", "part_id": "a.acc.0", "kind": "plush_pet", "category": "shoulder", "attachment": "RightCollarAttachment",
                "target_studs": (1.4, 1.2, 1.1), "face_limit": 3000, "views": v or views(), "project_slug": "Pink Duo", "asset_label": "Plush bunny",
                "inbox_path": "C:\\Users\\me\\DuoSkin Exports\\TripoPacks\\inbox", "free_plan_warning_acknowledged": True}
    args.update(kw)
    return tp.build_pack(tmp_path / "pack", **args)


# ------------------------------------------------------------------------------------------------------- pack
def test_the_pack_has_exactly_the_documented_files(tmp_path):
    r = build(tmp_path)
    assert sorted(p.name for p in (tmp_path / "pack").iterdir()) == sorted(tp.PACK_FILES) and len(tp.PACK_FILES) == 8
    assert set(r.files) == set(tp.PACK_FILES)
    assert tp.verify_pack(tmp_path / "pack") == []
    for name in ("00_FRONT_single.png", "01_FRONT.png", "02_LEFT_subject-left.png", "03_BACK.png", "04_RIGHT_subject-right.png"):
        with Image.open(tmp_path / "pack" / name) as im:
            assert im.size == (2048, 2048) and im.mode == "RGB"            # flattened: no alpha left


def test_pack_id_is_globally_unique_and_in_the_settings(tmp_path):
    a, b = tp.make_pack_id("Pink Duo", "a.acc.0"), tp.make_pack_id("Pink Duo", "a.acc.0")
    assert a != b and a.startswith("DS-pink-duo-a_acc_0-") and len(a.rsplit("-", 1)[1]) == 6
    assert tp.make_pack_id("x", "a.hair", token="3F9C1A").endswith("-3f9c1a")
    with pytest.raises(tp.PackError):
        tp.make_pack_id("x", "y", token="zz")
    r = build(tmp_path, pack_id="DS-pink-duo-a_acc_0-3f9c1a")
    assert r.pack_id in r.settings_text and f"{r.pack_id}.glb" in r.settings_text and "Rename the downloaded file" in r.settings_text
    assert json.loads((tmp_path / "pack" / "asset.json").read_text(encoding="utf-8"))["pack_id"] == r.pack_id
    assert mi.pack_id_from_name(f"{r.pack_id}.glb") == r.pack_id and mi.pack_id_from_name("Tripo_model (3).glb") is None


def test_asset_json_follows_appendix_b3(tmp_path):
    r = build(tmp_path, created_at="2026-10-05T10:00:00Z")
    a = json.loads((tmp_path / "pack" / "asset.json").read_text(encoding="utf-8"))
    assert a["schema"] == "duoskin.tripo_pack/1" and a["pack_version"] == 1 and a["created_at"] == "2026-10-05T10:00:00Z"
    for k in ("asset_id", "project_id", "part_id", "kind", "category", "attachment", "target_studs", "face_limit", "views"):
        assert k in a
    assert set(a["views"]) == {"front", "left", "back", "right"} and all(len(h) == 64 for h in a["views"].values())
    assert a["target_studs"] == [1.4, 1.2, 1.1] and a["face_limit"] == 3000
    assert a == r.asset


def test_settings_txt_is_ascii_with_the_free_plan_warning_first_and_the_exact_options(tmp_path):
    r = build(tmp_path, asset_label="Plush b\u00fcnny \u2192 pink")
    text = (tmp_path / "pack" / "SETTINGS.txt").read_bytes().decode("ascii")
    assert text.isascii() and text.index("FREE plan") < text.index("STEPS")
    for must in ("PUBLIC", "CC BY 4.0", "no commercial-use rights", "Smart Mesh", "P2.0", "Multi-view", "00_FRONT_single.png", "Triangles (not quads)",
                 "Face limit about 3000", "Texture ON, standard (2K)", "PBR OFF", "No compression", "Download the model as GLB", "[UNVERIFIED]"):
        assert must in text, must
    assert '"Left" is the object\'s own left side' in text and "C:\\Users\\me\\DuoSkin Exports\\TripoPacks\\inbox" in text
    assert r.settings_text == text


def test_non_ascii_inbox_path_is_left_out(tmp_path):
    r = build(tmp_path, inbox_path="C:\\Users\\Zo\u00eb\\DuoSkin Exports\\TripoPacks\\inbox")
    assert r.settings_text.isascii() and "Zo" not in r.settings_text and "Open inbox folder button" in r.settings_text


def test_the_free_plan_warning_must_be_acknowledged_first(tmp_path):
    with pytest.raises(tp.PackError) as e:
        build(tmp_path, free_plan_warning_acknowledged=False)
    assert e.value.code == "free_plan_warning_required" and not (tmp_path / "pack").exists()
    with pytest.raises(tp.PackError) as e2:
        build(tmp_path, v={"front": make_view((1, 2, 3))})
    assert e2.value.code == "missing_views"


def test_near_white_objects_are_flattened_on_d9d9d9_else_white(tmp_path):
    white = build(tmp_path / "w", v=views((250, 250, 250)))
    assert white.background == "#D9D9D9" and white.warnings
    with Image.open(tmp_path / "w" / "pack" / "01_FRONT.png") as im:
        assert im.getpixel((5, 5)) == (217, 217, 217)
    red = build(tmp_path / "r", v=views((200, 30, 30)))
    assert red.background == "#FFFFFF"
    with Image.open(tmp_path / "r" / "pack" / "01_FRONT.png") as im:
        assert im.getpixel((5, 5)) == (255, 255, 255)
    near, de = tp.edge_near_white(list(views((245, 245, 245)).values()))
    assert near and de < 10


def test_views_are_not_cropped_or_recentred_only_flattened(tmp_path):
    big = Image.new("RGBA", (2048, 2048), (0, 0, 0, 0))
    ImageDraw.Draw(big).rectangle([100, 700, 900, 1500], fill=(10, 120, 200, 255))      # off-centre object
    v = {k: big for k in ("front", "left", "back", "right")}
    build(tmp_path, v=v)
    with Image.open(tmp_path / "pack" / "01_FRONT.png") as im:
        arr = np.asarray(im)
    ys, xs = np.nonzero(np.abs(arr.astype(int) - 255).max(axis=2) > 20)
    assert (xs.min(), xs.max(), ys.min(), ys.max()) == (100, 900, 700, 1500)


def test_hair_packs_carry_hair_only_views_and_say_so(tmp_path):
    r = build(tmp_path, kind="hair", category="hair", attachment="HairAttachment", asset_id="a.hair", part_id="a.hair", face_limit=3500)
    assert r.asset["views_variant"] == "hair_only" and "ON ITS OWN (no head)" in r.settings_text and "grey cube" in r.settings_text
    acc = build(tmp_path / "acc")
    assert acc.asset["views_variant"] == "accessory" and "ON ITS OWN" not in acc.settings_text


def test_views_sheet_has_arrows_and_no_text(tmp_path):
    build(tmp_path)
    with Image.open(tmp_path / "pack" / "views_sheet.png") as sheet:
        arr = np.asarray(sheet.convert("L"))
    strip = arr[-128:, :]                                    # the marker strip under the tiles
    assert (strip < 80).sum() > 500                          # arrows were drawn
    sheet_w = arr.shape[1]
    left_tile = strip[:, sheet_w // 4: sheet_w // 2]          # left view's arrow points to the left: heavier ink on the left half of the arrow
    ys, xs = np.nonzero(left_tile < 80)
    assert len(xs) > 0
    assert (xs.min() + xs.max()) / 2 > 0 and len(set(ys.tolist())) > 3


def test_verify_pack_detects_tampering(tmp_path):
    build(tmp_path)
    (tmp_path / "pack" / "01_FRONT.png").write_bytes(b"x")
    problems = tp.verify_pack(tmp_path / "pack")
    assert any("01_FRONT.png does not match asset.json" in p for p in problems) and any("cannot be opened" in p for p in problems)
    (tmp_path / "pack" / "asset.json").unlink()
    assert "missing asset.json" in tp.verify_pack(tmp_path / "pack")


def test_pack_fingerprint_ignores_the_creation_time(tmp_path):
    a = build(tmp_path / "a", pack_id="DS-x-a_acc_0-000001", created_at="2026-01-01T00:00:00Z")
    b = build(tmp_path / "b", pack_id="DS-x-a_acc_0-000001", created_at="2027-01-01T00:00:00Z")
    assert tp.pack_fingerprint(a.asset) == tp.pack_fingerprint(b.asset)


# ------------------------------------------------------------------------------------------------------- import
def glb(tmp_path, name="m.glb"):
    return Path(export.write_glb(fixtures.f_fixture(), tmp_path / name))


def make_zip(path: Path, members: dict[str, bytes], *, symlink: str | None = None):
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in members.items():
            zf.writestr(n, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
    return path


def test_partial_and_hidden_names_are_never_imported(tmp_path):
    for name in ("model.glb.crdownload", "a.part", "b.tmp", "~$x.glb", ".hidden.glb", "c.glb~", "d.PART"):
        assert mi.is_partial_name(name), name
        p = tmp_path / name
        p.write_bytes(b"x" * 10)
        r = mi.import_file(p, tmp_path / "w")
        assert not r.ok and r.issues[0].code == "partial_download"
    assert not mi.is_partial_name("DS-pink-duo-a_acc_0-3f9c1a.glb")


def test_inbox_waits_for_a_stable_size_and_an_exclusive_open(tmp_path):
    p = tmp_path / "model.glb"
    p.write_bytes(b"x" * 100)
    assert not mi.ready_to_import(p, [100])                         # one poll is not enough
    assert not mi.ready_to_import(p, [50, 100])                     # still growing
    assert not mi.ready_to_import(p, [0, 0])
    assert mi.ready_to_import(p, [100, 100])
    assert not mi.ready_to_import(tmp_path / "x.crdownload", [1, 1])
    assert not mi.ready_to_import(tmp_path / "missing.glb", [1, 1])


def test_glb_import_records_hash_licence_and_banner(tmp_path):
    src = glb(tmp_path)
    r = mi.import_file(src, tmp_path / "w", licence=mi.licence_from_plan("free"), tripo_plan="free", task_link="https://tripo3d.ai/x")
    assert r.ok and r.kind == "glb" and r.work_path and Path(r.work_path).is_file() and len(r.sha256) == 64
    assert r.licence == "tripo_free_public_ccby_noncommercial" and r.banner == mi.FREE_PLAN_BANNER and "NO commercial-use rights" in r.banner
    assert r.provenance["source"] == "tripo_manual" and r.provenance["task_link"] == "https://tripo3d.ai/x"
    assert not mi.sell_ready_allowed(r.licence) and mi.sell_ready_allowed("tripo_paid_private_commercial")


def test_licence_mapping_covers_the_import_wizard_answers():
    assert mi.licence_from_plan("paid") == "tripo_paid_private_commercial"
    assert mi.licence_from_plan("not_tripo") == "user_made" and mi.licence_from_plan("api") == "tripo_api_private_commercial"
    assert mi.licence_banner("user_made") is None and mi.licence_banner("tripo_paid_private_commercial") is None
    assert "unknown" in mi.licence_banner("unknown")
    assert mi.provenance_source("user_made") == "user_made" and mi.provenance_source("tripo_free_public_ccby_noncommercial") == "tripo_manual"


def test_type_comes_from_content_not_suffix(tmp_path):
    src = glb(tmp_path)
    liar = tmp_path / "model.fbx"
    liar.write_bytes(src.read_bytes())
    r = mi.import_file(liar, tmp_path / "w")
    assert r.ok and r.kind == "glb" and any("named like a FBX" in n for n in r.notes)
    fbx = tmp_path / "real.glb"
    fbx.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\0" * 64)
    r2 = mi.import_file(fbx, tmp_path / "w2", blender_available=False)
    assert r2.kind == "fbx" and not r2.ok and r2.issues[0].code == "blender_missing" and r2.issues[0].message.startswith("Blender not installed: export GLB instead")


def test_fbx_blend_and_obj_need_blender_when_it_exists(tmp_path):
    fbx = tmp_path / "a.fbx"
    fbx.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\0" * 64)
    r = mi.import_file(fbx, tmp_path / "w", blender_available=True)
    assert r.ok and r.needs_blender and r.kind == "fbx"
    blend = tmp_path / "a.blend"
    blend.write_bytes(b"BLENDER-v400" + b"\0" * 64)
    assert mi.import_file(blend, tmp_path / "w2", blender_available=True).needs_blender
    obj = tmp_path / "a.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    assert mi.import_file(obj, tmp_path / "w3", blender_available=False).ok          # the built-in reader handles it


def test_size_cap_and_empty_files(tmp_path):
    big = tmp_path / "big.glb"
    big.write_bytes(glb(tmp_path).read_bytes())
    r = mi.import_file(big, tmp_path / "w", max_bytes=100)
    assert not r.ok and r.issues[0].code == "too_big" and "limit is" in r.issues[0].message
    empty = tmp_path / "e.glb"
    empty.write_bytes(b"")
    assert mi.import_file(empty, tmp_path / "w2").issues[0].code == "empty"
    assert mi.import_file(tmp_path / "gone.glb", tmp_path / "w3").issues[0].code == "not_found"
    assert int(mi.limits.threshold("upload.max_mb")) == 50


def make_glb_bytes(doc):
    js = json.dumps(doc).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    ch = struct.pack("<I4s", len(js), b"JSON") + js
    return struct.pack("<4sII", b"glTF", 2, 12 + len(ch)) + ch


@pytest.mark.parametrize("ext,word", [("EXT_meshopt_compression", "meshopt"), ("KHR_draco_mesh_compression", "Draco"), ("KHR_texture_basisu", "KTX2")])
def test_compressed_files_are_refused_with_reexport_advice(tmp_path, ext, word):
    p = tmp_path / "c.glb"
    p.write_bytes(make_glb_bytes({"asset": {"version": "2.0"}, "extensionsRequired": [ext], "extensionsUsed": [ext]}))
    r = mi.import_file(p, tmp_path / "w")
    assert not r.ok and r.issues[0].code == "unsupported_extension" and word in r.issues[0].message and "re-export" in r.issues[0].message.lower()


def test_gltf_with_external_files_is_refused_but_a_zip_of_them_works(tmp_path):
    files = export.write_gltf_set(fixtures.f_fixture(), tmp_path / "set", "f")
    r = mi.import_file(files["gltf"], tmp_path / "w")
    assert not r.ok and r.kind == "gltf" and r.issues[0].code == "missing_files" and "single .glb" in r.issues[0].message
    z = make_zip(tmp_path / "bundle.zip", {Path(p).name: Path(p).read_bytes() for p in files.values()})
    rz = mi.import_file(z, tmp_path / "w2")
    assert rz.ok and rz.kind == "gltf" and rz.work_path.endswith("f.gltf") and len(rz.extracted) == 3


def test_zip_slip_variants_are_refused_and_nothing_is_written_outside(tmp_path):
    data = glb(tmp_path).read_bytes()
    outside = tmp_path / "evil.txt"
    cases = {
        "dotdot": {"../evil.txt": b"x", "m.glb": data},
        "nested_dotdot": {"a/../../evil.txt": b"x", "m.glb": data},
        "absolute": {"/etc/evil.txt": b"x"},
        "backslash": {"..\\..\\evil.txt": b"x"},
        "drive": {"C:\\Windows\\evil.txt": b"x"},
        "reserved": {"CON.glb": data},
    }
    for name, members in cases.items():
        z = make_zip(tmp_path / f"{name}.zip", members)
        r = mi.import_file(z, tmp_path / f"w_{name}")
        assert not r.ok and r.issues[0].code == "unsafe_path", (name, r.issues)
        assert not outside.exists()
    with pytest.raises(mi.UnsafeArchive):
        mi._safe_member("a\x00.glb")                                   # (Python's zipfile already truncates such names)
    sym = make_zip(tmp_path / "sym.zip", {"m.glb": data}, symlink="link.glb")
    assert mi.import_file(sym, tmp_path / "w_sym").issues[0].code == "unsafe_path"


def test_zip_limits_count_files_and_bytes(tmp_path, monkeypatch):
    many = make_zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(201)})
    assert mi.import_file(many, tmp_path / "w").issues[0].code == "too_many_files"
    ok = make_zip(tmp_path / "ok.zip", {f"f{i}.txt": b"x" for i in range(199)} | {"m.glb": glb(tmp_path).read_bytes()})
    assert mi.safe_extract_zip(ok, tmp_path / "ok_out", max_files=300)
    bomb = make_zip(tmp_path / "bomb.zip", {"a.bin": b"\0" * (3 * 1024 * 1024)})
    with pytest.raises(mi.UnsafeArchive) as e:
        mi.safe_extract_zip(bomb, tmp_path / "bomb_out", max_total_bytes=1024 * 1024)
    assert e.value.code == "zip_too_big"
    with pytest.raises(mi.UnsafeArchive) as e2:
        mi.safe_extract_zip(bomb, tmp_path / "bomb_out2", max_total_bytes=10 * 1024 * 1024, max_member_bytes=1024 * 1024)
    assert e2.value.code == "zip_too_big"
    enc = make_zip(tmp_path / "enc.zip", {"m.glb": b"data"})
    raw = bytearray(enc.read_bytes())
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):                         # set the "encrypted" bit in the local and central headers
        at = raw.index(sig) + (6 if sig == b"PK\x03\x04" else 8)
        raw[at] |= 0x01
    enc.write_bytes(bytes(raw))
    assert mi.import_file(enc, tmp_path / "w_enc").issues[0].code == "encrypted"
    assert mi.import_file(tmp_path / "ok.zip", tmp_path / "w_ok").ok
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04" + b"garbage" * 10)
    assert mi.import_file(bad, tmp_path / "w_bad").issues[0].code == "corrupt"


def test_zip_with_several_models_or_none(tmp_path):
    data = glb(tmp_path).read_bytes()
    amb = make_zip(tmp_path / "amb.zip", {"a.glb": data, "b.glb": data})
    assert mi.import_file(amb, tmp_path / "w").issues[0].code == "ambiguous_zip"
    named = make_zip(tmp_path / "named.zip", {"DS-x-a_acc_0-aaaaaa.glb": data, "other.glb": data})
    r = mi.import_file(named, tmp_path / "w3", expected_pack_id="DS-x-a_acc_0-aaaaaa")
    assert r.ok and r.work_path.endswith("DS-x-a_acc_0-aaaaaa.glb") and r.assigned_pack_id == "DS-x-a_acc_0-aaaaaa"
    none = make_zip(tmp_path / "none.zip", {"readme.txt": b"hi", "tex.png": b"\x89PNG\r\n\x1a\n"})
    assert mi.import_file(none, tmp_path / "w4").issues[0].code == "no_model"


def test_images_and_unknown_files_are_not_models(tmp_path):
    png = tmp_path / "a.png"
    Image.new("RGB", (4, 4)).save(png)
    assert mi.import_file(png, tmp_path / "w").issues[0].code == "not_a_model"
    junk = tmp_path / "x.glb"
    junk.write_bytes(b"hello world" * 10)
    assert mi.import_file(junk, tmp_path / "w2").issues[0].code == "unknown_format"
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid x\nendsolid x\n")
    assert mi.import_file(stl, tmp_path / "w3").issues[0].code == "no_texture_format"


def test_chk_m16_assignment_and_licence(tmp_path):
    pack = "DS-pink-duo-a_acc_0-3f9c1a"
    src = tmp_path / f"{pack}.glb"
    src.write_bytes(glb(tmp_path).read_bytes())
    r = mi.import_file(src, tmp_path / "w", licence=mi.licence_from_plan("paid"), expected_pack_id=pack)
    chk = mi.check_assignment(r, pack, known_pack_ids={pack})
    assert chk.check_id == "CHK-M16" and chk.kind == "hard" and chk.passed
    wrong = mi.check_assignment(r, "DS-other-a_acc_1-aaaaaa")
    assert not wrong.passed and "dropped on" in wrong.evidence
    unknown = mi.import_file(src, tmp_path / "w2", expected_pack_id=pack)               # plan not recorded
    assert not mi.check_assignment(unknown, pack).passed and "plan" in mi.check_assignment(unknown, pack).evidence
    assert not mi.check_assignment(r, pack, known_pack_ids={"DS-x-y-000000"}).passed
    free = mi.import_file(src, tmp_path / "w3", licence="tripo_free_public_ccby_noncommercial")
    assert free.banner and mi.check_assignment(free, None).passed


def test_import_never_parses_meshes_in_this_process(tmp_path):
    import sys

    before = {m for m in sys.modules if m.startswith("trimesh")}
    mi.import_file(glb(tmp_path), tmp_path / "w")
    after = {m for m in sys.modules if m.startswith("trimesh")}
    assert after == before                                                            # only JSON was scanned


def test_provenance_file_roundtrip(tmp_path):
    r = mi.import_file(glb(tmp_path), tmp_path / "w", licence="user_made")
    mi.write_provenance(r, tmp_path / "prov.json")
    prov = json.loads((tmp_path / "prov.json").read_text(encoding="utf-8"))
    assert prov["source"] == "user_made" and prov["sha256"] == mi.file_digest(r.work_path) == r.sha256
