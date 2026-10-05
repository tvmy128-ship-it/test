"""duoskin doctor (CHK-S01..S15: the CHK-S11 split, S09 warn-only, S14 DreamSim, S15 availability; APP_SPEC §15.3, §2 S25)."""
from __future__ import annotations

import hashlib
import json
import ssl
import stat
import sys

import pytest

from duoskin import config, winplat
from duoskin.api import doctor_checks as dc
from duoskin.engine.runtime import Runtime
from duoskin.models.settings import Settings


@pytest.fixture
def ctx(tmp_path):
    paths = config.paths(tmp_path / "home")
    return dc.DoctorCtx(paths=paths, settings=Settings(), rt=None, quick=True)


def run(ctx, check_id):
    fn = next(d.fn for d in dc._CHECKS if d.id == check_id)
    return fn(ctx)


def report_for(rt, **kw):
    return dc.run_doctor(rt, quick=True, **kw)


def item(report, check_id):
    return next(i for i in report["checks"] if i["id"] == check_id)


# ------------------------------------------------------------------------------------------------- whole report
def test_every_check_is_present_in_order(rt):
    ids = [i["id"] for i in report_for(rt)["checks"]]
    assert ids == [f"CHK-S{n:02d}" for n in range(1, 16)]                                    # S11b and S11c became the single S15


def test_a_fresh_install_exits_zero_and_only_warns(rt):
    """APP_SPEC S25 / §17.7 criterion 1: a missing house style, head base, body base, hair kit, dreamsim.onnx or Blender only warn."""
    report = report_for(rt)
    assert report["blocks_paid_features"] is False and report["broken_install"] is False
    assert report["summary"]["failed"] == 0 and report["exit_code"] == 0                    # warnings alone exit 0
    for cid in ("CHK-S14", "CHK-S15"):
        i = item(report, cid)
        assert i["status"] == "warn" and i["blocking"] is False and i["kind"] == "soft"
    assert item(report, "CHK-S09")["kind"] == "soft" and item(report, "CHK-S09")["status"] in ("na", "pass")
    f = report["flags"]
    assert f["head_base_present"] is False and f["house_style_present"] is False and f["body_base_present"] is False
    assert f["hair_kit_empty"] is True and f["dreamsim_present"] is False and f["makeup"] == "unavailable"
    assert f["degraded_clone_check"] is True and f["clone_check_mode"] == "degraded" and "blender_present" in f
    assert rt.paid_blocked_reason() is None


def test_checks_are_checkresults_and_na_is_not_a_failure(rt):
    report = report_for(rt)
    for i in report["checks"]:
        r = i["check"]
        assert r["check_id"] == i["id"] and r["ran"] is True and r["kind"] == i["kind"]
        if i["status"] == "na":
            assert r["status"] == "not_applicable" and r["passed"] is True and r["na_reason"]
        if i["status"] == "pass":
            assert r["passed"] is True
        if i["kind"] == "soft" and i["status"] == "warn":
            assert r["passed"] is False                                                     # a soft warning is "not passed" but never blocks


def test_exit_codes(rt, monkeypatch):
    def make(status, kind="hard", fatal=False):
        return dc.CheckDef("CHK-T", "t", kind, lambda c: dc.Outcome(status, "msg"), [], fatal)

    # 0 = ok (warnings allowed; SOFT and not_applicable never block), 2 = a HARD or ASSERT check failed or did not run, 3 = broken install
    cases = [([make("pass")], 0), ([make("na")], 0), ([make("warn", "soft")], 0), ([make("warn")], 0), ([make("fail", "soft")], 0),
             ([make("fail")], 2), ([make("fail", "assert")], 2), ([make("fail", fatal=True)], 3),
             ([make("pass"), make("warn", "soft")], 0), ([make("warn", "soft"), make("fail")], 2)]
    for checks, expected in cases:
        monkeypatch.setattr(dc, "_CHECKS", checks)
        assert dc.run_doctor(rt, quick=True)["exit_code"] == expected, [(c.kind, c.fn(None).status) for c in checks]
    monkeypatch.setattr(dc, "_CHECKS", [make("fail", "soft")])                              # a SOFT failure can only warn
    r = dc.run_doctor(rt, quick=True)
    assert r["checks"][0]["status"] == "warn" and r["blocks_paid_features"] is False
    monkeypatch.setattr(dc, "_CHECKS", [make("fail", "assert")])
    assert dc.run_doctor(rt, quick=True)["blocks_paid_features"] is True                     # HARD and ASSERT failures block


def test_a_crashing_check_fails_closed(rt, monkeypatch):
    def boom(c):
        raise RuntimeError("check bug with key sk-ant-api03-" + "Q" * 30)

    monkeypatch.setattr(dc, "_CHECKS", [dc.CheckDef("CHK-X", "x", "hard", boom, [], False)])
    r = dc.run_doctor(rt, quick=True)
    i = r["checks"][0]
    assert i["status"] == "fail" and i["blocking"] is True and i["check"]["ran"] is False and i["check"]["passed"] is False
    assert "sk-ant-api03" not in i["message"] and r["blocks_paid_features"]


def test_only_filter_and_report_formatting(rt):
    r = dc.run_doctor(rt, quick=True, only={"CHK-S02", "CHK-S14"})
    assert [i["id"] for i in r["checks"]] == ["CHK-S02", "CHK-S14"]
    text = dc.format_report(report_for(rt))
    assert "[ OK ] CHK-S02" in text and "[WARN] CHK-S15" in text and "What to do:" in text and "Nothing blocks you" in text
    assert "FAIL" not in text


def test_format_report_for_blocking_and_broken(rt, monkeypatch):
    monkeypatch.setattr(dc, "_CHECKS", [dc.CheckDef("CHK-A", "a", "hard", lambda c: dc.Outcome("fail", "bad", "fix it"), [], False)])
    assert "Paid features are blocked" in dc.format_report(dc.run_doctor(rt, quick=True))
    monkeypatch.setattr(dc, "_CHECKS", [dc.CheckDef("CHK-A", "a", "hard", lambda c: dc.Outcome("fail", "bad", "fix it"), [], True)])
    assert "install is broken" in dc.format_report(dc.run_doctor(rt, quick=True))


# ------------------------------------------------------------------------------------------------- individual checks
def test_s01_s02_pass(ctx):
    assert run(ctx, "CHK-S01").status == "pass" and run(ctx, "CHK-S02").status == "pass"


def test_s03_python_rules(ctx, monkeypatch):
    assert run(ctx, "CHK-S03").status == "pass"
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    out = run(ctx, "CHK-S03")
    assert out.status == "fail" and ".venv" in out.message and "setup.bat" in out.fix
    monkeypatch.undo()
    monkeypatch.setattr(sys, "base_prefix", r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.14")
    assert "Store stub" in run(ctx, "CHK-S03").message
    monkeypatch.undo()
    monkeypatch.setattr(dc.sysconfig, "get_config_var", lambda name: 1 if name == "Py_GIL_DISABLED" else None)
    assert "free-threaded" in run(ctx, "CHK-S03").message


def test_s03_windows_rules(ctx, monkeypatch):
    monkeypatch.setattr(winplat, "IS_WINDOWS", True)
    monkeypatch.setattr(dc.sysconfig, "get_platform", lambda: "win32")
    out = run(ctx, "CHK-S03")
    assert out.status == "fail" and "x64" in out.message
    monkeypatch.setattr(dc.sysconfig, "get_platform", lambda: "win-amd64")
    for version in ((3, 11, 9, "final", 0), (3, 15, 0, "alpha", 1), (3, 10, 0, "final", 0)):
        monkeypatch.setattr(sys, "version_info", version)
        assert "not supported" in run(ctx, "CHK-S03").message, version
    for version in ((3, 12, 4, "final", 0), (3, 13, 1, "final", 0), (3, 14, 0, "final", 0)):       # one lock has wheels for all three
        monkeypatch.setattr(sys, "version_info", version)
        assert "not supported" not in run(ctx, "CHK-S03").message, version


def test_s04_is_skipped_in_quick_mode(ctx):
    assert run(ctx, "CHK-S04").status == "na"


def test_s05_needs_the_hook_only_when_started_through_the_module(ctx, monkeypatch):
    class Fake(ssl.SSLContext):
        pass

    Fake.__module__ = "truststore._api"
    monkeypatch.setattr(ssl, "SSLContext", ssl.SSLContext)
    assert run(ctx, "CHK-S05").status in ("na", "pass")
    monkeypatch.setattr(dc.DoctorCtx, "started_via_module", property(lambda self: True))
    monkeypatch.setattr(ssl, "SSLContext", ssl.SSLContext)
    if not ssl.SSLContext.__module__.startswith("truststore"):
        assert run(ctx, "CHK-S05").status == "fail"
    monkeypatch.setattr(ssl, "SSLContext", Fake)
    assert run(ctx, "CHK-S05").status == "pass"


def test_s06_security_self_test_passes_and_detects_a_wrong_bind(rt):
    c = dc.DoctorCtx(paths=rt.paths, settings=rt.effective_settings(), rt=rt, quick=True)
    assert run(c, "CHK-S06").status == "pass"
    rt.port, rt.bound_host = 8765, "0.0.0.0"
    out = run(c, "CHK-S06")
    assert out.status == "fail" and "0.0.0.0" in out.message


def test_s07_page_files(ctx):
    out = run(ctx, "CHK-S07")
    assert out.status in ("pass", "na")
    if (config.APP_ROOT / "duoskin" / "web" / "index.html").exists():
        assert out.status == "pass" and "no-store" in out.message


def test_s08_data_folder(ctx, monkeypatch):
    assert run(ctx, "CHK-S08").status in ("pass", "warn")
    monkeypatch.setattr(winplat, "is_onedrive_path", lambda p: True)
    out = run(ctx, "CHK-S08")
    assert out.status == "warn" and "OneDrive" in out.message and "DUOSKIN_HOME" in out.fix
    monkeypatch.undo()
    monkeypatch.setattr(winplat, "replace_with_retry", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    out = run(ctx, "CHK-S08")
    assert out.status == "fail" and "Cannot write" in out.message
    d = next(d for d in dc._CHECKS if d.id == "CHK-S08")
    assert d.fatal and d.kind == "hard"


def test_s09_blender_is_optional_and_only_ever_warns(ctx, tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSKIN_BLENDER", raising=False)
    monkeypatch.setattr(dc.shutil, "which", lambda name: None)
    monkeypatch.setattr(dc, "detect_blender", lambda settings: None)
    out = run(ctx, "CHK-S09")
    assert out.status == "na" and "optional" in out.message and "no-Blender mode" in out.message and out.detail["blender_present"] is False
    fake = tmp_path / "blender"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Blender 4.2.3'; exit 0; fi\nexit 3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(dc, "detect_blender", lambda settings: fake)
    full = dc.DoctorCtx(paths=ctx.paths, settings=ctx.settings, rt=None, quick=False)
    ok = run(full, "CHK-S09")
    assert ok.status == "pass" and ok.detail["blender_present"] is True
    # a configured Blender that fails is switched off with a warning (never a block): the no-Blender mode runs
    fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Blender 3.6.0'; exit 0; fi\nexit 3\n", encoding="utf-8")
    out = run(full, "CHK-S09")
    assert out.status == "warn" and "3.6" in out.message and "no-Blender mode" in out.message and out.detail["blender_present"] is False
    fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Blender 4.2.3'; exit 0; fi\nexit 0\n", encoding="utf-8")
    out = run(full, "CHK-S09")
    assert "expected 3" in out.message and out.status == "warn"                              # launcher-style: exit 0 when the script fails
    assert next(d for d in dc._CHECKS if d.id == "CHK-S09").kind == "soft"


def test_a_failing_blender_never_blocks_paid_features(rt, monkeypatch):
    monkeypatch.setattr(dc, "_CHECKS", [dc.CheckDef("CHK-S09", "b", "soft", lambda c: dc.Outcome("warn", "broken", "", {"blender_present": False}), [], False)])
    r = dc.run_doctor(rt, quick=True)
    assert r["exit_code"] == 0 and r["blocks_paid_features"] is False


def test_s10_needs_a_key_and_real_mode(ctx):
    assert run(ctx, "CHK-S10").status == "na"                                                # no key
    ctx.settings = Settings(demo_mode=True)
    assert "mock" in run(ctx, "CHK-S10").message


def test_s11_hard_part_and_its_missing_pieces(ctx):
    assert run(ctx, "CHK-S11").status in ("na", "pass")
    manifest = ctx.paths.kits_dir / "manifest.json"
    manifest.write_text("{ broken", encoding="utf-8")
    out = run(ctx, "CHK-S11")
    assert out.status == "fail" and "build-kit-manifest" in out.fix
    manifest.write_text('{"flags": {"head_base_present": false}}', encoding="utf-8")
    assert run(ctx, "CHK-S11").status == "pass"


def test_s11_banned_term_in_colour_names(ctx, monkeypatch, tmp_path):
    data = tmp_path / "app" / "duoskin" / "data"
    data.mkdir(parents=True)
    (data / "colour_names.json").write_text('{"colours": ["Sky Blue", "Dragon Ball Orange"]}', encoding="utf-8")
    (data / "banned_terms.json").write_text('{"terms": ["dragon ball", "pikachu"]}', encoding="utf-8")
    monkeypatch.setattr(config, "APP_ROOT", tmp_path / "app")
    out = run(ctx, "CHK-S11")
    assert out.status == "fail" and out.detail["count"] == 1
    (data / "colour_names.json").write_text('{"colours": ["Sky Blue", "Dragonfruit"]}', encoding="utf-8")
    assert run(ctx, "CHK-S11").status == "pass"
    (ctx.paths.user_data_dir / "banned_terms.extra.json").write_text('["dragonfruit"]', encoding="utf-8")   # the user's own additions count
    assert run(ctx, "CHK-S11").status == "fail"


def test_s11_enum_check_builds_the_kit_enums_without_a_head_base(ctx):
    out = run(ctx, "CHK-S11")
    assert out.status == "pass" and "enums build" in out.message                              # from builtin_kits/face_canvas_default.json


def test_s15_reports_each_missing_kit_with_its_route_and_never_blocks(ctx):
    d = next(d for d in dc._CHECKS if d.id == "CHK-S15")
    assert d.kind == "soft"
    out = run(ctx, "CHK-S15")
    assert out.status == "warn"
    for words in ("no head base: 2D face previews, no Head item", "no body base", "body_colors.json", "no house style sheet", "run S0",
                  "every hair is hair_custom", "no DreamSim", "no-Blender mode"):
        assert words in out.message, words
    f = out.detail["flags"]
    assert f["makeup"] == "unavailable" and f["head_base_present"] is False and f["hair_kit_empty"] is True
    (ctx.paths.kits_dir / "style").mkdir(parents=True)
    (ctx.paths.kits_dir / "style" / "house_style_v1.png").write_bytes(b"png")
    (ctx.paths.kits_dir / "head_base" / "round").mkdir(parents=True)
    (ctx.paths.kits_dir / "head_base" / "round" / "zones.json").write_text("{}", encoding="utf-8")
    flags = dc.kit_flags(ctx.paths)
    assert flags == {"head_base_present": True, "house_style_present": True, "body_base_present": False, "hair_kit_empty": True}
    out = run(ctx, "CHK-S15")
    assert "no head base" not in out.message and "no house style" not in out.message and "no body base" in out.message


def test_s15_writes_the_flags_into_an_existing_manifest_only(ctx):
    manifest = ctx.paths.kits_dir / "manifest.json"
    out = run(ctx, "CHK-S15")
    assert out.detail["manifest_updated"] is False and not manifest.exists()                  # it never creates a manifest
    manifest.write_text(json.dumps({"schema": 1, "hair": {}, "flags": {"head_base_present": False}}), encoding="utf-8")
    out = run(ctx, "CHK-S15")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert out.detail["manifest_updated"] is True and data["schema"] == 1 and data["hair"] == {}
    assert data["flags"]["dreamsim_present"] is False and data["flags"]["makeup"] == "unavailable" and "blender_present" in data["flags"]
    assert run(ctx, "CHK-S15").detail["manifest_updated"] is False                            # idempotent: nothing to change the second time
    manifest.write_text("{ broken", encoding="utf-8")
    assert run(ctx, "CHK-S15").detail["manifest_updated"] is False                            # a damaged manifest is S11's problem, not rewritten


def test_s15_switches_off_a_component_whose_sha_differs(ctx, monkeypatch, tmp_path):
    root = tmp_path / "app"
    (root / "duoskin" / "data").mkdir(parents=True)
    good = b"good-model"
    (ctx.paths.models_dir / "matting").mkdir(parents=True)
    (ctx.paths.models_dir / "matting" / "m.onnx").write_bytes(b"tampered")
    (root / "duoskin" / "data" / "optional_components.json").write_text(json.dumps({"components": [
        {"id": "matting", "path": "models/matting/m.onnx", "sha256": hashlib.sha256(good).hexdigest()},
        {"id": "ocr_extra", "path": "models/ocr/missing.onnx", "sha256": "a" * 64}]}), encoding="utf-8")
    monkeypatch.setattr(config, "APP_ROOT", root)
    out = run(ctx, "CHK-S15")
    assert out.detail["switched_off"] == ["matting"] and "switched off" in out.message        # a missing file is just an absent feature
    (ctx.paths.models_dir / "matting" / "m.onnx").write_bytes(good)
    assert run(ctx, "CHK-S15").detail["switched_off"] == []


def test_kit_flags_from_the_manifest_and_hair_kit(ctx):
    (ctx.paths.kits_dir / "manifest.json").write_text('{"flags": {"hair_kit_empty": false, "body_base_present": true, "head_base_present": "yes"}}',
                                                       encoding="utf-8")
    f = dc.kit_flags(ctx.paths)
    assert f["hair_kit_empty"] is False and f["body_base_present"] is True and f["head_base_present"] is False   # non-bool values are ignored
    (ctx.paths.kits_dir / "hair" / "bob").mkdir(parents=True)
    (ctx.paths.kits_dir / "hair" / "bob" / "style.json").write_text("{}", encoding="utf-8")
    assert dc.kit_flags(ctx.paths)["hair_kit_empty"] is False


def test_s12_redaction_self_test(ctx, monkeypatch):
    assert run(ctx, "CHK-S12").status == "pass"
    monkeypatch.setattr(dc, "redact", lambda text, extra=(): text)
    out = run(ctx, "CHK-S12")
    assert out.status == "fail" and "not masked" in out.message


def test_s13_stdlib_shadowing(ctx, monkeypatch, tmp_path):
    root = tmp_path / "app"
    (root / "duoskin" / "engine").mkdir(parents=True)
    (root / "duoskin" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "APP_ROOT", root)
    assert run(ctx, "CHK-S13").status == "pass"
    (root / "duoskin" / "engine" / "gc.py").write_text("", encoding="utf-8")                 # namespaced: listed, not a failure
    out = run(ctx, "CHK-S13")
    assert out.status == "pass" and "duoskin/engine/gc.py" in out.message
    (root / "inspect.py").write_text("", encoding="utf-8")                                    # the stray file that broke numpy once
    out = run(ctx, "CHK-S13")
    assert out.status == "fail" and "inspect.py" in out.message and "Rename or delete" in out.fix
    (root / "inspect.py").unlink()
    (root / "duoskin" / "secrets.py").write_text("", encoding="utf-8")                         # directly in the package folder: risky
    assert run(ctx, "CHK-S13").status == "fail"
    d = next(d for d in dc._CHECKS if d.id == "CHK-S13")
    assert d.kind == "assert" and d.fatal


def test_s13_the_real_package_has_no_risky_shadow_files(ctx):
    out = run(ctx, "CHK-S13")
    assert out.status == "pass", out.message


def test_s14_missing_file_sets_dreamsim_present_false(ctx):
    out = run(ctx, "CHK-S14")
    assert out.status == "warn" and "degraded" in out.message and out.detail["clone_check_mode"] == "degraded"
    assert out.detail["dreamsim_present"] is False and "Optional components" in out.fix


def _dreamsim_setup(ctx, monkeypatch, tmp_path, *, sha_ok=True, expectations=True, images=True):
    """A fake app root with the manifest, a model file, its sidecar and the fixture pair; the ONNX probe is replaced."""
    root = tmp_path / "app"
    (root / "duoskin" / "data").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "fixtures" / "dreamsim").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "APP_ROOT", root)
    blob = b"onnx-bytes"
    (ctx.paths.models_dir / "dreamsim.onnx").write_bytes(blob)
    sha = hashlib.sha256(blob if sha_ok else b"other").hexdigest()
    (root / "duoskin" / "data" / "optional_components.json").write_text(
        json.dumps({"components": [{"id": "dreamsim", "path": "models/dreamsim.onnx", "sha256": sha}]}), encoding="utf-8")
    sidecar = ctx.paths.models_dir / "dreamsim.onnx.json"
    sidecar.unlink(missing_ok=True)
    for name in ("a.png", "b.png"):
        (root / "tests" / "fixtures" / "dreamsim" / name).unlink(missing_ok=True)
    if expectations:
        sidecar.write_text(
            json.dumps({"fixture_expectations": [{"pair": ["a.png", "b.png"], "distance": 0.30}]}), encoding="utf-8")
    if images:
        for name in ("a.png", "b.png"):
            (root / "tests" / "fixtures" / "dreamsim" / name).write_bytes(b"png")
    return dc.DoctorCtx(paths=ctx.paths, settings=ctx.settings, rt=None, quick=False)


def test_s14_passes_when_sha_load_and_the_fixture_pair_agree(ctx, monkeypatch, tmp_path):
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path)
    monkeypatch.setattr(dc, "probe_dreamsim", lambda model, pairs: (True, [{"expected": 0.30, "distance": 0.305}], ""))   # within +-0.01
    out = run(full, "CHK-S14")
    assert out.status == "pass" and out.detail["clone_check_mode"] == "full" and out.detail["dreamsim_present"] is True


def test_s14_fixture_pair_off_by_more_than_the_tolerance_switches_it_off(ctx, monkeypatch, tmp_path):
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path)
    monkeypatch.setattr(dc, "probe_dreamsim", lambda model, pairs: (True, [{"expected": 0.30, "distance": 0.33}], ""))
    out = run(full, "CHK-S14")
    assert out.status == "warn" and "fixture" in out.message and out.detail["dreamsim_present"] is False
    assert out.detail["clone_check_mode"] == "degraded"
    monkeypatch.setattr(dc, "probe_dreamsim", lambda model, pairs: (True, [{"expected": 0.30, "distance": 0.3099}], ""))
    assert run(full, "CHK-S14").status == "pass"                                              # the edge: |delta| <= 0.01 passes


def test_s14_corrupt_or_unverifiable_files_are_switched_off(ctx, monkeypatch, tmp_path):
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path, sha_ok=False)
    monkeypatch.setattr(dc, "probe_dreamsim", lambda *a: (_ for _ in ()).throw(AssertionError("must not load a file with a wrong sha")))
    out = run(full, "CHK-S14")
    assert out.status == "warn" and "sha256" in out.message and out.detail["dreamsim_present"] is False
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path)                                         # right sha, but the model does not load
    monkeypatch.setattr(dc, "probe_dreamsim", lambda model, pairs: (False, [], "InvalidProtobuf"))
    out = run(full, "CHK-S14")
    assert out.status == "warn" and "did not load" in out.message and out.detail["dreamsim_present"] is False
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path, expectations=False)                     # fail closed: no sidecar, no fixture check
    out = run(full, "CHK-S14")
    assert out.status == "warn" and "fixture check could not run" in out.message and out.detail["dreamsim_present"] is False
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path, images=False)
    assert "fixture" in run(full, "CHK-S14").message and run(full, "CHK-S14").detail["dreamsim_present"] is False


def test_s14_without_a_pinned_sha_still_verifies_the_fixture(ctx, monkeypatch, tmp_path):
    full = _dreamsim_setup(ctx, monkeypatch, tmp_path)
    (config.APP_ROOT / "duoskin" / "data" / "optional_components.json").unlink()               # [UNVERIFIED] until the first CI export
    monkeypatch.setattr(dc, "probe_dreamsim", lambda model, pairs: (True, [{"expected": 0.30, "distance": 0.30}], ""))
    out = run(full, "CHK-S14")
    assert out.status == "pass" and "no sha256 is pinned" in out.message


def test_s14_quick_mode_only_looks_for_the_file(ctx):
    (ctx.paths.models_dir / "dreamsim.onnx").write_bytes(b"onnx")
    out = run(ctx, "CHK-S14")
    assert out.status == "pass" and out.detail["clone_check_mode"] == "full"


def test_the_real_probe_degrades_when_onnxruntime_is_missing(ctx, monkeypatch, tmp_path):
    """Without onnxruntime (this Linux dev venv) the child process cannot load the model: the result is "not loaded", not a crash."""
    pair_a, pair_b = tmp_path / "a.png", tmp_path / "b.png"
    from PIL import Image

    for p_ in (pair_a, pair_b):
        Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(p_)
    model = ctx.paths.models_dir / "dreamsim.onnx"
    model.write_bytes(b"not an onnx file")
    loaded, measured, problem = dc.probe_dreamsim(model, [(str(pair_a), str(pair_b), 0.3)])
    assert loaded is False and measured == [] and problem


def test_report_flags_follow_dreamsim(rt, monkeypatch, tmp_path):
    (rt.paths.models_dir / "dreamsim.onnx").write_bytes(b"x")
    f = report_for(rt)["flags"]
    assert f["dreamsim_present"] is True and f["clone_check_mode"] == "full" and f["degraded_clone_check"] is False


def test_the_report_never_contains_key_values(tmp_path):
    rt = Runtime.create(tmp_path / "h", providers_mode="real")
    try:
        rt.keys.set_key("anthropic", "sk-ant-api03-" + "Z" * 40)
        rt.startup(start_threads=False, backup=False)
        text = str(dc.run_doctor(rt, quick=True))
        assert "sk-ant-api03" not in text
    finally:
        rt.shutdown()


def test_s04_a_missing_pymeshlab_only_warns_but_a_missing_ocr_or_opencv_blocks(ctx, monkeypatch):
    """pymeshlab is optional in the code (the built-in decimator takes over), so it must not block paid features."""
    ctx.quick = False

    def fake_run_py(broken):
        def run_py(code, **kw):
            if "ctypes.WinDLL" in code:
                return 0, "ok", ""
            if "TEST" in code and "RapidOCR" in code:
                return (1, "", "ModuleNotFoundError: rapidocr") if "ocr" in broken else (0, '{"ok": true, "best": 0.99, "txts": ["TEST"]}', "")
            for name in ("pymeshlab", "cv2", "onnxruntime", "resvg_py", "trimesh"):
                if f"import {name}" in code:
                    return (1, "", f"ImportError: {name}") if name in broken else (0, "ok", "")
            return 0, "ok", ""
        return run_py

    monkeypatch.setattr(dc, "run_py", fake_run_py({"pymeshlab"}))
    out = run(ctx, "CHK-S04")
    assert out.status == "warn" and "pymeshlab" in out.message
    monkeypatch.setattr(dc, "run_py", fake_run_py(set()))
    assert run(ctx, "CHK-S04").status == "pass"
    for broken in ({"cv2"}, {"onnxruntime"}, {"ocr"}, {"pymeshlab", "cv2"}):
        monkeypatch.setattr(dc, "run_py", fake_run_py(broken))
        assert run(ctx, "CHK-S04").status == "fail", broken
