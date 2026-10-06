"""Models (§6, §14.1) and config (§5.3, §14): validators, patching, atomic settings IO, paths."""
from __future__ import annotations

import json
import os
from datetime import UTC
from pathlib import Path

import pytest
from pydantic import ValidationError

from duoskin import config
from duoskin.models.common import UtcDatetime, iso_utc, parse_iso, utcnow
from duoskin.models.project import Project, ProjectCreate, ProjectSettings, Stage, slugify
from duoskin.models.settings import ModelPins, ProviderMode, Settings, SettingsPatchError, apply_settings_patch


# ------------------------------------------------------------------------------------------------- settings model
def test_defaults_match_the_spec():
    s = Settings()
    assert s.port == 8765 and s.budgets.per_duo_usd == 15.0 and s.budgets.ask_above_usd == 2.0 and s.telemetry == "off"
    assert s.models.planner == "claude-opus-5" and s.models.image_draft == "gpt-image-2.5-flare-2026-09-08"
    assert s.providers.modes["fal"] == ProviderMode.DISABLED and s.providers.modes["anthropic"] == ProviderMode.REAL
    assert s.providers.openai_ipm == 5 and s.three_d.blender_tested_versions[0] == "4.2"
    assert s.paths.exports_root == r"%USERPROFILE%\DuoSkin Exports"
    assert s.checks.reference_similarity_default is False                # requirement 7: default OFF
    assert s.budgets.regression_ask_usd == 20.0 and s.regression_reuse_plan_cache is True      # APP_SPEC 14.1 (v1.3 additions)


def test_extra_fields_are_refused():
    with pytest.raises(ValidationError):
        Settings.model_validate({"nonsense": 1})
    with pytest.raises(ValidationError):
        Settings.model_validate({"budgets": {"per_duo_usd": 5, "typo": 1}})


@pytest.mark.parametrize("bad", ["claude-opus-latest", "gpt-image-latest", "LATEST", "x-latest-y", " "])
def test_model_aliases_are_rejected(bad):
    with pytest.raises(ValidationError):
        ModelPins(planner=bad)
    with pytest.raises(ValidationError):
        ModelPins(candidates={"planner": bad})


def test_unknown_provider_and_bad_port_are_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate({"providers": {"modes": {"nope": "real"}}})
    for port in (0, 70000):
        with pytest.raises(ValidationError):
            Settings(port=port)


def test_demo_mode_forces_mock_except_disabled():
    s = Settings(demo_mode=True)
    assert s.mode_of("anthropic") == ProviderMode.MOCK and s.mode_of("fal") == ProviderMode.DISABLED
    assert Settings().mode_of("openai") == ProviderMode.REAL


def test_patch_merges_nested_dicts_and_validates():
    base = Settings()
    new = apply_settings_patch(base, {"budgets": {"per_duo_usd": 20}, "providers": {"modes": {"tripo": "mock"}},
                                      "capabilities": {"openai.mask_multi_ok": True}})
    assert new.budgets.per_duo_usd == 20 and new.budgets.ask_above_usd == 2.0            # sibling kept
    assert new.providers.modes["tripo"] == ProviderMode.MOCK and new.providers.modes["anthropic"] == ProviderMode.REAL
    again = apply_settings_patch(new, {"capabilities": {"openai.mask_multi_ok": None}})   # null removes a key
    assert again.capabilities == {}
    assert base.budgets.per_duo_usd == 15.0                                               # the original is untouched


@pytest.mark.parametrize("patch", [{"telemetry": "on"}, {"schema_version": 9}, {"budgets": {"per_duo_usd": -1}},
                                   {"models": {"planner": "claude-latest"}}, {"unknown": 1}, {"port": "abc"}, []])
def test_patch_errors(patch):
    with pytest.raises(SettingsPatchError):
        apply_settings_patch(Settings(), patch)


# ------------------------------------------------------------------------------------------------- time helpers
def test_time_helpers_are_utc_and_sortable():
    a, b = utcnow(), utcnow()
    assert a.tzinfo is not None and iso_utc(a) <= iso_utc(b) and parse_iso(iso_utc(a)) == a
    from datetime import datetime

    naive = datetime(2026, 1, 1, 12, 0, 0)   # noqa: DTZ001 - a naive value on purpose: it must be read as UTC
    assert iso_utc(naive) == "2026-01-01T12:00:00.000000Z"
    assert iso_utc(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)) < iso_utc(datetime(2026, 1, 1, 12, 0, 0, 1, tzinfo=UTC))

    from pydantic import BaseModel

    class M(BaseModel):
        t: UtcDatetime

    assert M(t=naive).t.tzinfo is not None


# ------------------------------------------------------------------------------------------------- project model
def test_slugify_is_safe():
    assert slugify("Plush Koi!") == "plush-koi" and slugify("  ") == "duo" and slugify("CON") == "con-duo"
    assert slugify("Café 日本") == "cafe"
    assert len(slugify("x" * 200)) <= 40
    import re

    assert re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", slugify("--weird__Name--"))


def test_project_validators():
    now = utcnow()
    base = {"id": "prj_1", "name": "n", "slug": "n", "created_at": now, "updated_at": now, "combo": "bg", "brief": "",
            "stage": Stage.BRIEF, "settings": ProjectSettings()}
    Project(**base)
    with pytest.raises(ValidationError):
        Project(**{**base, "combo": "xx"})
    with pytest.raises(ValidationError):
        Project(**{**base, "must_include": ["one two three four five six seven eight nine ten eleven twelve thirteen"]})
    with pytest.raises(ValidationError):
        Project(**{**base, "must_include": ["a"] * 6})
    with pytest.raises(ValidationError):
        Project(**{**base, "brief": "x" * 2001})
    assert ProjectCreate(name="n", combo="gg", must_include=["  hi  ", ""]).must_include == ["hi"]


# ------------------------------------------------------------------------------------------------- config
def test_home_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSKIN_HOME", raising=False)
    explicit = tmp_path / "explicit"
    assert config.resolve_home(explicit) == explicit
    monkeypatch.setenv("DUOSKIN_HOME", str(tmp_path / "env"))
    assert config.resolve_home() == tmp_path / "env"
    assert config.resolve_home(explicit) == explicit                                   # explicit beats the environment
    monkeypatch.delenv("DUOSKIN_HOME")
    assert "DuoSkin" in str(config.resolve_home())                                     # platformdirs default


def test_paths_layout_is_created(tmp_path):
    p = config.paths(tmp_path / "home")
    for d in (p.cas_dir, p.kits_dir, p.models_dir, p.logs_dir, p.run_dir, p.tmp_dir, p.backups_dir, p.user_data_dir, p.regression_dir):
        assert d.is_dir()
    assert p.db.name == "duoskin.sqlite3" and p.settings.name == "settings.json" and p.secrets_dpapi.name == "secrets.dpapi"
    assert config.Paths is config.DataPaths


def test_settings_roundtrip_is_atomic_and_utf8(tmp_path):
    s = Settings(port=9001, demo_mode=True)
    config.save_settings(s, tmp_path)
    assert config.load_settings(tmp_path) == s
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]                   # no temp files left behind
    json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))


def test_missing_settings_gives_defaults_and_bad_files_are_moved_aside(tmp_path):
    assert config.load_settings(tmp_path / "none") == Settings()
    (tmp_path / "settings.json").write_text('{"port": 9, "mystery": true}', encoding="utf-8")
    assert config.load_settings(tmp_path) == Settings()                                # unknown field: defaults, file kept
    moved = list(tmp_path.glob("settings.corrupt-*.json"))
    assert len(moved) == 1 and "mystery" in moved[0].read_text(encoding="utf-8")
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert config.load_settings(tmp_path) == Settings()


def test_newer_settings_schema_is_not_loaded(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    assert config.load_settings(tmp_path) == Settings()


def test_expand_path_handles_percent_vars_everywhere(monkeypatch):
    monkeypatch.setenv("USERPROFILE", "/users/ann")
    assert config.expand_path(r"%USERPROFILE%\DuoSkin Exports") == Path("/users/ann/DuoSkin Exports")
    monkeypatch.delenv("USERPROFILE")
    assert config.expand_path(r"%USERPROFILE%\x") == Path.home() / "x"
    assert str(config.exports_root(Settings())).endswith("DuoSkin Exports")


def test_server_info_roundtrip(tmp_path):
    p = config.paths(tmp_path)
    assert config.read_server_info(p) is None
    config.write_server_info(p, port=8800, instance_id="abc")
    info = config.read_server_info(p)
    assert info["port"] == 8800 and info["url"] == "http://127.0.0.1:8800/" and info["instance_id"] == "abc"
    assert info["pid"] == os.getpid()


def test_runtime_overrides_and_sticky_port(tmp_path):
    from duoskin.engine.runtime import Runtime, parse_providers_mode

    assert parse_providers_mode("mock")["fal"] == ProviderMode.MOCK
    assert parse_providers_mode("anthropic:real, tripo:mock") == {"anthropic": ProviderMode.REAL, "tripo": ProviderMode.MOCK}
    for bad in ("anthropic", "x:mock", "openai:fast"):
        with pytest.raises(ValueError):
            parse_providers_mode(bad)
    rt = Runtime.create(tmp_path, providers_mode="openai:mock")
    try:
        assert rt.effective_settings().providers.modes["openai"] == ProviderMode.MOCK
        assert rt.settings.providers.modes["openai"] == ProviderMode.REAL            # the override is never persisted
        rt.update_settings({"demo_mode": False})
        assert config.load_settings(tmp_path).providers.modes["openai"] == ProviderMode.REAL
        rt.remember_port(8790)
        assert config.load_settings(tmp_path).port == 8790 and rt.port == 8790
    finally:
        rt.shutdown()


# ------------------------------------------------------------------------------------------------- v1.3 model shapes (APP_SPEC 6.1, 6.4, 6.6, 6.9)
def test_the_licence_enum_is_one_shared_literal():
    import typing

    from duoskin.models.asset import Provenance
    from duoskin.models.common import License
    from duoskin.models.part import Part

    values = set(typing.get_args(License))
    assert values == {"n/a", "tripo_api_private_commercial", "tripo_paid_private_commercial", "tripo_free_public_ccby_noncommercial",
                      "user_made", "unknown"}
    now = utcnow()
    for lic in values:                                                                          # Part and Provenance accept the same set
        assert Provenance(source="code", created_at=now, license=lic).license == lic
        assert Part(id="a.hair", project_id="p", character="a", kind="hair", label="x", license=lic).license == lic
    with pytest.raises(ValidationError):
        Provenance(source="code", created_at=now, license="tripo_free")            # the old free-text field is gone
    with pytest.raises(ValidationError):
        Part(id="a.hair", project_id="p", character="a", kind="hair", label="x", license="mine")


def test_patch_ops_are_split_into_revision_and_change_ops():
    from duoskin.models import spec_record
    from duoskin.models.spec_record import ChangeOp, RevisionOp, SpecRecord

    assert not hasattr(spec_record, "PatchOp")
    rev = RevisionOp(op="replace", path="/a/hair/kit_style_id", value_json='"bun"', finding="3")
    chg = ChangeOp(op="add", path="/a/accessories/-", value_json="{}", reason="the user asked")
    assert set(RevisionOp.model_fields) == {"op", "path", "value_json", "finding"} and set(ChangeOp.model_fields) == {"op", "path", "value_json", "reason"}
    with pytest.raises(ValidationError):
        RevisionOp(op="replace", path="/x", value_json="1", reason="r")                          # a revision cites a finding, not a reason
    with pytest.raises(ValidationError):
        ChangeOp(op="replace", path="/x", value_json="1", finding="1")
    rec = SpecRecord(id="s", project_id="p", plan_set_id="ps", plan_index=0, spec={"a": 1}, sha256="a" * 64, patch_from_parent=[rev, chg])
    again = SpecRecord.model_validate_json(rec.model_dump_json())
    assert [type(op) for op in again.patch_from_parent] == [RevisionOp, ChangeOp]                # the union round-trips to the right class


def test_build_stamp_is_its_own_record_and_the_approval_has_no_build_fields():
    from duoskin.models.part import ApprovalRecord, BuildStamp, Part

    assert set(BuildStamp.model_fields) == {"part_id", "build_hash", "approval_hash", "build_asset_shas", "built_at", "confirmed_decision_id"}
    assert not any(name.startswith("build") for name in ApprovalRecord.model_fields)
    part = Part(id="a.hair", project_id="p", character="a", kind="hair", label="x")
    assert part.build_stamp is None
    stamp = BuildStamp(part_id="a.hair", build_hash="b" * 64, approval_hash="a" * 64, built_at=utcnow())
    assert stamp.confirmed_decision_id is None and stamp.build_asset_shas == []
    assert Part.model_validate_json(part.model_copy(update={"build_stamp": stamp}).model_dump_json()).build_stamp == stamp


def test_gate_decision_target_and_flip_action():
    from duoskin.models.gate import GateAction, GateDecision, GateDecisionIn

    assert GateAction("flip_mirrored") is GateAction.FLIP_MIRRORED
    body = GateDecisionIn(tile_id="a.face", action="reimagine", target="iris", expected_version=0, client_decision_id="c1")
    assert body.target == "iris" and GateDecisionIn(tile_id="t", action="approve", expected_version=0, client_decision_id="c2").target is None
    stored = GateDecision(id="d", gate_id="g", tile_id="t", action="reimagine", target="both", decided_at=utcnow())
    assert GateDecision.model_validate_json(stored.model_dump_json()).target == "both"


def test_registry_tables_carry_part_role_and_approvals_carry_two_stamps(tmp_path):
    from duoskin.engine.testkit import make_runtime

    rt = make_runtime(tmp_path / "h")
    try:
        def cols(table):
            return [r["name"] for r in rt.db.conn().execute(f"PRAGMA table_info({table})")]

        assert "part_role" in cols("registry_face") and "part_role" in cols("registry_print")
        assert cols("approvals") == ["project_id", "part_id", "stamp", "stamp_hash", "decision_id", "valid", "json", "created_at"]
        with pytest.raises(Exception, match="CHECK"), rt.db.tx() as c:                            # only 'approval' and 'build' exist
            c.execute("INSERT INTO approvals VALUES ('p','a.hair','other','h','d',1,'{}','t')")
    finally:
        rt.shutdown()
