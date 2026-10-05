"""duoskin doctor (CHK-S01..S14, with the issue-file split of CHK-S11 and the new CHK-S14)."""
from __future__ import annotations

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
    assert ids == ["CHK-S01", "CHK-S02", "CHK-S03", "CHK-S04", "CHK-S05", "CHK-S06", "CHK-S07", "CHK-S08", "CHK-S09", "CHK-S10", "CHK-S11",
                   "CHK-S11b", "CHK-S11c", "CHK-S12", "CHK-S13", "CHK-S14"]


def test_a_fresh_install_is_never_blocked(rt):
    """Issue file #2: no house style, no head base and no dreamsim.onnx only warn and route to reduced modes."""
    report = report_for(rt)
    assert report["blocks_paid_features"] is False and report["broken_install"] is False
    assert report["summary"]["failed"] == 0
    assert item(report, "CHK-S11b")["status"] == "warn" and item(report, "CHK-S11c")["status"] == "warn" and item(report, "CHK-S14")["status"] == "warn"
    for cid in ("CHK-S11b", "CHK-S11c", "CHK-S14"):
        assert item(report, cid)["blocking"] is False and item(report, cid)["kind"] == "soft"
    assert report["exit_code"] == 2                                                         # warnings: setup says "finished with warnings"
    assert report["flags"] == {**report["flags"], "head_base_present": False, "house_style_present": False,
                               "degraded_clone_check": True, "clone_check_mode": "degraded"}


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

    cases = [([make("pass")], 0), ([make("na")], 0), ([make("warn", "soft")], 2), ([make("fail")], 2),
             ([make("fail", fatal=True)], 3), ([make("pass"), make("warn", "soft")], 2)]
    for checks, expected in cases:
        monkeypatch.setattr(dc, "_CHECKS", checks)
        assert dc.run_doctor(rt, quick=True)["exit_code"] == expected
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
    assert "[ OK ] CHK-S02" in text and "[WARN] CHK-S11c" in text and "What to do:" in text and "Nothing blocks you" in text
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
    monkeypatch.setattr(sys, "version_info", (3, 12, 4, "final", 0))
    assert "not supported" in run(ctx, "CHK-S03").message


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


def test_s09_blender_is_optional_and_checked_when_present(ctx, tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSKIN_BLENDER", raising=False)
    monkeypatch.setattr(dc.shutil, "which", lambda name: None)
    monkeypatch.setattr(dc, "detect_blender", lambda settings: None)
    out = run(ctx, "CHK-S09")
    assert out.status == "na" and "optional" in out.message
    fake = tmp_path / "blender"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Blender 4.2.3'; exit 0; fi\nexit 3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(dc, "detect_blender", lambda settings: fake)
    full = dc.DoctorCtx(paths=ctx.paths, settings=ctx.settings, rt=None, quick=False)
    assert run(full, "CHK-S09").status == "pass"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Blender 3.6.0'; exit 0; fi\nexit 3\n", encoding="utf-8")
    out = run(full, "CHK-S09")
    assert out.status == "fail" and "3.6" in out.message
    fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Blender 4.2.3'; exit 0; fi\nexit 0\n", encoding="utf-8")
    assert "expected 3" in run(full, "CHK-S09").message                                      # launcher-style: exit 0 when the script fails


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


def test_s11b_and_s11c_follow_the_kit_files_and_never_block(ctx):
    assert run(ctx, "CHK-S11b").status == "warn" and run(ctx, "CHK-S11c").status == "warn"
    (ctx.paths.kits_dir / "style").mkdir(parents=True)
    (ctx.paths.kits_dir / "style" / "house_style_v1.png").write_bytes(b"png")
    (ctx.paths.kits_dir / "head_base" / "round").mkdir(parents=True)
    (ctx.paths.kits_dir / "head_base" / "round" / "zones.json").write_text("{}", encoding="utf-8")
    assert run(ctx, "CHK-S11b").status == "pass" and run(ctx, "CHK-S11c").status == "pass"
    flags = dc.kit_flags(ctx.paths)
    assert flags == {"head_base_present": True, "house_style_present": True, "body_base_present": False, "hair_kit_empty": True}
    assert all(d.kind == "soft" for d in dc._CHECKS if d.id in ("CHK-S11b", "CHK-S11c", "CHK-S14"))


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


def test_s14_dreamsim(ctx):
    out = run(ctx, "CHK-S14")
    assert out.status == "warn" and "DEGRADED" in out.message and out.detail["clone_check_mode"] == "degraded"
    (ctx.paths.models_dir / "dreamsim.onnx").write_bytes(b"onnx")
    out = run(ctx, "CHK-S14")
    assert out.status == "pass" and out.detail["clone_check_mode"] == "full"
    full = dc.DoctorCtx(paths=ctx.paths, settings=ctx.settings, rt=None, quick=False)
    out = run(full, "CHK-S14")                                                                # not a real model: stays degraded
    assert out.status == "warn" and out.detail["clone_check_mode"] == "degraded"


def test_report_flags_follow_dreamsim(rt):
    (rt.paths.models_dir / "dreamsim.onnx").write_bytes(b"x")
    f = report_for(rt)["flags"]
    assert f["clone_check_mode"] == "full" and f["degraded_clone_check"] is False


def test_the_report_never_contains_key_values(tmp_path):
    rt = Runtime.create(tmp_path / "h", providers_mode="real")
    try:
        rt.keys.set_key("anthropic", "sk-ant-api03-" + "Z" * 40)
        rt.startup(start_threads=False, backup=False)
        text = str(dc.run_doctor(rt, quick=True))
        assert "sk-ant-api03" not in text
    finally:
        rt.shutdown()
