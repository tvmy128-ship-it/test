"""DUOSKIN_MOCK_FAULTS: parsing, selectors and the errors a fault raises (APP_SPEC 16)."""
from __future__ import annotations

import pytest

from duoskin.providers import faults as F
from duoskin.providers.base import ProviderError

SPEC = ("openai:moderation_blocked@first,tripo:2008@seed29,anthropic:refusal@L13,anthropic:truncated@L3,anthropic:schema_invalid@L3,"
        "openai:size_drift@I2,tripo:read_timeout_after_send@T3,recraft:429@first,tripo:1007@first,tripo:2000@first,openai:timeout@I4")


def test_the_spec_example_parses():
    fs = F.parse_spec(SPEC)
    assert len(fs) == 11 and fs[0].provider == "openai" and fs[0].name == "moderation_blocked" and fs[0].selector == "first"
    assert [f.selector for f in fs[1:3]] == ["seed29", "L13"]


@pytest.mark.parametrize("bad", ["nope", "openai", "openai:", "foo:refusal@first", "openai:not_a_fault@first", "tripo:abc@first", "openai:moderation_blocked@@x"])
def test_bad_entries_are_rejected_loudly(bad):
    with pytest.raises(ValueError):
        F.parse_spec(bad)
    assert F.parse_spec("") == [] and F.parse_spec(" , ,") == [] and F.parse_spec(None) == []


def test_numeric_tripo_codes_are_accepted():
    assert F.parse_spec("tripo:2018@first,tripo:5000@seed11")[1].name == "5000"


def test_first_fires_once_on_the_first_call_only():
    inj = F.FaultInjector("openai:moderation_blocked@first")
    assert inj.check("openai", tag="I1") == "moderation_blocked"
    assert inj.check("openai", tag="I1") is None and inj.check("openai") is None
    assert inj.check("tripo") is None                       # other providers are untouched and count separately


def test_first_does_not_fire_if_the_first_call_was_not_the_provider_s_first():
    inj = F.FaultInjector("recraft:429@first")
    assert inj.check("recraft") == "429"
    inj2 = F.FaultInjector("recraft:429@n2")
    assert [inj2.check("recraft") for _ in range(3)] == [None, "429", None]


def test_tag_selector_matches_route_prefix_step_id_and_aliases_once():
    inj = F.FaultInjector("anthropic:refusal@L13,openai:size_drift@I2,tripo:read_timeout_after_send@T3")
    assert inj.check("anthropic", tag="L3_planner") is None and inj.check("anthropic", tag="L13_ip") == "refusal"
    assert inj.check("anthropic", tag="L13_ip") is None                              # once
    assert inj.check("openai", tag="I1") is None and inj.check("openai", tag="I2") == "size_drift"
    assert inj.check("tripo", tag="T1", aliases=["image_to_multiview"]) is None
    assert inj.check("tripo", tag="T3", aliases=["multiview_to_model"]) == "read_timeout_after_send"
    assert F.FaultInjector("openai:size_drift@I1").check("openai", tag="I10") is None      # I1 does not match I10


def test_alias_selector_by_operation_name():
    inj = F.FaultInjector("tripo:2008@multiview_to_model")
    assert inj.check("tripo", tag="T3", aliases=["multiview_to_model"]) == "2008"


def test_seed_selector():
    inj = F.FaultInjector("tripo:2008@seed29")
    assert inj.check("tripo", seed=11) is None and inj.check("tripo", seed=None) is None
    assert inj.check("tripo", seed=29) == "2008" and inj.check("tripo", seed=29) is None


def test_repeat_counts_and_always():
    inj = F.FaultInjector("recraft:429@first*3,openai:timeout@always,anthropic:timeout@L3*")
    assert [inj.check("recraft") for _ in range(5)] == ["429", "429", "429", None, None]
    assert [inj.check("openai") for _ in range(4)] == ["timeout"] * 4
    assert [inj.check("anthropic", tag="L3_planner") for _ in range(4)] == ["timeout"] * 4


def test_fired_log_and_reset():
    inj = F.FaultInjector("openai:moderation_blocked@first")
    inj.check("openai", tag="I1")
    assert inj.fired == [{"provider": "openai", "fault": "moderation_blocked", "tag": "I1", "seed": None, "call": 1}]
    inj.reset()
    assert inj.fired == [] and inj.check("openai") == "moderation_blocked"


def test_default_injector_follows_the_environment(monkeypatch):
    assert not F.default_injector()
    monkeypatch.setenv("DUOSKIN_MOCK_FAULTS", "openai:moderation_blocked@first")
    a = F.default_injector()
    assert a.check("openai") == "moderation_blocked" and F.default_injector() is a and F.default_injector().check("openai") is None
    monkeypatch.setenv("DUOSKIN_MOCK_FAULTS", "recraft:429@first")
    b = F.default_injector()
    assert b is not a and b.check("recraft") == "429"
    assert F.FaultInjector.from_env({"DUOSKIN_MOCK_FAULTS": "tripo:1007@first"}).check("tripo") == "1007"


@pytest.mark.parametrize(("provider", "fault", "kind", "code"), [
    ("openai", "moderation_blocked", "moderation", "moderation_blocked"), ("openai", "timeout", "timeout", "timeout"),
    ("openai", "rate_limit", "rate_limit", None), ("openai", "insufficient_quota", "billing", "insufficient_quota"),
    ("openai", "org_unverified", "permission", None), ("openai", "unknown_parameter", "capability", "unknown_parameter"),
    ("openai", "server", "server", None), ("openai", "auth", "auth", None), ("openai", "network", "network", None),
    ("anthropic", "rate_limit", "rate_limit", None), ("anthropic", "overloaded", "overloaded", None), ("anthropic", "auth", "auth", None),
    ("anthropic", "billing", "billing", None), ("anthropic", "schema_too_complex", "schema_too_complex", "schema_too_complex"),
    ("anthropic", "timeout", "timeout", None), ("anthropic", "server", "server", None),
    ("recraft", "429", "rate_limit", None), ("recraft", "500", "server", None), ("recraft", "401", "auth", None), ("recraft", "402", "billing", None),
    ("recraft", "moderation", "moderation", None), ("recraft", "style_required", "bad_request", "style_id_required"), ("recraft", "timeout", "timeout", None),
    ("tripo", "1007", "rate_limit", "1007"), ("tripo", "2000", "concurrency", "2000"), ("tripo", "2010", "billing", "2010"),
    ("tripo", "2015", "not_found", "2015"), ("tripo", "auth", "auth", None), ("tripo", "connect_error", "network", None),
    ("gemini", "image_safety", "moderation", "IMAGE_SAFETY"), ("gemini", "image_prohibited", "moderation", "IMAGE_PROHIBITED_CONTENT"),
    ("gemini", "recitation", "recitation", "IMAGE_RECITATION"), ("gemini", "no_image", "other", "NO_IMAGE"), ("gemini", "429", "rate_limit", None),
])
def test_error_faults_raise_the_real_errors(provider, fault, kind, code):
    with pytest.raises(ProviderError) as ei:
        F.raise_for(provider, fault)
    e = ei.value
    assert e.kind == kind and e.provider == provider and (code is None or e.code == code) and e.user_message


@pytest.mark.parametrize(("provider", "fault"), [
    ("anthropic", "refusal"), ("anthropic", "truncated"), ("anthropic", "schema_invalid"), ("anthropic", "fail_rule"), ("openai", "size_drift"),
    ("openai", "opaque_alpha"), ("openai", "usage_none"), ("recraft", "hostile_svg"), ("tripo", "read_timeout_after_send"), ("tripo", "2008"),
    ("tripo", "slow"), ("gemini", "empty_text"),
])
def test_behavioural_faults_return_none_for_the_mock_to_act_on(provider, fault):
    assert F.raise_for(provider, fault) is None


def test_every_known_fault_is_either_an_error_or_a_behaviour():
    for provider, names in F.KNOWN_FAULTS.items():
        for n in names:
            try:
                F.raise_for(provider, n)
            except ProviderError:
                assert n in F.ERROR_FAULTS[provider], (provider, n)
            else:
                assert n in F.BEHAVIOUR_FAULTS.get(provider, ()), (provider, n)
