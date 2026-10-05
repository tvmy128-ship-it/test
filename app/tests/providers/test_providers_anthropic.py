"""Claude adapter: request shape, stop_reason handling, served-model logging, errors, batches (APP_SPEC 7.2)."""
from __future__ import annotations

import io
import json

import pytest
from prov_helpers import (
    Spy,
    anthropic_error,
    anthropic_stream,
    json_response,
    make_anthropic_client,
    png_bytes,
)
from pydantic import BaseModel

from duoskin.providers import anthropic_llm as A
from duoskin.providers.base import CallCtx, Cancelled, CapabilityFlags, ProviderError, RateLimiter


class Plan(BaseModel):
    title: str
    items: list[str]
    n: int


GOOD = json.dumps({"title": "t", "items": ["a", "b"], "n": 2})
SYS = [{"type": "text", "text": "You help."}]
CONTENT = [{"type": "text", "text": "<brief>go</brief>"}]


def provider(spy: Spy, **kw) -> A.AnthropicProvider:
    client = make_anthropic_client(spy)
    return A.AnthropicProvider(client=client, limiter=RateLimiter(concurrent=2), **kw)


def call(p: A.AnthropicProvider, route="L3_planner", **kw):
    return p.call(route, system=SYS, content=CONTENT, out=Plan, ctx=CallCtx.null(), prompt_version=3, **kw)


def test_success_opus_uses_beta_fallbacks_and_logs_models():
    spy = Spy(anthropic_stream(text=GOOD, thinking="reasoning...", model="claude-opus-4-8"))
    r = call(provider(spy))
    spy.assert_hit(1)
    assert r.parsed == Plan(title="t", items=["a", "b"], n=2)
    assert r.stop_reason == "end_turn" and r.raw_text == GOOD
    assert r.requested_model == "claude-opus-5" and r.served_model == "claude-opus-4-8" and r.fallback_used
    assert r.thinking_summary == "reasoning..."
    assert r.request_id == "req_test_1" and r.prompt_version == 3 and len(r.schema_hash) == 64
    assert set(r.usage) >= {"input", "output", "cache_read_input_tokens", "cache_creation_input_tokens"}
    assert r.cost["provider"] == "anthropic" and r.cost["basis"] == "usage" and r.cost["usd"] > 0
    req, body = spy.requests[0], spy.bodies[0]
    assert req.headers["anthropic-beta"] == A.FALLBACK_BETA
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["stream"] is True and body["max_tokens"] == 64000


def test_sonnet_route_uses_plain_stream_without_fallbacks():
    spy = Spy(anthropic_stream(text=GOOD, model="claude-sonnet-5"))
    r = call(provider(spy), route="L11_checker")
    spy.assert_hit(1)
    assert "anthropic-beta" not in spy.requests[0].headers
    assert "fallbacks" not in spy.bodies[0]
    assert spy.bodies[0]["model"] == "claude-sonnet-5" and spy.bodies[0]["output_config"]["effort"] == "medium"
    assert not r.fallback_used


def test_sonnet_fallbacks_enabled_by_flag():
    spy = Spy(anthropic_stream(text=GOOD, model="claude-sonnet-5"))
    flags = CapabilityFlags({"anthropic.sonnet_fallbacks": True})
    call(provider(spy, flags=flags), route="L11_checker")
    assert spy.bodies[0]["fallbacks"] == "default"


@pytest.mark.parametrize("key", ["temperature", "top_p", "top_k", "budget_tokens"])
def test_request_never_contains_sampling_params_or_prefill(key):
    spy = Spy(anthropic_stream(text=GOOD))
    p = provider(spy)
    call(p)
    body = spy.bodies[0]
    assert key not in body and key not in body["thinking"]
    assert body["messages"][-1]["role"] == "user"
    # the pre-flight rejects them if some caller tries
    kw = dict(p.last_request)
    kw[key] = 1
    with pytest.raises(ProviderError) as ei:
        A.AnthropicProvider.preflight(kw, CONTENT)
    assert ei.value.code == "forbidden_param"


def test_preflight_rejects_prefill_and_thinking_disabled_with_xhigh():
    base = {"model": "m", "max_tokens": 5, "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
            "system": SYS, "messages": [{"role": "user", "content": CONTENT}]}
    A.AnthropicProvider.preflight(base, CONTENT)
    bad = {**base, "messages": [*base["messages"], {"role": "assistant", "content": "{"}]}
    with pytest.raises(ProviderError):
        A.AnthropicProvider.preflight(bad, CONTENT)
    bad2 = {**base, "thinking": {"type": "disabled"}, "output_config": {"effort": "xhigh"}}
    with pytest.raises(ProviderError):
        A.AnthropicProvider.preflight(bad2, CONTENT)


def test_refusal_is_not_retryable_and_carries_cost():
    spy = Spy(anthropic_stream(text=None, stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"}))
    with pytest.raises(ProviderError) as ei:
        call(provider(spy))
    spy.assert_hit(1)
    e = ei.value
    assert e.kind == "refusal" and e.group == "refusal" and not e.retryable and e.billed == "yes"
    assert e.cost and e.cost["usd"] > 0 and "declined" in e.user_message.lower()


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded"])
def test_truncation_is_retryable(stop):
    spy = Spy(anthropic_stream(text='{"title": "x', stop_reason=stop))
    with pytest.raises(ProviderError) as ei:
        call(provider(spy))
    assert ei.value.kind == "truncated" and ei.value.retryable and ei.value.code == stop and ei.value.cost


def test_missing_text_block_is_truncated():
    spy = Spy(anthropic_stream(text=None, thinking="only thinking"))
    with pytest.raises(ProviderError) as ei:
        call(provider(spy))
    assert ei.value.kind == "truncated" and ei.value.code == "no_text"


def test_schema_violating_json_is_validation_error_with_raw_text():
    spy = Spy(anthropic_stream(text=json.dumps({"title": 1, "items": "no"})))
    with pytest.raises(ProviderError) as ei:
        call(provider(spy))
    e = ei.value
    assert e.kind == "validation" and e.billed == "yes" and e.cost
    assert e.context["raw_text"].startswith("{") and isinstance(e.context["errors"], list)


def test_cost_sink_receives_cost_even_on_refusal():
    seen = []
    spy = Spy(anthropic_stream(text=None, stop_reason="refusal"))
    p = provider(spy, cost_sink=seen.append)
    with pytest.raises(ProviderError):
        call(p)
    assert len(seen) == 1 and seen[0]["operation"] == "messages.stream:L3_planner"


def test_with_pins_changes_models():
    spy = Spy(anthropic_stream(text=GOOD, model="claude-opus-5-2"))

    class Pins:
        planner = "claude-opus-5-2"
        critic = "claude-opus-5-2"
        judge = "claude-opus-5-2"
        checker = "claude-sonnet-5-2"

    call(provider(spy).with_pins(Pins()))
    assert spy.bodies[0]["model"] == "claude-opus-5-2"


def test_max_tokens_override_for_truncated_retry():
    spy = Spy(anthropic_stream(text=GOOD))
    call(provider(spy), max_tokens_override=128000)
    assert spy.bodies[0]["max_tokens"] == 128000


@pytest.mark.parametrize(("status", "etype", "msg", "kind", "retryable"), [
    (401, "authentication_error", "invalid x-api-key", "auth", False),
    (403, "permission_error", "no access", "permission", False),
    (404, "not_found_error", "model: claude-x", "not_found", False),
    (400, "invalid_request_error", "Your credit balance is too low to access the API", "billing", False),
    (402, "billing_error", "payment required", "billing", False),
    (400, "invalid_request_error", "Schema is too complex for compilation", "schema_too_complex", False),
    (400, "invalid_request_error", "messages: bad", "bad_request", False),
    (413, "request_too_large", "too big", "bad_request", False),
    (429, "rate_limit_error", "slow down", "rate_limit", True),
    (529, "overloaded_error", "Overloaded", "overloaded", True),
    (500, "api_error", "boom", "server", True),
])
def test_http_errors_map_to_kinds(status, etype, msg, kind, retryable):
    spy = Spy(anthropic_error(status, msg, etype, headers={"retry-after": "7"} if status == 429 else None))
    flags = CapabilityFlags()
    p = provider(spy, flags=flags)
    with pytest.raises(ProviderError) as ei:
        call(p)
    spy.assert_hit(1)
    e = ei.value
    assert e.kind == kind and e.retryable is retryable and e.http == status and e.request_id == "req_err_1"
    assert e.user_message
    if status == 429:
        assert e.retry_after_s == 7.0
    if kind == "schema_too_complex":
        assert flags.get("anthropic.schema_ok.L3_planner") is False


def test_429_penalizes_the_shared_limiter():
    spy = Spy(anthropic_error(429, "slow", "rate_limit_error", headers={"retry-after": "30"}))
    lim = RateLimiter(concurrent=1)
    p = A.AnthropicProvider(client=make_anthropic_client(spy), limiter=lim)
    with pytest.raises(ProviderError):
        call(p)
    assert lim._blocked_until > 0


def test_cancel_during_stream_propagates():
    spy = Spy(anthropic_stream(text=GOOD, thinking="x"))

    def cancel():
        raise Cancelled()

    ctx = CallCtx(check_cancel=cancel)
    with pytest.raises(Cancelled):
        provider(spy).call("L3_planner", system=SYS, content=CONTENT, out=Plan, ctx=ctx, prompt_version=1)


def test_heartbeat_called_per_event():
    spy = Spy(anthropic_stream(text=GOOD, thinking="x"))
    beats = []
    ctx = CallCtx(heartbeat=lambda: beats.append(1))
    provider(spy).call("L3_planner", system=SYS, content=CONTENT, out=Plan, ctx=ctx, prompt_version=1)
    assert len(beats) >= 5


def test_cache_monitor_alerts_on_second_call_without_cache_read():
    spy = Spy(anthropic_stream(text=GOOD), anthropic_stream(text=GOOD), anthropic_stream(text=GOOD, usage={"cache_read_input_tokens": 900}))
    p = provider(spy)
    call(p)
    call(p)
    assert [a["check_id"] for a in p.monitor.alerts] == ["CHK-X04"]
    call(p)
    assert len(p.monitor.alerts) == 1      # a cache read: no new alert


def test_fanout_sends_first_then_the_rest():
    order = []

    def mk(i):
        def handler(request):
            order.append(i)
            return anthropic_stream(text=GOOD)
        return handler

    spy = Spy(mk(0), mk(1), mk(2))
    p = provider(spy)
    reqs = [{"system": SYS, "content": CONTENT, "out": Plan, "prompt_version": 1}] * 3
    res = p.call_fanout("L11_checker", reqs, ctx=CallCtx.null())
    spy.assert_hit(3)
    assert all(isinstance(r, A.LLMResult) for r in res) and order[0] == 0


def test_fanout_returns_errors_in_place():
    spy = Spy(anthropic_stream(text=GOOD), anthropic_error(400, "bad"), anthropic_stream(text=GOOD))
    res = provider(spy).call_fanout("L11_checker", [{"system": SYS, "content": CONTENT, "out": Plan, "prompt_version": 1}] * 3,
                                     ctx=CallCtx.null(), max_workers=1)
    assert isinstance(res[0], A.LLMResult) and isinstance(res[1], ProviderError) and isinstance(res[2], A.LLMResult)


# ----- schemas -------------------------------------------------------------------------------------------------

class Inner(BaseModel):
    a: int
    b: str = "x"


class Outer(BaseModel):
    inner: Inner
    inners: list[Inner]
    opt: int | None = None


def test_schema_all_required_and_no_unions_after_sentinels():
    schema, h = A.SCHEMA_CACHE.get(Inner)
    assert schema["required"] == ["a", "b"] and schema["additionalProperties"] is False and len(h) == 64
    assert A.count_unions_and_optionals(schema) == {"anyOf": 0, "optional": 0}
    full, _ = A.SCHEMA_CACHE.get(Outer)
    # make_all_required makes even the defaulted/optional fields required and recurses into $defs
    assert set(full["required"]) == {"inner", "inners", "opt"}
    assert full["$defs"]["Inner"]["required"] == ["a", "b"]


def test_schema_cache_is_built_once_and_invalidated_by_version():
    c = A.SchemaCache()
    s1 = c.get(Inner)[0]
    assert c.get(Inner)[0] is s1
    c.set_version("kit-sha-1")
    assert c.get(Inner)[0] is not s1


def test_make_all_required_does_not_mutate_input():
    raw = {"type": "object", "properties": {"x": {"type": "object", "properties": {"y": {"type": "integer"}}}}}
    out = A.make_all_required(raw)
    assert "required" not in raw and out["required"] == ["x"] and out["properties"]["x"]["required"] == ["y"]


# ----- images to Claude ------------------------------------------------------------------------------------------

def test_prepare_judge_images_gives_grey_and_checker_of_legal_size():
    out = A.prepare_judge_images(png_bytes(40, 30, (255, 0, 0, 0)))
    assert set(out) == {"grey", "checker"}
    from PIL import Image
    g = Image.open(io.BytesIO(out["grey"]))
    assert min(g.size) >= 256 and g.getpixel((5, 5)) == (128, 128, 128)
    c = Image.open(io.BytesIO(out["checker"]))
    assert c.getpixel((0, 0)) != c.getpixel((8, 0))      # an 8 px checkerboard
    big = A.prepare_judge_images(png_bytes(3000, 3000, (1, 2, 3, 255)))
    assert max(Image.open(io.BytesIO(big["grey"])).size) <= 2576
    many = A.prepare_judge_images(png_bytes(3000, 3000, (1, 2, 3, 255)), many_images=True)
    assert max(Image.open(io.BytesIO(many["grey"])).size) <= 2000


def test_content_images_checked_before_sending():
    A.check_content_images([A.image_block(png_bytes(256, 256))])
    with pytest.raises(ProviderError) as ei:
        A.check_content_images([A.image_block(png_bytes(100, 300))])
    assert ei.value.code == "image_too_small"
    with pytest.raises(ProviderError):
        A.image_block(b"\xff\xd8not a png")
    with pytest.raises(ProviderError) as ei2:
        A.check_content_images([A.image_block(png_bytes(2600, 300))])
    assert ei2.value.code == "image_too_large"


def test_with_cache_control_sets_ttl_on_last_block():
    out = A.with_cache_control([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], ttl="1h")
    assert "cache_control" not in out[0] and out[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert A.with_cache_control(SYS)[0]["cache_control"] == {"type": "ephemeral"}


def test_file_source_adds_files_beta():
    spy = Spy(anthropic_stream(text=GOOD, model="claude-sonnet-5"))
    content = [A.file_image_block("file_abc"), *CONTENT]
    provider(spy).call("L11_checker", system=SYS, content=content, out=Plan, ctx=CallCtx.null(), prompt_version=1)
    assert A.FILES_BETA in spy.requests[0].headers["anthropic-beta"]


# ----- files, batches, capabilities, smoke ---------------------------------------------------------------------

def test_files_upload_and_delete():
    spy = Spy(json_response({"id": "file_1", "type": "file", "filename": "a.png", "mime_type": "image/png", "size_bytes": 3,
                             "created_at": "2026-01-01T00:00:00Z", "downloadable": False}),
              json_response({"id": "file_1", "type": "file_deleted"}))
    p = provider(spy)
    assert p.upload_file(b"abc", name="a.png", mime="image/png") == "file_1"
    p.delete_file("file_1")
    spy.assert_hit(2)
    assert spy.requests[0].url.path == "/v1/files" and spy.requests[1].method == "DELETE"


def _batch(status="ended", results_url=None, **counts):
    c = {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    c.update(counts)
    return json_response({"id": "msgbatch_1", "type": "message_batch", "processing_status": status, "request_counts": c,
                          "created_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-02T00:00:00Z", "ended_at": None,
                          "archived_at": None, "cancel_initiated_at": None, "results_url": results_url})


def test_batch_submit_has_no_fallbacks_and_results_are_validated():
    from prov_helpers import httpx

    def result_line(cid, msg):
        return json.dumps({"custom_id": cid, "result": {"type": "succeeded", "message": msg}})

    def msg(text, stop="end_turn", model="claude-sonnet-5"):
        return {"id": "m", "type": "message", "role": "assistant", "model": model, "stop_reason": stop, "stop_sequence": None,
                "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 10, "output_tokens": 5,
                                                                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}

    lines = "\n".join([result_line("ok", msg(GOOD)), result_line("cut", msg("{", "max_tokens")),
                       result_line("refused", msg("", "refusal")), result_line("bad", msg('{"title": 5}')),
                       json.dumps({"custom_id": "err", "result": {"type": "errored", "error": {"type": "error", "error": {"type": "api_error", "message": "boom"}}}}),
                       json.dumps({"custom_id": "exp", "result": {"type": "expired"}})])
    url = "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results"
    spy = Spy(_batch("in_progress", processing=6),                               # create()
              _batch("in_progress", processing=6), _batch("ended", succeeded=4, errored=1, expired=1),   # status x2
              _batch("ended", url, succeeded=4, errored=1, expired=1),           # results(): retrieve first
              httpx.Response(200, content=lines.encode(), headers={"content-type": "application/binary"}))
    p = provider(spy)
    items = [A.BatchItem(c, "L11_checker", SYS, CONTENT, Plan) for c in ("ok", "cut", "refused", "bad", "err", "exp")]
    bid = p.batch_submit(items)
    assert bid == "msgbatch_1"
    sent = spy.bodies[0]["requests"]
    assert len(sent) == 6 and all("fallbacks" not in r["params"] and "stream" not in r["params"] for r in sent)
    assert sent[0]["params"]["output_config"]["format"]["type"] == "json_schema"
    assert p.batch_status(bid) == "in_progress"
    assert p.batch_status(bid) == "ended"
    res = {r.custom_id: r for r in p.batch_results(bid)}
    assert res["ok"].status == "succeeded" and res["ok"].parsed.n == 2 and not res["ok"].rerun and res["ok"].cost["batch"] is True
    assert res["cut"].status == "truncated" and res["cut"].rerun
    assert res["refused"].status == "refused" and res["refused"].rerun
    assert res["bad"].status == "invalid" and res["bad"].rerun
    assert res["err"].status == "errored" and res["err"].rerun
    assert res["exp"].status == "expired" and res["exp"].rerun


def test_batch_status_all_expired_and_all_canceled():
    spy = Spy(_batch("ended", expired=3), _batch("ended", canceled=2))
    p = provider(spy)
    assert p.batch_status("b") == "expired"
    assert p.batch_status("b") == "canceled"


def test_capabilities_and_allowed_fallbacks_set_flag():
    model_info = {"id": "claude-opus-5", "type": "model", "display_name": "Opus 5", "created_at": "2026-01-01T00:00:00Z",
                  "max_input_tokens": 1000000, "max_tokens": 128000, "capabilities": None}
    spy = Spy(json_response(model_info), json_response({**model_info, "id": "claude-sonnet-5"}),
              json_response({**model_info, "id": "claude-sonnet-5", "allowed_fallback_models": ["claude-opus-4-8"]}),
              json_response({**model_info, "id": "claude-sonnet-5", "allowed_fallback_models": []}))
    flags = CapabilityFlags()
    p = provider(spy, flags=flags)
    caps = p.capabilities()
    assert set(caps) == {"claude-opus-5", "claude-sonnet-5"}
    assert p.allowed_fallbacks("claude-sonnet-5") == ["claude-opus-4-8"] and flags.get("anthropic.sonnet_fallbacks") is True
    assert p.allowed_fallbacks("claude-sonnet-5") == [] and flags.get("anthropic.sonnet_fallbacks") is False
    assert spy.requests[2].headers["anthropic-beta"] == A.FALLBACK_MODELS_BETA


def test_smoke_test_passes_and_fails_closed():
    spy = Spy(anthropic_stream(text=GOOD, model="claude-sonnet-5"))
    flags = CapabilityFlags()
    p = provider(spy, flags=flags)
    r = p.smoke_test("L11_checker", Plan)
    assert r.passed and r.ran and r.check_id == "CHK-S10" and flags.get("anthropic.schema_ok.L11_checker") is True
    assert spy.bodies[0]["thinking"] == {"type": "disabled"} and spy.bodies[0]["max_tokens"] <= 2048
    spy2 = Spy(anthropic_error(400, "Schema is too complex for compilation"))
    r2 = provider(spy2, flags=flags).smoke_test("L11_checker", Plan)
    assert not r2.passed and r2.ran and flags.get("anthropic.schema_ok.L11_checker") is False
    # an unexpected exception inside the check: ran=False, passed=False
    r3 = provider(Spy(anthropic_stream(text=GOOD))).smoke_test("no_such_route", Plan)
    assert not r3.ran and not r3.passed


def test_repr_never_contains_the_key():
    p = provider(Spy())
    assert "sk-ant" not in repr(p)


# ----- key test and startup probe -------------------------------------------------------------------------------------

def test_test_key_and_startup_probe_are_free_calls():
    info = {"id": "claude-opus-5", "type": "model", "display_name": "Opus 5", "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1000000,
            "max_tokens": 128000, "capabilities": None}
    spy = Spy(json_response(info))
    r = provider(spy).test_key()
    assert r["ok"] is True and "claude-opus-5" in r["message"] and spy.requests[0].url.path == "/v1/models/claude-opus-5"
    bad = Spy(anthropic_error(401, "invalid x-api-key", "authentication_error"))
    r2 = provider(bad).test_key()
    assert r2["ok"] is False and r2["kind"] == "auth" and "key" in r2["message"].lower()
    spy3 = Spy(json_response(info), json_response({**info, "id": "claude-sonnet-5"}), json_response({**info, "allowed_fallback_models": ["claude-opus-4-8"]}),
               json_response({**info, "id": "claude-sonnet-5", "allowed_fallback_models": []}))
    probe = provider(spy3).startup_probe()
    assert probe["ok"] is True and set(probe["allowed_fallbacks"]) == {"claude-opus-5", "claude-sonnet-5"}
    spy4 = Spy(anthropic_error(404, "model: claude-opus-5", "not_found_error"), json_response({**info, "id": "claude-sonnet-5"}), anthropic_error(404, "x", "not_found_error"),
               anthropic_error(404, "x", "not_found_error"))
    p4 = provider(spy4).startup_probe()
    assert p4["ok"] is False and "not_found" in p4["message"]
