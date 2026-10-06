"""Offline "first paid call" checks: every request the provider adapters build is validated against ground truth, with no key.

Ground truth, strongest first:

* **installed SDK types** (``anthropic`` 1.11.0, ``openai`` 3.24.0): the captured WIRE body of a real SDK client running on an
  ``httpx`` MockTransport is walked against the SDK's own request TypedDicts (``shape_check``), so an unknown key, a misspelt field or a
  wrong nesting fails here instead of on the first paid call;
* **bundled claude-api docs** (``shared/models.md``, ``shared/model-migration.md``): model ids, the ``fallbacks`` beta header, thinking rules;
* **official google-genai 2.11.0 source** (``ref/google_genai_rest_keys.json``, extracted from ``types.py``);
* **third-party only** (labelled UNVERIFIED in the adapters): Tripo (``ref/tripo_v3_request_schemas.json``, a locally maintained OpenAPI of
  the vendor docs) and Recraft (Recraft MCP client + ComfyUI node + API Evangelist profile; the key sets are listed inline below).
"""
from __future__ import annotations

import io
import json
import pathlib
import re
from typing import Any

import numpy as np
import pytest
import typing_extensions as te
from PIL import Image
from prov_helpers import (
    FakeClock,
    Spy,
    anthropic_stream,
    json_response,
    make_anthropic_client,
    make_openai_client,
    noop_sleep,
    openai_images_response,
    png_bytes,
)
from pydantic import BaseModel
from shape_check import check, known_keys

from duoskin.providers import anthropic_llm as A
from duoskin.providers import gemini as G
from duoskin.providers import openai_images as O
from duoskin.providers import recraft as R
from duoskin.providers import tripo as T
from duoskin.providers._http import httpx
from duoskin.providers.base import CallCtx, CapabilityFlags, ProviderError, RateLimiter

REF = pathlib.Path(__file__).resolve().parent / "ref"
CTX = CallCtx.null()


def _ref(name: str) -> Any:
    return json.loads((REF / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------------
# 1. Claude
# ---------------------------------------------------------------------------------------------------------------------

#: exact ids of the bundled docs ``shared/models.md`` ("Only use exact model IDs listed in this file"); the 5.x aliases have no dated id
DOC_MODEL_IDS = {"claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5", "claude-opus-5-5", "claude-opus-5",
                 "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
                 "claude-haiku-4-5", "claude-haiku-4-5-20251001"}
FORBIDDEN_BODY_KEYS = ("temperature", "top_p", "top_k", "tool_choice", "tools", "stop_sequences", "service_tier", "metadata", "output_format")
#: JSON-schema keywords the structured-outputs API rejects (the SDK's ``transform_schema`` is supposed to strip or move them)
UNSUPPORTED_SCHEMA_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
                               "maxItems", "uniqueItems", "patternProperties", "oneOf", "not", "if", "then", "else", "dependentSchemas"}


def _sdk_params(beta: bool) -> type:
    if beta:
        from anthropic.types.beta import message_create_params as bm
        return bm.MessageCreateParamsStreaming
    from anthropic.types import message_create_params as m
    return m.MessageCreateParamsStreaming


def _wire(spy: Spy) -> tuple[Any, dict[str, Any], bool]:
    req, body = spy.requests[0], spy.bodies[0]
    return req, body, req.url.params.get("beta") == "true"


def _provider(spy: Spy, **kw: Any) -> A.AnthropicProvider:
    return A.AnthropicProvider(client=make_anthropic_client(spy), limiter=RateLimiter(concurrent=2), **kw)


def _fake_inputs(meta: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, decl in meta.inputs.items():
        if decl.required:
            out[name] = True if decl.kind == "flag" else (decl.values[0] if decl.values else ("1" if decl.kind == "int" else "x"))
    return out


def _llm_templates() -> list[str]:
    from duoskin.prompts import registry
    return [t for t in registry.template_ids("llm") if registry.get(t).meta.provider == "anthropic"]


class _Any(BaseModel):
    model_config = {"extra": "allow"}


class _Plan(BaseModel):
    title: str
    n: int


def _call_role(template_id: str) -> tuple[Spy, Any]:
    """Compile a real role template and send it through the real SDK client; the (empty) answer is rejected, the request is kept."""
    from duoskin.pipeline.plan import route_of
    from duoskin.prompts import registry
    from duoskin.prompts.llm import compile_llm, schema_class

    meta = registry.get(template_id).meta
    prompt = compile_llm(template_id, _fake_inputs(meta))
    content = [A.image_block(png_bytes(256, 256)), A.text_block(prompt.user_text)]
    spy = Spy(anthropic_stream(text="{}"))
    try:
        _provider(spy).call(route_of(template_id), system=prompt.system, content=content, out=schema_class(prompt), ctx=CTX,
                            prompt_version=prompt.template_version)
    except ProviderError:
        pass                                                # "{}" does not satisfy a role schema; only the request is under test
    spy.assert_hit(1)
    return spy, meta


@pytest.mark.parametrize("template_id", _llm_templates())
def test_every_claude_role_request_fits_the_sdk_request_types(template_id):
    """Planner (L3), critic (L4), ranker (L5), reviser (L6), change interpreter (L7), per-asset yes/no check (L11) and every other
    Claude role: the wire body only has parameters the installed SDK declares, with legal values, and obeys the documented rules."""
    from duoskin.pipeline.plan import route_of

    spy, meta = _call_role(template_id)
    req, body, beta = _wire(spy)
    problems = check(body, _sdk_params(beta))
    assert not problems, f"{template_id}: {problems}"
    assert {"betas", "user_profile_id", "workspace_id"}.isdisjoint(body)                  # header-only params never ride in the body
    # the adapter's route table and the template front matter (what the prompt was written for) agree
    route = A.ROUTES[route_of(template_id)]
    assert (body["model"], body["output_config"]["effort"], body["max_tokens"]) == (
        meta.route["model"], meta.route["effort"], meta.route["max_tokens"])
    assert beta == (meta.route["fallbacks"] == "default") == route.fallbacks
    # documented rules for the newest models
    assert not set(FORBIDDEN_BODY_KEYS) & body.keys()
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}                # no budget_tokens, no "enabled"
    assert body["messages"][-1]["role"] == "user"                                          # no assistant prefill
    assert body["stream"] is True and 0 < body["max_tokens"] <= 128_000                    # big max_tokens requires streaming
    assert body["model"] in DOC_MODEL_IDS
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"] and all(b["type"] == "text" for b in body["system"])
    assert "cache_control" in body["system"][-1] and sum("cache_control" in b for b in body["system"]) <= 4
    if beta:
        # docs: `fallbacks: "default"` pairs with server-side-fallback-2026-07-01; the array form needs -2026-06-01; mixing them is a 400
        assert body["fallbacks"] == "default"
        assert req.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    else:
        assert "fallbacks" not in body and "anthropic-beta" not in req.headers
        assert req.url.path == "/v1/messages"


def test_the_six_named_roles_use_the_documented_models_and_efforts():
    from anthropic.types.output_config_param import OutputConfigParam

    table = {"L3_planner": ("claude-opus-5", "high", True), "L4_critic": ("claude-opus-5", "medium", True),
             "L5_pairwise": ("claude-opus-5", "medium", True), "L6_reviser": ("claude-opus-5", "medium", True),
             "L7_change": ("claude-opus-5", "medium", True), "L11_checker": ("claude-sonnet-5", "medium", False)}
    for route, (model, effort, fallbacks) in table.items():
        cfg = A.ROUTES[route]
        assert (cfg.model, cfg.effort, cfg.fallbacks) == (model, effort, fallbacks), route
        assert not check({"effort": cfg.effort}, OutputConfigParam)


def test_default_model_pins_are_exact_ids_from_the_vendor_tables():
    from openai.types.image_model import ImageModel

    from duoskin.models.settings import ModelPins

    pins = ModelPins()
    for role in ("planner", "critic", "judge", "checker"):
        assert getattr(pins, role) in DOC_MODEL_IDS, role
    for role in ("image_draft", "image_final"):
        assert getattr(pins, role) in te.get_args(ImageModel), role
    assert pins.tripo_mesh == T.P2 and pins.tripo_fallback_p1 == T.P1 and pins.tripo_fallback_h31 == T.H31
    assert {pins.recraft_face, pins.recraft_bootstrap, pins.recraft_print} <= set(R.VECTOR_MODELS)
    assert pins.gemini_judge == G.JUDGE_MODEL and pins.gemini_image == G.IMAGE_MODEL


def test_there_is_no_forced_tool_choice_or_prefill_path():
    """Opus 5.5 / Sonnet 5.5 / Fable return 400 for forced ``tool_choice``; the adapter has no tool path at all."""
    p = A.AnthropicProvider(client=make_anthropic_client(Spy()), limiter=RateLimiter(concurrent=1))
    kw = p.build_request("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], schema={"type": "object"})
    assert "tool_choice" not in kw and "tools" not in kw
    with pytest.raises(ProviderError):
        p.preflight({**kw, "messages": [*kw["messages"], {"role": "assistant", "content": "{"}]})


def _schema_problems(node: Any, path: str = "$") -> list[str]:
    out: list[str] = []
    if isinstance(node, list):
        for i, v in enumerate(node):
            out += _schema_problems(v, f"{path}[{i}]")
    elif isinstance(node, dict):
        if "properties" in node and node.get("additionalProperties") is not False:
            out.append(f"{path}: object without additionalProperties=false")
        if node.get("minItems", 0) not in (0, 1):
            out.append(f"{path}: minItems={node['minItems']}")
        for k, v in node.items():
            if k in ("properties", "$defs", "definitions") and isinstance(v, dict):         # keys are names, not keywords
                for name, sub in v.items():
                    out += _schema_problems(sub, f"{path}.{k}.{name}")
            elif k in ("enum", "required", "const", "default", "examples"):
                continue
            else:
                if k in UNSUPPORTED_SCHEMA_KEYWORDS:
                    out.append(f"{path}: {k}")
                out += _schema_problems(v, f"{path}.{k}")
    return out


def test_structured_output_schemas_avoid_keywords_the_api_rejects():
    """Structured outputs reject numeric / length / count constraints and non-closed objects; ``transform_schema`` moves them into the
    description. A leftover would be a 400 'schema not supported' on the first call of that route (CHK-S10 only runs with a key)."""
    from duoskin.prompts import registry
    from duoskin.prompts.llm import compile_llm, schema_class

    bad: dict[str, list[str]] = {}
    for tid in _llm_templates():
        schema, _h = A.SCHEMA_CACHE.get(schema_class(compile_llm(tid, _fake_inputs(registry.get(tid).meta))))
        problems = _schema_problems(schema)
        if problems:
            bad[tid] = problems[:5]
    assert not bad, bad


def test_smoke_route_thinking_rule_per_model():
    """``thinking: disabled`` is a 400 on Opus 5.5 / Sonnet 5.5 / Fable (docs): there the smoke test sends adaptive at effort low."""
    assert A.thinking_config("claude-sonnet-5", False) == {"type": "disabled"}
    assert A.thinking_config("claude-opus-5", False) == {"type": "disabled"}
    assert A.thinking_config("claude-opus-5", True) == {"type": "adaptive", "display": "summarized"}
    for m in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5"):
        assert A.thinking_config(m, False) == {"type": "adaptive", "display": "omitted"}, m
    for m in ("claude-sonnet-5-5", "claude-sonnet-5"):
        spy = Spy(anthropic_stream(text="{}", model=m))
        _provider(spy, routes=A.build_routes(checker=m, planner=m, critic=m, judge=m)).smoke_test("L11_checker", _Any)
        spy.assert_hit(1)
        body = spy.bodies[0]
        assert not check(body, _sdk_params(False)), m
        assert body["output_config"]["effort"] == "low" and body["max_tokens"] == 2048 and "fallbacks" not in body
        assert body["thinking"]["type"] == ("adaptive" if m.endswith("5-5") else "disabled")


def test_batch_params_fit_the_sdk_batch_request_type():
    from anthropic.types.messages import batch_create_params as bc

    batch = {"id": "msgbatch_1", "type": "message_batch", "processing_status": "in_progress",
             "request_counts": {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
             "created_at": "2026-10-01T00:00:00Z", "expires_at": "2026-10-02T00:00:00Z", "ended_at": None,
             "cancel_initiated_at": None, "results_url": None, "archived_at": None}
    spy = Spy(json_response(batch))
    _provider(spy).batch_submit([A.BatchItem("c1", "L11_checker", [A.text_block("sys")], [A.text_block("hi")], _Plan)])
    spy.assert_hit(1)
    body = spy.bodies[0]
    assert spy.requests[0].url.path == "/v1/messages/batches" and body.keys() == {"requests"}
    for r in body["requests"]:
        assert not check(r, bc.Request), check(r, bc.Request)
        assert "fallbacks" not in r["params"] and "stream" not in r["params"]          # docs: fallbacks are rejected on the Batches API


def test_image_blocks_and_cache_control_fit_the_sdk_types():
    spy = Spy(anthropic_stream(text="{}"))
    content = [*[A.image_block(png_bytes(256, 256)) for _ in range(2)], A.text_block("rules")]
    system = A.with_cache_control([A.text_block("a"), A.text_block("b")], ttl="1h")
    with pytest.raises(ProviderError):
        _provider(spy).call("L11_checker", system=system, content=content, out=_Plan, ctx=CTX, prompt_version=1)
    body = spy.bodies[0]
    assert not check(body, _sdk_params(False))
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["messages"][0]["content"][0]["source"] == {"type": "base64", "media_type": "image/png", "data": body["messages"][0]["content"][0]["source"]["data"]}


def test_file_image_source_fits_the_beta_request_type_and_sends_the_files_header():
    spy = Spy(anthropic_stream(text="{}"))
    content = [A.file_image_block("file_011CNha8iCJcU1wXNR6q4V8w"), A.text_block("rules")]
    with pytest.raises(ProviderError):
        _provider(spy).call("L11_checker", system=[A.text_block("s")], content=content, out=_Plan, ctx=CTX, prompt_version=1)
    req, body, beta = _wire(spy)
    assert beta and not check(body, _sdk_params(True))
    assert "files-api-2025-04-14" in req.headers["anthropic-beta"]


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _fallback_stream(*, partial: str, final: str, iterations: list[dict[str, Any]], model: str = "claude-opus-5") -> Any:
    """A beta stream in which the requested model declined mid-stream: partial text, a ``fallback`` block, the fallback's own text."""
    start = {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    fb = {"type": "fallback", "from": {"model": model}, "to": {"model": "claude-opus-4-8"}, "trigger": {"type": "refusal", "category": "cyber"}}

    def text_block(i: int, text: str) -> list[tuple[str, dict[str, Any]]]:
        return [("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text}}),
                ("content_block_stop", {"type": "content_block_stop", "index": i})]

    ev = [("message_start", {"type": "message_start", "message": start}), *text_block(0, partial),
          ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": fb}),
          ("content_block_stop", {"type": "content_block_stop", "index": 1}), *text_block(2, final),
          ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                             "usage": {"output_tokens": 80, "input_tokens": 100, "iterations": iterations}}),
          ("message_stop", {"type": "message_stop"})]
    return httpx.Response(200, content=_sse(ev), headers={"content-type": "text/event-stream", "request-id": "req_fb"})


_ITER = {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def test_mid_stream_fallback_answer_is_the_text_after_the_fallback_block_and_every_attempt_is_billed():
    """Docs: a mid-stream decline keeps the declined model's partial text in ``content``, then a ``fallback`` block, then the fallback
    model's blocks; top-level ``usage`` covers only the final attempt and ``usage.iterations`` is the per-attempt bill."""
    good = json.dumps({"title": "t", "n": 2})
    its = [{"type": "message", "model": "claude-opus-5", "input_tokens": 100, "output_tokens": 7, **_ITER},
           {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 80, **_ITER}]
    costs: list[dict[str, Any]] = []
    spy = Spy(_fallback_stream(partial='{"title": "par', final=good, iterations=its))
    r = _provider(spy, cost_sink=costs.append).call("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], out=_Plan,
                                                    ctx=CTX, prompt_version=1)
    assert r.parsed == _Plan(title="t", n=2) and r.served_model == "claude-opus-4-8" and r.fallback_used
    assert r.cost["usd"] == pytest.approx((100 * 5 + 7 * 25 + 100 * 5 + 80 * 25) / 1e6)       # both $5 / $25 models, both attempts
    assert {u["name"]: u["qty"] for u in r.cost["units"]} == {"input_tokens": 200, "output_tokens": 87}
    assert costs and costs[0]["usd"] == r.cost["usd"]


def test_mid_stream_fallback_that_continued_the_partial_is_joined():
    full = json.dumps({"title": "t", "n": 2})
    cut = len('{"title": "t", "n"')
    its = [{"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 80, **_ITER}]
    spy = Spy(_fallback_stream(partial=full[:cut], final=full[cut:], iterations=its))
    r = _provider(spy).call("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], out=_Plan, ctx=CTX, prompt_version=1)
    assert r.parsed == _Plan(title="t", n=2)


def test_answer_text_without_a_fallback_block_is_the_first_text_block():
    class B:
        def __init__(self, type_, text=""):
            self.type, self.text = type_, text
    assert A.answer_text([B("thinking"), B("text", "{}"), B("text", "extra")]) == "{}"
    assert A.answer_text([B("thinking")]) is None


def test_usage_fields_feed_the_cost_with_cache_tokens():
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 2000}
    spy = Spy(anthropic_stream(text=json.dumps({"title": "t", "n": 1}), usage=usage))
    r = _provider(spy).call("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], out=_Plan, ctx=CTX, prompt_version=1)
    assert r.usage["input"] == 1000 and r.usage["cache_read_input_tokens"] == 4000 and r.usage["cache_creation_input_tokens"] == 2000
    assert r.cost["usd"] == pytest.approx((1000 * 5 + 500 * 25 + 4000 * 5 * 0.1 + 2000 * 5 * 1.25) / 1e6)


def test_rejected_fallbacks_parameter_never_blocks_the_call():
    """``fallbacks`` rides a dated beta header: if the API refuses it (billed "no") the same request is resent without it, once, and the
    model is remembered."""
    from prov_helpers import anthropic_error

    good = json.dumps({"title": "t", "n": 2})
    spy = Spy(anthropic_error(400, "fallbacks: Extra inputs are not permitted"), anthropic_stream(text=good), anthropic_stream(text=good))
    flags = CapabilityFlags()
    p = _provider(spy, flags=flags)
    r = p.call("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], out=_Plan, ctx=CTX, prompt_version=1)
    assert r.parsed == _Plan(title="t", n=2)
    assert spy.bodies[0]["fallbacks"] == "default" and "fallbacks" not in spy.bodies[1] and "anthropic-beta" not in spy.requests[1].headers
    assert flags.get("anthropic.fallbacks_off.claude-opus-5") is True
    p.call("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], out=_Plan, ctx=CTX, prompt_version=1)
    assert "fallbacks" not in spy.bodies[2]
    # an unrelated 400 is still an error
    spy2 = Spy(anthropic_error(400, "messages.0.content: invalid image"))
    with pytest.raises(ProviderError) as e:
        _provider(spy2).call("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], out=_Plan, ctx=CTX, prompt_version=1)
    assert e.value.kind == "bad_request"
    spy2.assert_hit(1)


def test_max_tokens_never_exceeds_the_documented_output_limit():
    p = A.AnthropicProvider(client=make_anthropic_client(Spy()), limiter=RateLimiter(concurrent=1))
    kw = p.build_request("L3_planner", system=[A.text_block("s")], content=[A.text_block("u")], schema={"type": "object"},
                         max_tokens_override=256_000)
    assert kw["max_tokens"] == 128_000


def test_per_model_prices_follow_the_vendor_table():
    from duoskin.providers.pricing import claude_cost

    u = {"input": 1_000_000, "output": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert claude_cost("claude-opus-5", u, operation="x")["usd"] == pytest.approx(5 + 25 + 0.5)
    assert claude_cost("claude-sonnet-5", u, operation="x")["usd"] == pytest.approx(2 + 10 + 0.2)
    assert claude_cost("claude-opus-5-5", u, operation="x")["usd"] == pytest.approx(4 + 20 + 0.2)          # cache reads $0.20 / MTok
    assert claude_cost("claude-fable-5-1", u, operation="x")["usd"] == pytest.approx(10 + 50 + 0.25)


# ---------------------------------------------------------------------------------------------------------------------
# 2. OpenAI images
# ---------------------------------------------------------------------------------------------------------------------

def _multipart(req: Any) -> list[tuple[str, str | None]]:
    """(field name, filename) of every part of a multipart request."""
    boundary = re.search(r"boundary=([^;]+)", req.headers["content-type"]).group(1).strip('"').encode()
    out = []
    for part in req.content.split(b"--" + boundary):
        head, _, _body = part.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"(?:; filename="([^"]*)")?', head)
        if m:
            out.append((m.group(1).decode(), m.group(2).decode() if m.group(2) else None))
    return out


def _openai(spy: Spy, **kw: Any) -> O.OpenAIImages:
    ft = FakeClock()
    return O.OpenAIImages(client=make_openai_client(spy), limiter=RateLimiter(concurrent=2, ipm=100, clock=ft.now, sleep=ft.sleep),
                          sleep=noop_sleep, **kw)


def _oai_req(**kw: Any) -> O.ImageRequest:
    d = {"model": "gpt-image-2.5-flare-2026-09-08", "prompt": "A tidy blocky character.", "size": "1024x1024", "quality": "low",
         "background": "opaque", "n": 1, **kw}
    return O.ImageRequest(**d)


def test_openai_generate_body_fits_the_sdk_params():
    from openai.types.image_generate_params import ImageGenerateParamsNonStreaming

    spy = Spy(openai_images_response([png_bytes(1024, 1024)] * 2, size="1024x1024"))
    _openai(spy).generate(_oai_req(background="transparent", n=2, quality="high"), CTX)
    spy.assert_hit(1)
    req, body = spy.requests[0], spy.bodies[0]
    assert req.url.path == "/v1/images/generations" and req.headers["authorization"].startswith("Bearer ")
    assert not check(body, ImageGenerateParamsNonStreaming), check(body, ImageGenerateParamsNonStreaming)
    assert body == {"model": "gpt-image-2.5-flare-2026-09-08", "prompt": "A tidy blocky character.", "size": "1024x1024", "quality": "high",
                    "background": "transparent", "output_format": "png", "n": 2, "user": "duoskin-local"}
    for k in ("response_format", "style", "input_fidelity", "moderation", "output_compression", "stream", "partial_images"):
        assert k not in body                                                            # legacy, ignored or unsupported on these models


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare-2026-09-08", "gpt-image-2.5-sunburst-2026-09-08", "gpt-image-2.5-flare",
                                   "gpt-image-2.5-sunburst", "gpt-image-2", "gpt-image-2-2026-04-21"])
def test_openai_model_ids_are_known_to_the_installed_sdk(model):
    from openai.types.image_model import ImageModel

    assert model in te.get_args(ImageModel)
    O.validate_request(_oai_req(model=model), edit=False)


def test_openai_xhigh_and_max_exist_only_on_the_25_models():
    for q in ("xhigh", "max"):
        O.validate_request(_oai_req(quality=q), edit=False)
        for old in ("gpt-image-2", "gpt-image-2-2026-04-21"):
            with pytest.raises(ProviderError) as e:
                O.validate_request(_oai_req(model=old, quality=q), edit=False)
            assert e.value.code == "quality_unsupported_for_model"
    for q in ("low", "medium", "high"):
        O.validate_request(_oai_req(model="gpt-image-2", quality=q), edit=False)


def _mask(w: int = 1024, h: int = 1024) -> bytes:
    m = np.zeros((h, w, 4), np.uint8)
    m[..., 3] = 255
    m[100:300, 100:300, 3] = 0
    buf = io.BytesIO()
    Image.fromarray(m, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def test_openai_edit_sends_the_image_array_field_the_mask_and_only_sdk_params():
    """Several input images go out as multipart ``image[]`` (the SDK's ``<array>`` path), the mask as ``mask``; every other field is an
    SDK ``images.edit`` parameter."""
    from openai.types.image_edit_params import ImageEditParamsBase

    spy = Spy(openai_images_response([png_bytes(1024, 1024)], size="1024x1024"))
    flags = CapabilityFlags()
    flags.set("openai.mask_multi_ok", True)
    ft = FakeClock()
    p = O.OpenAIImages(client=make_openai_client(spy), flags=flags, sleep=noop_sleep,
                       limiter=RateLimiter(concurrent=2, ipm=100, clock=ft.now, sleep=ft.sleep))
    imgs = tuple(O.NamedPng(f"img{i}.png", png_bytes(1024, 1024, (200, 10 * i, 5, 255))) for i in range(3))
    p.edit(_oai_req(images=imgs, mask=O.NamedPng("mask.png", _mask()), quality="medium"), CTX)
    spy.assert_hit(1)
    req = spy.requests[0]
    assert req.url.path == "/v1/images/edits" and req.headers["content-type"].startswith("multipart/form-data")
    fields = _multipart(req)
    names = [n for n, _f in fields]
    assert names.count("image[]") == 3 and names.count("mask") == 1 and "image" not in names
    assert [f for n, f in fields if n == "image[]"] == ["img0.png", "img1.png", "img2.png"]
    sdk_fields = known_keys(ImageEditParamsBase)
    assert {n.removesuffix("[]") for n in names} <= sdk_fields, set(names) - sdk_fields
    assert {"model", "prompt", "size", "quality", "background", "output_format", "n", "user"} <= set(names)
    for k in ("input_fidelity", "response_format", "moderation", "stream"):
        assert k not in names


def test_openai_edit_kwargs_validate_against_the_sdk_literals():
    from openai.types.image_edit_params import ImageEditParamsBase

    spy = Spy(openai_images_response([png_bytes(1024, 1024)], size="1024x1024"))
    p = _openai(spy)
    p.edit(_oai_req(images=(O.NamedPng("a.png", png_bytes(1024, 1024)),), quality="xhigh"), CTX)
    kw = {k: v for k, v in p.last_kwargs.items() if k not in ("image", "mask")}
    assert not check(kw, ImageEditParamsBase), check(kw, ImageEditParamsBase)


def test_openai_size_rules_match_the_sdk_documentation():
    """SDK docs: WxH with both sides divisible by 16, ratio 1:3 to 3:1, at most 3840x2160 (the adapter is stricter at the 3840 edge)."""
    for w, h in ((1024, 1024), (1536, 1024), (1024, 1536), (1536, 864), (2560, 1440), (816, 816), (2048, 1152)):
        assert O.valid_size(w, h), (w, h)
    for w, h in ((1000, 1000), (4096, 2048), (1024, 3600), (0, 16)):
        assert not O.valid_size(w, h), (w, h)


def test_openai_usage_object_prices_the_call():
    usage = {"input_tokens": 300, "input_tokens_details": {"text_tokens": 100, "image_tokens": 200}, "output_tokens": 196, "total_tokens": 496}
    spy = Spy(openai_images_response([png_bytes(1024, 1024)], size="1024x1024", usage=usage))
    r = _openai(spy).generate(_oai_req(), CTX)
    assert r.usage_present and r.cost["basis"] == "usage"
    assert r.cost["usd"] == pytest.approx((100 * 5 + 200 * 8 + 196 * 30) / 1e6)


# ---------------------------------------------------------------------------------------------------------------------
# 3. Tripo (reference: tripo_openapi_head.yaml, UNVERIFIED against the live API)
# ---------------------------------------------------------------------------------------------------------------------

TRIPO = _ref("tripo_v3_request_schemas.json")


def _tripo_value_errors(value: Any, sch: dict[str, Any], path: str) -> list[str]:
    if "$ref" in sch:
        return _tripo_object_errors(value, TRIPO["objects"][sch["$ref"].split("/")[-1]], path)
    if "oneOf" in sch:
        alts = [_tripo_value_errors(value, s, path) for s in sch["oneOf"]]
        return [] if any(not a for a in alts) else [f"{path}: fits none of {len(alts)} alternatives: {alts[0]}"]
    t = sch.get("type")
    ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
          "number": isinstance(value, (int, float)) and not isinstance(value, bool), "boolean": isinstance(value, bool),
          "array": isinstance(value, list), "object": isinstance(value, dict), None: True}[t]
    if not ok:
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    errs: list[str] = []
    if t == "string" and "maxLength" in sch and len(value) > sch["maxLength"]:
        errs.append(f"{path}: longer than {sch['maxLength']}")
    if t == "array":
        if "maxItems" in sch and len(value) > sch["maxItems"]:
            errs.append(f"{path}: more than {sch['maxItems']} items")
        if "minItems" in sch and len(value) < sch["minItems"]:
            errs.append(f"{path}: fewer than {sch['minItems']} items")
        for i, v in enumerate(value):
            errs += _tripo_value_errors(v, sch.get("items", {}), f"{path}[{i}]")
    return errs


def _tripo_object_errors(value: Any, obj: dict[str, Any], path: str = "$") -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: expected object"]
    errs = []
    for k, v in value.items():
        if k not in obj["properties"]:
            if obj.get("additionalProperties") is False:
                errs.append(f"{path}: unknown key {k!r}")
            continue
        errs += _tripo_value_errors(v, obj["properties"][k], f"{path}.{k}")
    errs += [f"{path}: missing {k!r}" for k in obj.get("required", []) if k not in value]
    return errs


def _tripo_errors(req: Any, body: Any) -> list[str]:
    path = req.url.path.removeprefix("/v3")
    entry = TRIPO["paths"][path][req.method]
    return _tripo_object_errors(body, entry["schema"])


def _tripo_api(spy: Spy) -> T.TripoApi:
    ft = FakeClock()
    return T.TripoApi("tsk_secretkey0000", transport=spy.transport, sleep=noop_sleep, clock=ft.now,
                      limiter=RateLimiter(concurrent=2, clock=ft.now, sleep=ft.sleep))


def _created() -> Any:
    return json_response({"code": 0, "data": {"task_id": "t-1"}})


TOKENS = {"front": "tok-f", "left": "tok-l", "back": "tok-b", "right": "tok-r"}


def _tripo_calls() -> list[tuple[str, Any]]:
    p2, p1, h31 = T.P2Params.for_seed(3000, 11), T.P1Params.for_seed(3000, 11), T.H31Params.for_seed(3000, 11)
    p2_extras = T.P2Params(face_limit=3000, model_seed=11, texture_seed=11, orthographic_projection=True)
    return [
        ("image_to_multiview", lambda a: a.image_to_multiview("tok")),
        ("edit_multiview", lambda a: a.edit_multiview("t-mv", {"front": "make it red", "back": "make it blue"})),
        ("multiview_p2", lambda a: a.multiview_to_model(TOKENS, p2)),
        ("multiview_p2_ortho", lambda a: a.multiview_to_model(TOKENS, p2_extras)),
        ("multiview_p1", lambda a: a.multiview_to_model(TOKENS, p1)),
        ("multiview_h31", lambda a: a.multiview_to_model(TOKENS, h31)),
        ("multiview_reuse", lambda a: a.multiview_to_model("t-mv", p2)),
        ("image_to_model", lambda a: a.image_to_model("tok", p2)),
        ("image_to_model_ortho", lambda a: a.image_to_model("tok", p2_extras)),
        ("text_to_model", lambda a: a.text_to_model("a plush cat", p2, negative_prompt="blurry")),
        ("convert", lambda a: a.convert("t-1", fmt="GLTF", face_limit=3000)),
        ("texture_model", lambda a: a.texture_model("t-1", texture_seed=11, texture_prompt={"text": "red"})),
        ("mesh_segment", lambda a: a.segment_mesh("t-1", model="v2.0-20260430", granularity="balanced", split_by_connectivity=True)),
        ("mesh_complete", lambda a: a.complete_mesh("t-seg", part_names=["arm"], completion_mode="quick_cap")),
        ("retopology", lambda a: a.retopology("t-1", face_limit=3000, quad=False, bake=True)),
        ("import_model", lambda a: a.import_model("tok")),
    ]


@pytest.mark.parametrize(("name", "call"), _tripo_calls(), ids=[n for n, _ in _tripo_calls()])
def test_tripo_bodies_match_the_reference_request_schemas(name, call):
    spy = Spy(_created())
    call(_tripo_api(spy))
    spy.assert_hit(1)
    req, body = spy.requests[0], spy.bodies[0]
    assert req.method == "POST" and req.url.host == "openapi.tripo3d.ai" and req.url.path.startswith("/v3/")
    assert req.headers["authorization"] == "Bearer tsk_secretkey0000"
    assert not _tripo_errors(req, body), (name, _tripo_errors(req, body))


def test_tripo_generation_endpoints_drop_the_keys_their_schema_lacks():
    spy = Spy(_created(), _created())
    a = _tripo_api(spy)
    a.text_to_model("a plush cat", T.P2Params.for_seed(3000, 11))
    a.image_to_model("tok", T.P2Params(face_limit=3000, model_seed=11, texture_seed=11, orthographic_projection=True))
    assert not {"orientation", "texture_alignment"} & spy.bodies[0].keys()
    assert "orthographic_projection" not in spy.bodies[1] and spy.bodies[1]["texture_alignment"] == "original_image"


def test_tripo_multiview_retries_once_with_object_inputs_when_the_string_form_is_rejected():
    rejected = httpx.Response(400, json={"code": 400, "message": "inputs[0].front: invalid input", "suggestion": "", "request_id": "r"})
    spy = Spy(rejected, _created(), _created())
    a = _tripo_api(spy)
    p2 = T.P2Params.for_seed(3000, 11)
    assert a.multiview_to_model(TOKENS, p2) == "t-1"
    spy.assert_hit(2)
    assert spy.bodies[0]["inputs"][0] == {"front": "tok-f"}
    assert spy.bodies[1]["inputs"][0] == {"front": {"type": "png", "file_token": "tok-f"}}
    assert not _tripo_errors(spy.requests[1], spy.bodies[1]) and a.flags.get("tripo.multiview_object_inputs") is True
    a.multiview_to_model(TOKENS, p2)                                   # remembered
    assert spy.bodies[2]["inputs"][0] == {"front": {"type": "png", "file_token": "tok-f"}}


def test_tripo_upload_is_multipart_file_and_answers_data_file_token():
    spy = Spy(json_response({"code": 0, "data": {"file_token": "ft-1"}}))
    assert _tripo_api(spy).upload(png_bytes(64, 64), name="a.png") == "ft-1"
    req = spy.requests[0]
    assert req.url.path == "/v3/files" and [n for n, _f in _multipart(req)] == ["file"]
    assert set(TRIPO["paths"]["/files"]["POST"]["schema"]["properties"]) == {"file"}


def test_tripo_get_and_list_requests_match_the_reference():
    task = {"code": 0, "data": {"task_id": "t-1", "status": "queued", "progress": 0}}
    spy = Spy(json_response({"code": 0, "data": {"balance": 100.0, "frozen": 10.0}}), json_response({"code": 0, "data": []}),
              json_response(task), json_response({"code": 0, "data": {"tasks": {"t-1": task["data"]}, "missed": ["t-2"]}}))
    a = _tripo_api(spy)
    assert a.balance() == (100.0, 10.0)
    assert a.usage(limit=50, offset=0) == []
    assert a.task("t-1").status == "queued"
    found, missed = a.tasks(["t-1", "t-2"])
    assert set(found) == {"t-1"} and missed == ["t-2"]
    assert [(r.method, r.url.path) for r in spy.requests] == [("GET", "/v3/account/balance"), ("GET", "/v3/account/usage"),
                                                              ("GET", "/v3/tasks/t-1"), ("POST", "/v3/tasks/list")]
    assert dict(spy.requests[1].url.params) == {"limit": "50", "offset": "0"}
    assert not _tripo_errors(spy.requests[3], spy.bodies[3])
    assert TRIPO["paths"]["/account/usage"]["GET"]["operationId"] == "getUsage"


def test_tripo_statuses_and_result_url_fields():
    assert TRIPO["task_statuses"] == ["queued", "running", "success", "failed", "cancelled"]
    for raw in TRIPO["task_statuses"]:
        assert T.parse_task({"task_id": "t", "status": raw}).status == raw
    for legacy, code in (("banned", 2008), ("expired", 2018), ("unknown", None)):
        st = T.parse_task({"task_id": "t", "status": legacy})
        assert st.status == "failed" and st.error_code == code and st.failure_error() is not None, legacy       # never treated as success
    out = {"model_url": "https://a.tripo3d.ai/m.glb", "rendered_image_url": "https://a.tripo3d.ai/r.webp",
           "generate_multiview_image": {"front_view_url": "https://a.tripo3d.ai/f.png", "left_view_url": "https://a.tripo3d.ai/l.png",
                                        "back_view_url": "https://a.tripo3d.ai/b.png", "right_view_url": "https://a.tripo3d.ai/r.png"},
           "model_urls": ["https://a.tripo3d.ai/m0.glb"]}
    assert set(out) <= set(TRIPO["task_output_keys"])
    urls = T.parse_task({"task_id": "t", "status": "success", "output": out}).output_urls
    assert {"model_url", "rendered_image_url", "front_view_url", "left_view_url", "back_view_url", "right_view_url"} <= set(urls)


def test_tripo_error_envelope_fields_and_codes():
    assert set(TRIPO["error_response_keys"]) == {"code", "status", "message", "suggestion", "request_id"}
    e = T.error_from_response(400, {"code": 2008, "status": "error", "message": "blocked", "suggestion": "x", "request_id": "rq"})
    assert e.kind == "moderation" and e.request_id == "rq"
    assert T.error_from_response(200, {"code": 2010, "message": "no credits"}).kind == "billing"
    assert T.parse_task({"task_id": "t", "status": "failed", "error_code": 2018}).failure_error().retryable


def test_tripo_download_hosts_cover_both_tripo_domains():
    from duoskin.providers.base import HostAllowlist

    allow = HostAllowlist(T.DOWNLOAD_HOSTS)
    assert allow.allows("tripo-data.rg1.data.tripo3d.com") and allow.allows("cdn.tripo3d.ai") and allow.allows("a.b.tripo3d.com")
    assert not allow.allows("tripo3d.com.evil.com") and not allow.allows("evil.com")


# ---------------------------------------------------------------------------------------------------------------------
# 4. Recraft (references: Recraft MCP client, ComfyUI node, API Evangelist profile; UNVERIFIED against Recraft's own docs)
# ---------------------------------------------------------------------------------------------------------------------

RECRAFT_GENERATION_KEYS = {"prompt", "n", "model", "style", "style_id", "size", "negative_prompt", "response_format", "text_layout", "controls",
                           "style_match", "style_reference_urls", "substyle", "strength", "random_seed"}
RECRAFT_CONTROL_KEYS = {"colors", "background_color", "no_text", "artistic_level"}
RECRAFT_V4_SIZES = ["1024x1024", "1536x768", "768x1536", "1280x832", "832x1280", "1216x896", "896x1216", "1152x896", "896x1152",
                    "832x1344", "1280x896", "896x1280", "1344x768", "768x1344"]
RECRAFT_V4_PRO_SIZES = ["2048x2048", "3072x1536", "1536x3072", "2560x1664", "1664x2560", "2432x1792", "1792x2432", "2304x1792",
                        "1792x2304", "1664x2688", "2560x1792", "1792x2560", "2688x1536", "1536x2688"]
RECRAFT_VECTOR_MODELS = {"recraftv4_1_vector", "recraftv4_1_utility_vector", "recraftv4_1_pro_vector", "recraftv4_1_utility_pro_vector",
                         "recraftv4_styles_vector", "recraftv4_styles_pro_vector"}


def _recraft(spy: Spy) -> R.RecraftProvider:
    ft = FakeClock()
    return R.RecraftProvider("rc-key-0000", transport=spy.transport, sleep=noop_sleep,
                             limiter=RateLimiter(concurrent=2, ipm=100, rps=50, clock=ft.now, sleep=ft.sleep))


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#ff00ff"/></svg>'


def _b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode()


def test_recraft_models_and_sizes_match_the_reference_nodes():
    assert set(R.VECTOR_MODELS) == RECRAFT_VECTOR_MODELS
    assert list(R.V4_SIZES) == RECRAFT_V4_SIZES and list(R.V4_PRO_SIZES) == RECRAFT_V4_PRO_SIZES


def test_recraft_generation_body_uses_only_keys_the_references_know():
    sid = "229b2a75-05e4-4580-85f9-b47ee521a00d"
    spy = Spy(json_response({"created": 1, "credits": 160, "data": [{"image_id": "i1", "b64_json": _b64(SVG)}], "style_id": sid}))
    rec = _recraft(spy)
    rec.generate(R.VectorRequest(model="recraftv4_styles_vector", prompt="1. One round eye.", size="1024x1024", n=1, style_id=sid,
                                 colors=((255, 0, 0),), background_rgb=(255, 0, 255)), CTX)
    spy.assert_hit(1)
    req, body = spy.requests[0], spy.bodies[0]
    assert req.url.path == "/v1/images/generations" and req.headers["authorization"] == "Bearer rc-key-0000"
    assert set(body) <= RECRAFT_GENERATION_KEYS and set(body["controls"]) <= RECRAFT_CONTROL_KEYS
    assert body["controls"]["colors"] == [{"rgb": [255, 0, 0]}] and body["controls"]["background_color"] == {"rgb": [255, 0, 255]}
    assert "style" not in body and body["style_id"] == sid and body["style_match"] == "precise"      # ComfyUI sends no `style` for *_vector
    assert not {"negative_prompt", "no_text", "artistic_level"} & body.keys()                      # V4 ignores or rejects them


def test_recraft_file_endpoints_use_the_image_field_the_reference_clients_send():
    spy = Spy(json_response({"image": {"b64_json": _b64(SVG)}}), json_response({"image": {"b64_json": _b64(png_bytes(300, 300))}}))
    rec = _recraft(spy)
    rec.vectorize(png_bytes(300, 300))
    rec.remove_background(png_bytes(300, 300))
    assert [r.url.path for r in spy.requests] == ["/v1/images/vectorize", "/v1/images/removeBackground"]
    for r in spy.requests:
        names = [n for n, _f in _multipart(r)]
        assert "image" in names and "file" not in names and "response_format" in names


def test_recraft_create_style_and_user_endpoints():
    sid = "229b2a75-05e4-4580-85f9-b47ee521a00d"
    spy = Spy(json_response({"id": sid}), json_response({"id": "u", "credits": 1000}))
    rec = _recraft(spy)
    assert rec.create_style([png_bytes(300, 300), png_bytes(300, 300)], model="recraftv4_styles_vector", style="vector_illustration") == sid
    names = [n for n, _f in _multipart(spy.requests[0])]
    assert spy.requests[0].url.path == "/v1/styles" and {"style", "model", "file1", "file2"} <= set(names)
    assert rec.test_key()["ok"] and spy.requests[1].url.path == "/v1/users/me" and spy.requests[1].method == "GET"


def test_recraft_retries_with_url_when_b64_json_is_rejected():
    """``response_format="b64_json"`` is named by the API profile but never exercised by the references: a 400 about it switches to ``url``."""
    spy = Spy(httpx.Response(400, json={"message": "Invalid response_format: b64_json is not supported for this model"}),
              json_response({"created": 1, "credits": 80, "data": [{"image_id": "i1", "url": "https://img.recraft.ai/x.svg"}]}),
              httpx.Response(200, content=SVG))
    rec = _recraft(spy)
    out = rec.generate(R.VectorRequest(model="recraftv4_1_vector", prompt="1. A dot.", size="1024x1024", n=1), CTX)
    assert out.svgs == [SVG] and spy.bodies[0]["response_format"] == "b64_json" and spy.bodies[1]["response_format"] == "url"
    assert rec.flags.get("recraft.response_format") == "url"


# ---------------------------------------------------------------------------------------------------------------------
# 5. Gemini (reference: google-genai 2.11.0 source, official)
# ---------------------------------------------------------------------------------------------------------------------

GENAI = _ref("google_genai_rest_keys.json")


def _gemini(spy: Spy) -> G.GeminiProvider:
    ft = FakeClock()
    return G.GeminiProvider("AIzaSyTESTKEY0000000000000000000", transport=spy.transport, sleep=noop_sleep,
                            limiter=RateLimiter(concurrent=2, clock=ft.now, sleep=ft.sleep))


def _gemini_body_problems(body: dict[str, Any]) -> list[str]:
    errs = [f"top-level {k}" for k in body if k not in GENAI["request_top_level"]]
    for c in body["contents"]:
        errs += [f"content {k}" for k in c if k not in GENAI["Content"]]
        for part in c["parts"]:
            errs += [f"part {k}" for k in part if k not in GENAI["Part"]]
            if "inlineData" in part:
                errs += [f"inlineData {k}" for k in part["inlineData"] if k not in GENAI["Blob_mldev"]]
    gc = body["generationConfig"]
    errs += [f"generationConfig {k}" for k in gc if k not in GENAI["GenerationConfig"]]
    errs += [f"thinkingConfig {k}" for k in gc.get("thinkingConfig", {}) if k not in GENAI["ThinkingConfig"]]
    errs += [f"imageConfig {k}" for k in gc.get("imageConfig", {}) if k not in GENAI["ImageConfig_mldev"]]
    if "thinkingConfig" in gc and gc["thinkingConfig"]["thinkingLevel"] not in GENAI["ThinkingLevel"]:
        errs.append("thinkingLevel value")
    if "mediaResolution" in gc and gc["mediaResolution"] not in GENAI["MediaResolution"]:
        errs.append("mediaResolution value")
    if "imageConfig" in gc and gc["imageConfig"]["imageSize"] not in GENAI["image_size_values"]:
        errs.append("imageSize value")
    return errs


def test_gemini_judge_request_matches_the_official_sdk_field_names():
    answer = {"checks": [{"rule_id": "r1", "evidence": "x", "pass": True}]}
    spy = Spy(json_response({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}, "finishReason": "STOP"}],
                             "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 60, "thoughtsTokenCount": 40}}))
    out = _gemini(spy).judge(png_bytes(300, 300), [G.RuleSpec(rule_id="r1", statement="The iris is round.")], thinking_level="LOW", ctx=CTX)
    assert out[0].passed
    req, body = spy.requests[0], spy.bodies[0]
    assert req.url.path == "/v1beta/models/gemini-3.8-flash:generateContent" and req.headers["x-goog-api-key"].startswith("AIza")
    assert "key" not in req.url.params                                                    # the key never travels in the URL
    assert not _gemini_body_problems(body), _gemini_body_problems(body)
    gc = body["generationConfig"]
    assert gc["responseMimeType"] == "application/json" and "responseJsonSchema" in gc and "responseSchema" not in gc
    assert not {"temperature", "topP", "topK"} & gc.keys()


def test_gemini_image_request_matches_the_official_sdk_field_names():
    png = png_bytes(64, 64)
    resp = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": _b64(png)}}]}, "finishReason": "STOP"}]}
    spy = Spy(json_response(resp))
    raw, kind, _meta = _gemini(spy).generate(prompt="A character.", images=[png], aspect="1:1", size="1K", ctx=CTX)
    assert raw == png and kind == "png"
    req, body = spy.requests[0], spy.bodies[0]
    assert req.url.path == "/v1beta/models/gemini-3.1-flash-image:generateContent"
    assert not _gemini_body_problems(body), _gemini_body_problems(body)
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "1:1", "imageSize": "1K"}


def test_gemini_finish_and_block_reasons_are_known_to_the_sdk():
    for r in G.MODERATION_REASONS | G.RECITATION_REASONS:
        assert r in GENAI["FinishReason"] or r in GENAI["BlockedReason"], r
