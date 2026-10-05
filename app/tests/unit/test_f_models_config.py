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
