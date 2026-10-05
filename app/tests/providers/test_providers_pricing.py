"""Prices, cost records and estimates (APP_SPEC 6.10, 8.8; bible 20.1)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from duoskin.providers import pricing as P


@pytest.fixture(autouse=True)
def _fresh_prices():
    P.set_prices(None)
    yield
    P.set_prices(None)


def entry(cost: dict):
    """Every cost record must validate as a models.cost.CostEntry once the ledger adds ts (the engine's job)."""
    from duoskin.models.cost import CostEntry
    return CostEntry(ts=datetime.now(UTC), **cost)


def test_claude_cost_opus_and_sonnet_rates():
    usage = {"input": 1_000_000, "output": 100_000, "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0}
    opus = P.claude_cost("claude-opus-5", usage, operation="messages.stream:L3")
    assert opus["usd"] == pytest.approx(5.0 + 2.5 + 0.5)          # input + output + cache read at 0.1x
    sonnet = P.claude_cost("claude-sonnet-5", usage, operation="messages.stream:L11")
    assert sonnet["usd"] == pytest.approx(2.0 + 1.0 + 0.2)
    assert opus["provider"] == "anthropic" and opus["basis"] == "usage" and opus["price_table"] == P.PRICE_TABLE
    entry(opus)


def test_claude_cache_write_and_batch_discount():
    usage = {"input": 0, "output": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1_000_000}
    five = P.claude_cost("claude-opus-5", usage, operation="x")
    one = P.claude_cost("claude-opus-5", usage, operation="x", cache_ttl="1h")
    assert five["usd"] == pytest.approx(6.25) and one["usd"] == pytest.approx(10.0)
    split = P.claude_cost("claude-opus-5", {**usage, "cache_creation_5m": 400_000, "cache_creation_1h": 600_000}, operation="x")
    assert split["usd"] == pytest.approx(0.4 * 6.25 + 0.6 * 10.0)
    batch = P.claude_cost("claude-opus-5", {"input": 1_000_000, "output": 0}, operation="x", batch=True)
    assert batch["usd"] == pytest.approx(2.5) and batch["batch"] is True
    entry(batch)
    assert P.claude_cost("claude-opus-5", None, operation="x")["usd"] == 0


def test_a_fallback_model_is_priced_by_its_family():
    u = {"input": 1_000_000, "output": 0}
    assert P.claude_cost("claude-opus-4-8", u, operation="x")["usd"] == pytest.approx(5.0)
    assert P.claude_cost("claude-future-9", u, operation="x")["usd"] == pytest.approx(5.0)       # unknown family: the dearer rate


def test_openai_cost_from_usage_and_from_estimate():
    usage = {"input_tokens": 1000, "output_tokens": 1756, "input_tokens_details": {"text_tokens": 400, "image_tokens": 600}}
    c = P.openai_image_cost("gpt-image-2.5-sunburst-2026-09-08", usage, operation="images.edit:I1", quality="high", n=1)
    assert c["basis"] == "usage" and c["usd"] == pytest.approx(400 * 5e-6 + 600 * 8e-6 + 1756 * 30e-6)
    est = P.openai_image_cost("gpt-image-2.5-flare-2026-09-08", None, operation="images.generate:I2", quality="low", n=4, prompt_chars=800)
    assert est["basis"] == "estimate" and est["usd"] == pytest.approx(4 * 196 * 30e-6 + 200 * 5e-6)
    entry(c)
    entry(est)
    # bible 20.1: a 1024 square at low / medium / high / xhigh / max costs about 0.0059 / 0.013 / 0.053 / 0.094 / 0.211
    for q, want in [("low", 0.0059), ("medium", 0.013), ("high", 0.053), ("xhigh", 0.094), ("max", 0.211)]:
        assert P.estimate_openai_image(quality=q, prompt_chars=0).usd == pytest.approx(want, rel=0.05)
    assert P.estimate_openai_image(quality="low", n_input_images=2).usd > P.estimate_openai_image(quality="low").usd


def test_recraft_prices():
    assert P.recraft_unit_price("recraftv4_1_vector") == 0.08 and P.recraft_unit_price("recraftv4_styles_vector") == 0.05
    assert P.recraft_unit_price("style_create") == 0.005 and P.recraft_unit_price("vectorize") == 0.01 and P.recraft_unit_price("remove_background") == 0.01
    assert P.recraft_unit_price("recraftv4_9_future") >= 0.30                       # unknown model id: the dearest known price
    c = P.recraft_cost("recraftv4_1_vector", operation="images.generate:R1", n=3, api_units=240)
    assert c["usd"] == pytest.approx(0.24) and any(u["name"] == "api_units" for u in c["units"])
    entry(c)
    assert P.estimate_recraft("recraftv4_styles_vector", n=6).usd == pytest.approx(0.30)


def test_tripo_credits_and_usd():
    assert P.tripo_credits("image_to_multiview") == 10 and P.tripo_credits("edit_multiview", views=3) == 15
    assert P.tripo_credits("multiview_to_model") == 110 and P.tripo_credits("import_model") == 0
    assert P.tripo_credits("multiview_to_model", route="p1") == 50 and P.tripo_credits("multiview_to_model", route="h31") == 40
    assert P.tripo_credits("convert", route="p1") == 10                              # routes only price generation ops
    assert P.tripo_usd(110) == pytest.approx(1.10)
    c = P.tripo_cost("P2-20260801", operation="multiview_to_model", credits=110.0, task_id="t1")
    assert c["basis"] == "credits" and c["credits"] == 110.0 and c["usd"] == pytest.approx(1.10) and c["remote_task_id"] == "t1"
    entry(c)
    unknown = P.tripo_cost("P2-20260801", operation="multiview_to_model", credits=None, task_id="t1")
    assert unknown["basis"] == "estimate" and unknown["credits"] == 110.0
    assert P.tripo_cost("m", operation="x", credits=5.0, task_id="t", balance_before=100.0)["balance_before"] == 100.0
    e = P.estimate_tripo("multiview_to_model")
    assert e.credits == 110 and e.usd == pytest.approx(1.10) and e.as_dict()["provider"] == "tripo"


def test_gemini_prices_change_after_the_promo_date():
    u = {"promptTokenCount": 1_000_000, "candidatesTokenCount": 1_000_000}
    before = P.gemini_cost("gemini-3.8-flash", u, operation="judge", today=date(2026, 12, 31))
    after = P.gemini_cost("gemini-3.8-flash", u, operation="judge", today=date(2027, 1, 1))
    assert before["usd"] == pytest.approx(0.75 + 3.75) and after["usd"] == pytest.approx(1.50 + 7.50)
    img = P.gemini_cost("gemini-3.1-flash-image", None, operation="image", image=True)
    assert img["usd"] == pytest.approx(0.067) and img["basis"] == "estimate"
    entry(before)
    assert P.estimate_gemini_judge().usd > 0


def test_estimates_validate_as_models_cost_estimate():
    from duoskin.models.cost import Estimate
    for est in (P.estimate_claude("claude-opus-5", input_tokens=20_000, output_tokens=8_000, cached_input_tokens=15_000),
                P.estimate_openai_image(quality="high", n=4), P.estimate_recraft("recraftv4_1_vector"), P.estimate_tripo("multiview_to_model"),
                P.estimate_gemini_judge()):
        Estimate(**est.as_dict())
    assert P.estimate_claude("claude-opus-5", input_tokens=1_000_000, output_tokens=0, batch=True).usd == pytest.approx(2.5)


def test_json_overrides_merge_known_numeric_leaves_only(tmp_path):
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({"claude": {"opus": {"input": 6.0, "bogus": 1}, "sonnet": "x"}, "tripo": {"usd_per_credit": 0.02}, "nonsense": {"a": 1}}),
                 encoding="utf-8")
    t = P.load_prices(f)
    assert t["claude"]["opus"]["input"] == 6.0 and "bogus" not in t["claude"]["opus"] and t["claude"]["sonnet"]["input"] == 2.0
    assert t["tripo"]["usd_per_credit"] == 0.02 and "nonsense" not in t
    assert P.load_prices(tmp_path / "missing.json")["claude"]["opus"]["input"] == 5.0
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert P.load_prices(tmp_path / "bad.json")["claude"]["opus"]["input"] == 5.0
    P.set_prices(t)
    assert P.tripo_usd(100) == pytest.approx(2.0)


def test_as_mock_relabels_the_provider_only():
    c = P.tripo_cost("m", operation="x", credits=10.0, task_id="t")
    m = P.as_mock(c)
    assert m["provider"] == "mock" and m["usd"] == c["usd"] and c["provider"] == "tripo"
    entry(m)
