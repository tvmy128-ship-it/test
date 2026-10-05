"""Subprocess tree kill (psutil) and the headless Blender bridge, tested with a fake blender executable."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import psutil
import pytest

from duoskin.mesh import blender as bl
from duoskin.mesh import export, fixtures, load
from duoskin.mesh.proc import kill_tree, run_process
from duoskin.mesh.types import MeshError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake blender is a POSIX script")

FAKE = '''#!{python}
import json, os, shutil, sys, time, subprocess
argv = sys.argv[1:]
log = os.environ.get("FAKE_BLENDER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")
if argv == ["--version"]:
    print("Blender 4.2.3\\nbuild date: x"); sys.exit(0)
mode = os.environ.get("FAKE_BLENDER_MODE", "ok")
script = argv[argv.index("--python") + 1]
args_path, result_path = argv[argv.index("--") + 1], argv[argv.index("--") + 2]
args = json.load(open(args_path, encoding="utf-8"))
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.environ["FAKE_BLENDER_PIDFILE"], "w").write(str(child.pid))
    time.sleep(120)
if mode == "exit0_noresult":
    sys.exit(0)
if mode == "fail":
    json.dump({{"ok": False, "error": "RuntimeError: the file has no mesh objects"}}, open(result_path, "w")); sys.exit(3)
name = os.path.basename(script)
if name == "fbx_import.py":
    shutil.copy(args["src"] if args["kind"] == "glb" else os.environ["FAKE_BLENDER_GLB"], args["dst"]); files = {{"glb": args["dst"]}}
elif name == "fbx_export.py":
    shutil.copy(args["src"], args["dst"]); files = {{"fbx": args["dst"]}}
else:
    files = {{}}
json.dump({{"ok": True, "files": files, "blender_version": "4.2.3"}}, open(result_path, "w"))
'''


@pytest.fixture()
def fake_blender(tmp_path, monkeypatch):
    exe = tmp_path / "blender"
    exe.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DUOSKIN_BLENDER", str(exe))
    monkeypatch.setenv("FAKE_BLENDER_LOG", str(tmp_path / "log.jsonl"))
    glb = export.write_glb(fixtures.f_fixture(), tmp_path / "ref.glb")
    monkeypatch.setenv("FAKE_BLENDER_GLB", glb)
    bl._version_cache.clear()
    return exe


def test_run_process_captures_output_and_exit_code():
    ok = run_process([sys.executable, "-c", "print('hi')"], timeout_s=20)
    assert ok.ok and ok.stdout.strip() == "hi" and ok.returncode == 0
    bad = run_process([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"], timeout_s=20)
    assert not bad.ok and bad.returncode == 7 and "boom" in bad.stderr


def test_timeout_kills_the_whole_process_tree(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
            f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(120)")
    t0 = time.monotonic()
    res = run_process([sys.executable, "-c", code], timeout_s=1.5)
    assert res.timed_out and not res.ok and time.monotonic() - t0 < 15
    child = int(pidfile.read_text())
    time.sleep(0.3)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_memory_ceiling_kills_a_runaway_child():
    res = run_process([sys.executable, "-c", "x = bytearray(600 * 1024 * 1024); import time; time.sleep(60)"], timeout_s=30, max_rss_mb=150)
    assert res.killed_for_memory and not res.ok


def test_kill_tree_on_a_missing_pid_is_harmless():
    kill_tree(2 ** 22 + 12345)


def test_find_blender_explicit_env_and_launcher_rewrite(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSKIN_BLENDER", raising=False)
    monkeypatch.delenv("BLENDER_PATH", raising=False)
    monkeypatch.setattr(bl, "_candidates", list)
    assert bl.find_blender("") is None
    exe = tmp_path / "blender.exe"
    exe.write_text("x", encoding="utf-8")
    launcher = tmp_path / "blender-launcher.exe"
    launcher.write_text("x", encoding="utf-8")
    assert bl.find_blender(str(launcher)) == str(exe)                  # blender-launcher.exe returns at once: never used
    assert bl.find_blender(str(tmp_path / "missing")) is None
    monkeypatch.setenv("DUOSKIN_BLENDER", str(exe))
    monkeypatch.setattr(bl, "_candidates", lambda: [str(exe)])
    assert bl.find_blender() == str(exe)


def test_the_command_line_is_exactly_the_documented_one():
    cmd = bl.build_command("/x/blender", "/s/fbx_import.py", "/t/args.json", "/t/result.json")
    assert cmd == ["/x/blender", "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "3",
                   "--python", "/s/fbx_import.py", "--", "/t/args.json", "/t/result.json"]


def test_blender_scripts_exist_and_follow_the_contract():
    for name in ("fbx_import.py", "fbx_export.py", "bake_atlas.py", "polish_pack.py"):
        text = (bl.SCRIPT_DIR / name).read_text(encoding="utf-8")
        compile(text, name, "exec")                                      # valid Python
        assert "sys.argv.index(\"--\")" in text and "sys.exit(3)" in text and "duoskin" not in text.replace("DuoSkin", "")
    fbx = (bl.SCRIPT_DIR / "fbx_export.py").read_text(encoding="utf-8")
    assert "FBX_SCALE_UNITS" in fbx and "path_mode=\"COPY\"" in fbx and "embed_textures=True" in fbx
    imp = (bl.SCRIPT_DIR / "fbx_import.py").read_text(encoding="utf-8")
    assert "ARMATURE" in imp and "TRIANGULATE" in imp and "transform_apply" in imp


def test_version_and_successful_script_run(fake_blender, tmp_path):
    assert bl.blender_version(str(fake_blender)) == "4.2.3"
    res = bl.import_to_glb(str(fake_blender), tmp_path / "in.glb", tmp_path / "out" / "x.glb", kind="glb")
    # the fake copies the "src" (which does not exist for kind glb) -> failure with a readable error
    assert not res.ok
    src = Path(os.environ["FAKE_BLENDER_GLB"])
    res = bl.import_to_glb(str(fake_blender), src, tmp_path / "out" / "x.glb", kind="glb")
    assert res.ok and res.version == "4.2.3" and Path(res.files["glb"]).is_file()
    logged = [json.loads(line) for line in Path(os.environ["FAKE_BLENDER_LOG"]).read_text(encoding="utf-8").splitlines()]
    cmd = [a for a in logged if "--python" in a][-1]
    assert cmd[:6] == ["--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "3", "--python"]


def test_failures_are_reported_through_the_json_file_not_the_exit_code(fake_blender, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_BLENDER_MODE", "fail")
    res = bl.export_fbx(str(fake_blender), os.environ["FAKE_BLENDER_GLB"], tmp_path / "a.fbx")
    assert not res.ok and "no mesh objects" in res.error
    monkeypatch.setenv("FAKE_BLENDER_MODE", "exit0_noresult")          # Blender exits 0 even when the script failed
    res2 = bl.export_fbx(str(fake_blender), os.environ["FAKE_BLENDER_GLB"], tmp_path / "b.fbx")
    assert not res2.ok


def test_a_hanging_blender_is_killed_with_its_children(fake_blender, tmp_path, monkeypatch):
    pidfile = tmp_path / "blender_child.pid"
    monkeypatch.setenv("FAKE_BLENDER_MODE", "hang")
    monkeypatch.setenv("FAKE_BLENDER_PIDFILE", str(pidfile))
    t0 = time.monotonic()
    res = bl.run_script(str(fake_blender), "fbx_export.py", {"src": "a", "dst": "b"}, timeout_s=2)
    assert not res.ok and "timed out" in res.error and time.monotonic() - t0 < 20
    time.sleep(0.3)
    child = int(pidfile.read_text())
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_fbx_goes_through_blender_when_found_and_says_so(fake_blender, tmp_path):
    fbx = tmp_path / "model.fbx"
    fbx.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\0" * 64)
    loaded = load.load_mesh(fbx, workdir=tmp_path / "work")
    assert loaded.facts["converted_by"] == "blender" and loaded.messages[0].startswith("converted from FBX by headless Blender")
    assert loaded.mesh.n_tris == fixtures.f_fixture().n_tris


def test_a_failing_blender_import_is_a_clean_error(fake_blender, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_BLENDER_MODE", "fail")
    fbx = tmp_path / "model.fbx"
    fbx.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\0" * 64)
    with pytest.raises(MeshError) as e:
        load.load_mesh(fbx, workdir=tmp_path / "w")
    assert e.value.code == "blender_failed"


def test_export_all_adds_the_fbx_only_when_blender_exists(fake_blender, tmp_path, monkeypatch):
    m = fixtures.f_fixture()
    files, msgs = export.export_all(m, tmp_path / "with", "acc")
    assert set(files) >= {"gltf", "bin", "png", "glb_archive", "fbx"} and not msgs
    monkeypatch.delenv("DUOSKIN_BLENDER")
    monkeypatch.setattr(bl, "_candidates", list)
    files2, msgs2 = export.export_all(m, tmp_path / "without", "acc")
    assert "fbx" not in files2 and "fbx: not produced" in msgs2[0] and "Blender not installed" in msgs2[0]


def test_fbx_roundtrip_runs_with_blender_present(fake_blender, tmp_path):
    chk = export.fbx_roundtrip(tmp_path / "rt", str(fake_blender))
    assert chk.check_id == "CHK-M19" and chk.passed and chk.status == "passed", chk.evidence


def test_status_explains_when_blender_is_needed(fake_blender, monkeypatch):
    st = bl.status(str(fake_blender))
    assert st["found"] and st["usable"] and st["version"] == "4.2.3" and st["guidance"] is None and st["optional"]
    monkeypatch.delenv("DUOSKIN_BLENDER")
    monkeypatch.setattr(bl, "_candidates", list)
    none = bl.status()
    assert not none["found"] and none["guidance"].startswith("Blender not installed: export GLB instead")
    assert any("FBX" in w for w in none["required_when"]) and "fbx: not produced" in none["without_blender"]
