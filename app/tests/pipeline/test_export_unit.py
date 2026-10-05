"""Export helpers: the secret scan, the slug linter, MOCK file names, glTF URI rewriting, the mock-source rule and the checklist rows."""
from __future__ import annotations

import json
import zipfile

import pytest

from duoskin.pipeline import export


def test_secret_scan_finds_key_shapes_signed_urls_and_stored_keys(rt, tmp_path):
    rt.keys.set_key("openai", "sk-live-0123456789abcdefghijklmnop")
    root = tmp_path / "kit"
    root.mkdir()
    (root / "a.txt").write_text("fine text")
    (root / "b.json").write_text('{"k": "sk-proj-0123456789abcdefABCDEF"}')
    (root / "c.json").write_text('{"u": "https://x.example/f?X-Amz-Signature=abcdef0123456789abcd"}')
    (root / "d.txt").write_text("mail me: someone@example.com")
    (root / "e.txt").write_text("the stored key sk-live-0123456789abcdefghijklmnop leaked")
    (root / "f.png").write_bytes(b"\x89PNG" + b"some@bytes.in" + b"noise")
    hits = export.secret_scan(rt, root=root)
    names = {h.split(":")[0] for h in hits}
    assert names == {"b.json", "c.json", "d.txt", "e.txt"}
    assert not any("a.txt" in h or "f.png" in h for h in hits)
    assert "sk-live" not in " ".join(hits)                       # the finding never repeats the secret


def test_secret_scan_covers_the_zip(rt, tmp_path):
    z = tmp_path / "k.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok.txt", "ok")
        zf.writestr("bad/x.txt", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    assert any("bad/x.txt" in h for h in export.secret_scan(rt, zip_path=z))


@pytest.mark.parametrize("rel,bad", [("A_rin/classic/shirt.png", False), ("A_rin/hair/hair.gltf", False), ("con/x.png", True), ("A_rin/café.png", True),
                                      ("A_rin/trailing.", True), ("A_rin/a<b>.png", True), ("x/" + "y" * 120 + ".png", True)])
def test_slug_linter(tmp_path, rel, bad):
    root = tmp_path / "kit"
    f = root / rel
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    except OSError:
        pytest.skip("this file system cannot hold that name")
    assert bool(export.lint_names(root)) is bad


def test_writer_puts_mock_in_every_file_name(tmp_path):
    w = export.Writer(tmp_path, mock=True)
    rel = w.write("A_rin/classic/shirt.png", b"png")
    assert rel == "A_rin/classic/MOCK_shirt.png" and (tmp_path / rel).read_bytes() == b"png"
    assert w.write("manifest.json", b"{}") == "MOCK_manifest.json"
    assert w.write("MOCK_already.txt", b"x") == "MOCK_already.txt"
    plain = export.Writer(tmp_path / "p", mock=False)
    assert plain.write("A/x.png", b"1") == "A/x.png"
    assert all(len(f["sha256"]) == 64 for f in w.files.values())


def test_gltf_uris_follow_the_kit_file_names():
    doc = {"asset": {"version": "2.0"}, "buffers": [{"uri": "acc.bin", "byteLength": 4}], "images": [{"uri": "acc.png"}, {"uri": "data:image/png;base64,AA=="}]}
    out = json.loads(export.rewrite_gltf(json.dumps(doc).encode(), "MOCK_acc.bin", "MOCK_acc.png"))
    assert out["buffers"][0]["uri"] == "MOCK_acc.bin" and out["images"][0]["uri"] == "MOCK_acc.png"
    assert out["images"][1]["uri"].startswith("data:")


def test_the_mock_flag_is_off_by_default_and_scoped():
    assert export.ALLOW_MOCK_FOR_TESTS is False
    with export.allow_mock():
        assert export.ALLOW_MOCK_FOR_TESTS is True
    assert export.ALLOW_MOCK_FOR_TESTS is False


def test_the_flag_is_not_reachable_from_the_settings(rt):
    with pytest.raises(Exception):
        rt.update_settings({"export": {"allow_mock_for_tests": True}})
    assert not hasattr(rt.effective_settings(), "export")


def test_readme_is_ascii_and_names_the_banners():
    class P:
        name = "Rin and Kai"

    text = export.readme(P, {}, [{"item_id": "a.shirt", "type": "Shirt", "files": [1], "upload_channel": "creator_dashboard"}], ["clone check degraded"], True)
    assert text.isascii() and "DEMO KIT" in text and "clone check degraded" in text


def test_character_folder_is_an_ascii_slug():
    assert export.character_folder({"a": {"role_in_duo": "Leader, with a café!"}}, "a") == "A_leader-with-a-ca"
