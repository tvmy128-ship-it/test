"""Registry: mode lookup, keys, caching, disabled adapters, settings integration (APP_SPEC 7, 14, 16)."""
from __future__ import annotations

import pytest

from duoskin.providers import registry as R
from duoskin.providers.base import ProviderError
from duoskin.providers.fal import FalProvider
from duoskin.providers.mock.gemini import MockGemini
from duoskin.providers.mock.images import MockImages
from duoskin.providers.mock.llm import MockLLM
from duoskin.providers.mock.recraft import MockRecraft
from duoskin.providers.mock.tripo import MockTripo

KEYS = {"anthropic": "sk-ant-aaaa1111", "openai": "sk-bbbb2222", "recraft": "rc-cccc3333", "tripo": "tsk_dddd4444", "gemini": "AIzaeeee5555", "fal": "fal-ffff"}


def reg(modes=None, keys=None, **kw) -> R.ProviderRegistry:
    modes = {p: "real" for p in R.PROVIDERS} | (modes or {})
    keys = KEYS if keys is None else keys
    kw.setdefault("settings_loader", lambda: None)
    return R.ProviderRegistry(mode_lookup=lambda p: modes[p], key_provider=lambda p: keys.get(p), **kw)


# ----- env parsing -------------------------------------------------------------------------------------------------------------

def test_parse_env():
    assert R.parse_env("mock") == ("mock", {}) and R.parse_env("") == (None, {}) and R.parse_env(None) == (None, {})
    assert R.parse_env("anthropic:real,tripo:mock") == (None, {"anthropic": "real", "tripo": "mock"})
    assert R.parse_env("mock, openai:real") == ("mock", {"openai": "real"})
    for bad in ("bogus", "openai:fake", "nope:mock", "anthropic:"):
        with pytest.raises(ValueError):
            R.parse_env(bad)


def test_env_overrides_everything():
    r = R.ProviderRegistry(mode_lookup=lambda p: "real", key_provider=lambda p: "k", env={"DUOSKIN_PROVIDERS": "mock"}, settings_loader=lambda: None)
    assert all(r.mode_of(p) == "mock" for p in R.PROVIDERS)
    r2 = R.ProviderRegistry(mode_lookup=lambda p: "mock", key_provider=lambda p: "k", env={"DUOSKIN_PROVIDERS": "anthropic:real,tripo:disabled"}, settings_loader=lambda: None)
    assert r2.mode_of("anthropic") == "real" and r2.mode_of("tripo") == "disabled" and r2.mode_of("openai") == "mock"
    bad = R.ProviderRegistry(env={"DUOSKIN_PROVIDERS": "wat"}, settings_loader=lambda: None)
    with pytest.raises(ValueError):
        bad.mode_of("openai")
    with pytest.raises(ValueError):
        reg().mode_of("unknown")


def test_process_wide_registry_reads_the_environment(monkeypatch):
    monkeypatch.setenv("DUOSKIN_PROVIDERS", "mock")
    R.reset()
    assert isinstance(R.get("openai"), MockImages) and R.mode_of("tripo") == "mock"
    assert R.get("openai") is R.get("openai")
    R.configure(mode_lookup=lambda p: "disabled")
    monkeypatch.delenv("DUOSKIN_PROVIDERS")
    assert isinstance(R.get("openai"), R.DisabledProvider)
    assert R.status()["openai"]["mode"] == "disabled"


def test_without_settings_or_env_the_default_is_mock_and_fal_disabled():
    r = R.ProviderRegistry(settings_loader=lambda: None, env={})
    assert r.mode_of("anthropic") == "mock" and r.mode_of("fal") == "disabled"
    assert isinstance(r.get("anthropic"), MockLLM) and isinstance(r.get("fal"), R.DisabledProvider)


# ----- mocks ------------------------------------------------------------------------------------------------------------------

def test_mock_adapters_are_singletons_with_the_right_classes():
    r = reg({p: "mock" for p in R.PROVIDERS})
    classes = {"anthropic": MockLLM, "openai": MockImages, "recraft": MockRecraft, "tripo": MockTripo, "gemini": MockGemini, "fal": FalProvider}
    for p, cls in classes.items():
        a = r.get(p)
        assert isinstance(a, cls) and r.get(p) is a
    r.reset()
    assert r.get("openai") is not None


def test_mocks_share_flags_and_cost_sink_and_never_need_keys():
    seen = []
    r = reg({p: "mock" for p in R.PROVIDERS}, keys={}, cost_sink=seen.append)
    assert r.get("openai").flags is r.flags is r.get("recraft").flags
    from duoskin.providers.base import CallCtx
    from duoskin.providers.recraft import VectorRequest
    r.get("recraft").generate(VectorRequest(model="recraftv4_1_vector", prompt="x", size="1024x1024", n=1), CallCtx.null())
    assert len(seen) == 1 and seen[0]["provider"] == "mock"


def test_mock_faults_can_be_injected_through_the_registry():
    from duoskin.providers.faults import FaultInjector
    r = reg({p: "mock" for p in R.PROVIDERS}, faults=FaultInjector("recraft:429@first"))
    from duoskin.providers.base import CallCtx
    from duoskin.providers.recraft import VectorRequest
    with pytest.raises(ProviderError):
        r.get("recraft").generate(VectorRequest(model="recraftv4_1_vector", prompt="x", size="1024x1024", n=1), CallCtx.null())


# ----- real ---------------------------------------------------------------------------------------------------------------------

def test_real_adapters_are_built_with_the_key_the_provider_returns():
    from duoskin.providers.anthropic_llm import AnthropicProvider
    from duoskin.providers.gemini import GeminiProvider
    from duoskin.providers.openai_images import OpenAIImages
    from duoskin.providers.recraft import RecraftProvider
    from duoskin.providers.tripo import TripoApi
    r = reg()
    for p, cls in (("anthropic", AnthropicProvider), ("openai", OpenAIImages), ("recraft", RecraftProvider), ("tripo", TripoApi),
                   ("gemini", GeminiProvider), ("fal", FalProvider)):
        a = r.get(p)
        assert isinstance(a, cls) and r.get(p) is a
        for secret in KEYS.values():
            assert secret not in repr(a)
    assert r.get("openai").client.max_retries == 0 and r.get("anthropic").client.max_retries == 2
    assert r.get("openai").flags is r.flags


def test_a_missing_key_is_an_auth_error_with_a_hint_and_real_available_says_so():
    r = reg(keys={"anthropic": "k"})
    assert r.real_available("anthropic") and not r.real_available("openai")
    with pytest.raises(ProviderError) as ei:
        r.get("openai")
    assert ei.value.kind == "auth" and ei.value.code == "missing_key" and "OpenAI" in ei.value.user_message and ei.value.billed == "no"
    with pytest.raises(ProviderError):
        reg(keys={"openai": ""}).get("openai")


def test_a_changed_key_builds_a_new_adapter_and_old_ones_stay_cached_per_key():
    keys = {"openai": "sk-one"}
    r = reg(keys=keys)
    a = r.get("openai")
    assert r.get("openai") is a
    keys["openai"] = "sk-two"
    b = r.get("openai")
    assert b is not a
    keys["openai"] = "sk-one"
    assert r.get("openai") is a


def test_switching_a_mode_changes_the_adapter():
    modes = {p: "real" for p in R.PROVIDERS}
    r = R.ProviderRegistry(mode_lookup=lambda p: modes[p], key_provider=lambda p: KEYS[p], settings_loader=lambda: None)
    real = r.get("tripo")
    modes["tripo"] = "mock"
    assert isinstance(r.get("tripo"), MockTripo) and r.get("tripo") is not real
    modes["tripo"] = "disabled"
    d = r.get("tripo")
    assert isinstance(d, R.DisabledProvider)
    with pytest.raises(ProviderError) as ei:
        d.image_to_multiview("tok")
    assert ei.value.code == "provider_disabled" and "switched off" in ei.value.user_message and ei.value.billed == "no"
    with pytest.raises(AttributeError):
        _ = d._private


def test_keystore_is_imported_lazily_and_failures_mean_no_key(monkeypatch):
    import sys
    import types
    r = R.ProviderRegistry(mode_lookup=lambda p: "real", settings_loader=lambda: None)
    monkeypatch.setitem(sys.modules, "duoskin.keystore", None)             # import fails -> no key
    assert r.key_for("openai") is None
    ks = types.ModuleType("duoskin.keystore")
    ks.get_key = lambda p: f"key-for-{p}"
    monkeypatch.setitem(sys.modules, "duoskin.keystore", ks)
    assert r.key_for("openai") == "key-for-openai" and r.real_available("openai")
    ks.get_key = lambda p: 1 / 0
    assert r.key_for("openai") is None


def test_status_never_contains_keys():
    r = reg({"openai": "mock", "recraft": "disabled"}, keys={"anthropic": "sk-secret-1234"})
    st = r.status()
    assert st["openai"] == {"mode": "mock", "key_stored": False, "effective": "mock"}
    assert st["anthropic"]["effective"] == "real" and st["tripo"]["effective"] == "unavailable: no key" and st["recraft"]["effective"] == "disabled"
    assert "sk-secret" not in repr(st)
    bad = R.ProviderRegistry(env={"DUOSKIN_PROVIDERS": "oops"}, settings_loader=lambda: None).status()
    assert bad["openai"]["mode"] == "invalid"


# ----- settings integration -------------------------------------------------------------------------------------------------------

def test_settings_drive_modes_limits_flags_and_pins():
    from duoskin.models.settings import Settings
    s = Settings.model_validate({"demo_mode": False, "providers": {"modes": {"openai": "real", "tripo": "mock", "gemini": "real", "anthropic": "real"},
                                                                    "openai_ipm": 7, "openai_concurrency": 4, "gemini_key_billed": True},
                                 "models": {"planner": "claude-opus-5-2"}, "capabilities": {"openai.mask_multi_ok": True}})
    r = R.ProviderRegistry(key_provider=lambda p: KEYS[p], settings_loader=lambda: s, env={})
    assert r.mode_of("tripo") == "mock" and r.mode_of("openai") == "real"
    oi = r.get("openai")
    assert oi.limiter.ipm_capacity == 7 and oi.limiter.concurrent == 4
    assert r.flags.get("openai.mask_multi_ok") is True and oi.flags is r.flags
    assert r.get("anthropic").routes["L3_planner"].model == "claude-opus-5-2"
    assert r.get("gemini").key_billed is True
    mi = R.ProviderRegistry(settings_loader=lambda: s.model_copy(update={"demo_mode": True}), env={}, key_provider=lambda p: None)
    assert all(mi.mode_of(p) in ("mock", "disabled") for p in R.PROVIDERS) and isinstance(mi.get("openai"), MockImages)
    assert mi.get("openai").per_request_cap() == 4


def test_flag_changes_go_to_the_flag_sink():
    changes = []
    r = reg(flag_sink=lambda k, v: changes.append((k, v)))
    r.flags.set("openai.mask_multi_ok", True)
    assert changes == [("openai.mask_multi_ok", True)]


def test_configure_rejects_unknown_options():
    with pytest.raises(TypeError):
        reg().configure(bogus=1)
