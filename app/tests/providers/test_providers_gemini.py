"""Gemini adapter (judge and backup image) and the fal stub (APP_SPEC 7.6, 7.7)."""
from __future__ import annotations

import base64
import json

import pytest
from prov_helpers import FakeClock, Spy, json_response, noop_sleep, png_bytes

from duoskin.providers import gemini as G
from duoskin.providers._http import httpx
from duoskin.providers.base import CallCtx, ProviderError, RateLimiter
from duoskin.providers.fal import FalProvider

CTX = CallCtx.null()
RULES = [G.RuleSpec(rule_id="fp_round", statement="The iris is round."), G.RuleSpec(rule_id="fp_one_part", statement="Only one part is drawn.")]
PNG = png_bytes(300, 300)
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def provider(spy: Spy, **kw) -> G.GeminiProvider:
    ft = FakeClock()
    kw.setdefault("limiter", RateLimiter(concurrent=2, clock=ft.now, sleep=ft.sleep))
    kw.setdefault("sleep", noop_sleep)
    return G.GeminiProvider("AIzaSyTESTKEY0000000000000000000", transport=spy.transport, **kw)


def judged(checks, **extra):
    return json_response({"candidates": [{"content": {"parts": [{"text": json.dumps({"checks": checks})}]}, "finishReason": "STOP"}],
                          "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 60}, **extra})


def passed(rule_id, ok=True):
    return {"rule_id": rule_id, "evidence": f"saw {rule_id}", "pass": ok}


def text_only(text):
    return json_response({"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


def image_resp(data: bytes, *, thought_first=True, finish="STOP"):
    parts = []
    if thought_first:
        parts.append({"thought": True, "inlineData": {"mimeType": "image/png", "data": base64.b64encode(png_bytes(8, 8)).decode()}})
    parts.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(data).decode()}})
    return json_response({"candidates": [{"content": {"parts": parts}, "finishReason": finish}], "usageMetadata": {"promptTokenCount": 10}})


# ----- judge ---------------------------------------------------------------------------------------------------------

def test_judge_request_shape_and_verdicts_in_requested_order():
    spy = Spy(judged([passed("fp_one_part", False), passed("fp_round")]))
    out = provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    spy.assert_hit(1)
    http, body = spy.requests[0], spy.bodies[0]
    assert str(http.url).endswith("/v1beta/models/gemini-3.8-flash:generateContent") and "key=" not in str(http.url)
    assert http.headers["x-goog-api-key"].startswith("AIza")
    gc = body["generationConfig"]
    assert gc["responseMimeType"] == "application/json" and gc["thinkingConfig"] == {"thinkingLevel": "LOW"} and gc["mediaResolution"] == "MEDIA_RESOLUTION_HIGH"
    for k in ("temperature", "topP", "topK", "top_p", "top_k"):
        assert k not in gc
    props = list(gc["responseJsonSchema"]["properties"]["checks"]["items"]["properties"])
    assert props.index("evidence") < props.index("pass")                  # evidence before the verdict
    parts = body["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/png" and "fp_round" in parts[1]["text"] and "The iris is round." in parts[1]["text"]
    assert [(v.rule_id, v.passed) for v in out] == [("fp_round", True), ("fp_one_part", False)]
    assert out[0].evidence == "saw fp_round"


def test_empty_text_is_a_fail_after_one_retry():
    spy = Spy(text_only(""), text_only(""))
    out = provider(spy).judge(PNG, RULES, thinking_level="MEDIUM", ctx=CTX)
    spy.assert_hit(2)
    assert [v.passed for v in out] == [False, False] and "empty response" in out[0].evidence
    assert spy.bodies[0]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "MEDIUM"


def test_empty_then_good_answer_passes_on_the_retry():
    spy = Spy(text_only(""), judged([passed("fp_round"), passed("fp_one_part")]))
    out = provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    spy.assert_hit(2)
    assert all(v.passed for v in out)


def test_thought_parts_do_not_count_as_text():
    resp = json_response({"candidates": [{"content": {"parts": [{"thought": True, "text": "thinking about it"},
                                                                   {"text": json.dumps({"checks": [passed("fp_round"), passed("fp_one_part")]})}]}}]})
    assert all(v.passed for v in provider(Spy(resp)).judge(PNG, RULES, thinking_level="LOW", ctx=CTX))
    assert G.extract_text({"candidates": [{"content": {"parts": [{"thought": True, "text": "x"}]}}]}) == ""


def test_missing_rule_fails_and_unparseable_fails():
    spy = Spy(judged([passed("fp_round")]), judged([passed("fp_round")]))
    out = provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    spy.assert_hit(2)                                               # retried once for the missing rule
    assert out[0].passed and not out[1].passed and "missing" in out[1].evidence
    out2 = provider(Spy(text_only("not json"), text_only("{broken"))).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    assert [v.passed for v in out2] == [False, False]


def test_unknown_and_duplicate_rule_ids_in_the_answer_are_ignored():
    spy = Spy(judged([passed("made_up"), passed("fp_round"), passed("fp_round", False), passed("fp_one_part")]))
    out = provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    assert [(v.rule_id, v.passed) for v in out] == [("fp_round", True), ("fp_one_part", True)]


@pytest.mark.parametrize(("kw", "code"), [
    ({"thinking_level": "MINIMAL"}, "bad_thinking_level"), ({"thinking_level": "HIGH"}, "bad_thinking_level"),
])
def test_judge_rejects_invalid_thinking_levels(kw, code):
    spy = Spy()
    with pytest.raises(ProviderError) as ei:
        provider(spy).judge(PNG, RULES, ctx=CTX, **kw)
    assert ei.value.code == code and not spy.requests


def test_judge_rejects_bad_rule_sets_and_non_png():
    p = provider(Spy())
    many = [G.RuleSpec(rule_id=f"r{i}", statement="s") for i in range(6)]
    for rules in ([], many, [RULES[0], RULES[0]]):
        with pytest.raises(ProviderError) as ei:
            p.judge(PNG, rules, thinking_level="LOW", ctx=CTX)
        assert ei.value.code == "bad_rules"
    with pytest.raises(ProviderError) as e2:
        p.judge(b"\xff\xd8jpeg", RULES, thinking_level="LOW", ctx=CTX)
    assert e2.value.code == "not_png"


def test_private_images_need_a_billed_key():
    with pytest.raises(ProviderError) as ei:
        provider(Spy()).judge(PNG, RULES, thinking_level="LOW", ctx=CTX, private=True)
    assert ei.value.code == "private_image_unbilled_key"
    spy = Spy(judged([passed("fp_round"), passed("fp_one_part")]))
    provider(spy, key_billed=True).judge(PNG, RULES, thinking_level="LOW", ctx=CTX, private=True)
    spy.assert_hit(1)


def test_cost_is_recorded_for_the_judge():
    seen = []
    provider(Spy(judged([passed("fp_round"), passed("fp_one_part")])), cost_sink=seen.append).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    assert len(seen) == 1 and seen[0]["provider"] == "gemini" and seen[0]["usd"] > 0


# ----- backup image -----------------------------------------------------------------------------------------------

def test_image_request_shape_and_thought_parts_skipped():
    out_png = png_bytes(64, 64, (10, 20, 30, 255))
    spy = Spy(image_resp(out_png))
    data, kind, meta = provider(spy).generate(prompt="Draw a flat shape.", images=[PNG], aspect="1:1", size="1K", ctx=CTX)
    spy.assert_hit(1)
    gc = spy.bodies[0]["generationConfig"]
    assert gc["responseModalities"] == ["IMAGE"] and gc["imageConfig"] == {"aspectRatio": "1:1", "imageSize": "1K"}
    assert gc["thinkingConfig"] == {"thinkingLevel": "MINIMAL"} and "gemini-3.1-flash-image" in str(spy.requests[0].url)
    assert data == out_png and kind == "png" and meta["synthid"] is True and meta["has_alpha"] is False


def test_jpeg_is_reported_as_jpeg():
    jpg = png_bytes(8, 8, (255, 255, 255), mode="RGB")
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.open(io.BytesIO(jpg)).save(buf, "JPEG")
    data, kind, _ = provider(Spy(image_resp(buf.getvalue()))).generate(prompt="x", images=[], aspect="1:1", size="1K", ctx=CTX)
    assert kind == "jpeg" and data[:2] == b"\xff\xd8"


def test_unknown_image_bytes_rejected():
    with pytest.raises(ProviderError) as ei:
        provider(Spy(image_resp(b"GIF89a....."))).generate(prompt="x", images=[], aspect="1:1", size="1K", ctx=CTX)
    assert ei.value.code == "bad_magic_unknown"


@pytest.mark.parametrize(("finish", "kind"), [("IMAGE_SAFETY", "moderation"), ("IMAGE_PROHIBITED_CONTENT", "moderation"), ("IMAGE_RECITATION", "recitation")])
def test_finish_reasons_map_to_kinds(finish, kind):
    resp = json_response({"candidates": [{"finishReason": finish}]})
    with pytest.raises(ProviderError) as ei:
        provider(Spy(resp)).generate(prompt="x", images=[], aspect="1:1", size="1K", ctx=CTX)
    assert ei.value.kind == kind and not ei.value.retryable
    if kind == "recitation":
        assert "originality" in ei.value.user_message


def test_blocked_prompt_has_no_candidates():
    resp = json_response({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
    with pytest.raises(ProviderError) as ei:
        provider(Spy(resp)).generate(prompt="x", images=[], aspect="1:1", size="1K", ctx=CTX)
    assert ei.value.kind == "moderation" and ei.value.code == "PROHIBITED_CONTENT" and ei.value.billed == "no"


def test_no_image_part_is_an_error():
    resp = json_response({"candidates": [{"content": {"parts": [{"text": "I cannot draw that"}]}, "finishReason": "NO_IMAGE"}]})
    with pytest.raises(ProviderError) as ei:
        provider(Spy(resp)).generate(prompt="x", images=[], aspect="1:1", size="1K", ctx=CTX)
    assert ei.value.code == "NO_IMAGE" and ei.value.retryable


@pytest.mark.parametrize(("kw", "code"), [({"size": "2K"}, "bad_size"), ({"aspect": "square"}, "bad_aspect"), ({"prompt": " "}, "bad_input"),
                                          ({"images": [b"jpg"]}, "not_png")])
def test_image_request_validation(kw, code):
    args = {"prompt": "x", "images": [], "aspect": "1:1", "size": "1K", "ctx": CTX, **kw}
    spy = Spy()
    with pytest.raises(ProviderError) as ei:
        provider(spy).generate(**args)
    assert ei.value.code == code and not spy.requests


# ----- errors ----------------------------------------------------------------------------------------------------------

def gerr(status, message, code="INVALID_ARGUMENT"):
    return httpx.Response(status, json={"error": {"code": status, "message": message, "status": code}})


@pytest.mark.parametrize(("status", "message", "code", "kind"), [
    (400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", "auth"), (400, "bad field", "INVALID_ARGUMENT", "bad_request"),
    (403, "no access", "PERMISSION_DENIED", "permission"), (404, "model not found", "NOT_FOUND", "not_found"),
])
def test_client_errors_not_retried(status, message, code, kind):
    spy = Spy(gerr(status, message, code))
    with pytest.raises(ProviderError) as ei:
        provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    spy.assert_hit(1)
    assert ei.value.kind == kind and ei.value.billed == "no"


def test_429_and_503_are_retried_with_backoff():
    spy = Spy(gerr(429, "quota", "RESOURCE_EXHAUSTED"), gerr(503, "busy", "UNAVAILABLE"), judged([passed("fp_round"), passed("fp_one_part")]))
    out = provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    spy.assert_hit(3)
    assert all(v.passed for v in out)
    spy2 = Spy(*[gerr(503, "busy", "UNAVAILABLE")] * 3)
    with pytest.raises(ProviderError) as ei:
        provider(spy2).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    spy2.assert_hit(3)
    assert ei.value.kind == "overloaded" and ei.value.retryable


def test_timeout_and_network():
    with pytest.raises(ProviderError) as e1:
        provider(Spy(httpx.ReadTimeout("t"))).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    assert e1.value.kind == "timeout"
    with pytest.raises(ProviderError) as e2:
        provider(Spy(httpx.ConnectError("c"))).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    assert e2.value.kind == "network"


def test_latest_aliases_rejected_and_key_not_leaked():
    with pytest.raises(ValueError):
        G.GeminiProvider("k", judge_model="gemini-flash-latest")
    p = provider(Spy())
    assert "AIza" not in repr(p)
    spy = Spy(gerr(400, "API key not valid: AIzaSyTESTKEY0000000000000000000", "INVALID_ARGUMENT"))
    with pytest.raises(ProviderError) as ei:
        provider(spy).judge(PNG, RULES, thinking_level="LOW", ctx=CTX)
    assert "AIzaSyTEST" not in str(ei.value)


# ----- fal ----------------------------------------------------------------------------------------------------------------

def test_fal_is_a_stub():
    f = FalProvider("fal-key")
    assert f.status() == {"provider": "fal", "implemented": False, "key_stored": True, "label": "stored, unused"}
    for call in (lambda: f.generate(), lambda: f.edit(), lambda: f.call()):
        with pytest.raises(ProviderError) as ei:
            call()
        assert ei.value.code == "not_implemented" and ei.value.billed == "no"
    assert FalProvider().status()["key_stored"] is False


def test_test_key_gets_the_model_for_free():
    spy = Spy(json_response({"name": "models/gemini-3.8-flash"}))
    r = provider(spy).test_key()
    assert r["ok"] is True and spy.requests[0].method == "GET" and str(spy.requests[0].url).endswith("/v1beta/models/gemini-3.8-flash")
    bad = provider(Spy(gerr(400, "API key not valid", "INVALID_ARGUMENT"))).test_key()
    assert bad["ok"] is False and bad["kind"] == "auth"
    assert provider(Spy(httpx.ReadTimeout("t"))).test_key()["kind"] == "timeout"
