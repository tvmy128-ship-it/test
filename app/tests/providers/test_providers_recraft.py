"""Recraft adapter: body rules, styles registry, downloads, retries, utility endpoints (APP_SPEC 7.4)."""
from __future__ import annotations

import base64

import pytest
from prov_helpers import Spy, json_response, noop_sleep, png_bytes

from duoskin.providers import recraft as R
from duoskin.providers._http import httpx
from duoskin.providers.base import CallCtx, CapabilityFlags, ProviderError, RateLimiter

CTX = CallCtx.null()
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#ff00ff"/></svg>'
STYLE_ID = "229b2a75-05e4-4580-85f9-b47ee521a00d"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def gen_ok(n=1, **extra):
    return json_response({"created": 1, "credits": 80 * n, "data": [{"image_id": f"img-{i}", "b64_json": b64(SVG)} for i in range(n)], **extra},
                         headers={"x-request-id": "rc-1"})


def provider(spy: Spy, **kw) -> R.RecraftProvider:
    kw.setdefault("limiter", RateLimiter(concurrent=2, ipm=100, rps=50))
    kw.setdefault("sleep", noop_sleep)
    return R.RecraftProvider("rc-key-0000", transport=spy.transport, **kw)


def vreq(**kw) -> R.VectorRequest:
    d = dict(model="recraftv4_1_vector", prompt="1. One round eye iris.", size="1024x1024", n=1)
    d.update(kw)
    return R.VectorRequest(**d)


def test_generate_body_has_only_allowed_keys_and_bearer_auth():
    spy = Spy(gen_ok(2))
    p = provider(spy)
    r = p.generate(vreq(n=2, colors=((255, 0, 0), (0, 0, 255)), background_rgb=(255, 0, 255), nonce="n1", tag="R1"), CTX)
    spy.assert_hit(1)
    http, body = spy.requests[0], spy.bodies[0]
    assert str(http.url) == "https://external.api.recraft.ai/v1/images/generations"
    assert http.headers["authorization"] == "Bearer rc-key-0000"
    assert body == {"prompt": "1. One round eye iris.", "model": "recraftv4_1_vector", "size": "1024x1024", "n": 2, "response_format": "b64_json",
                    "controls": {"colors": [{"rgb": [255, 0, 0]}, {"rgb": [0, 0, 255]}], "background_color": {"rgb": [255, 0, 255]}}}
    for k in R.FORBIDDEN_BODY_KEYS:
        assert k not in body
    assert len(r.svgs) == 2 and r.svgs[0] == SVG and r.image_ids == ["img-0", "img-1"] and r.credits == 160.0
    assert r.request_json["nonce"] == "n1" and r.request_json["model"] == "recraftv4_1_vector"
    assert r.cost["provider"] == "recraft" and r.cost["usd"] == pytest.approx(0.16) and r.cost["operation"] == "images.generate:R1"


def test_styles_model_requires_style_id_and_sends_style_match():
    spy = Spy(gen_ok(1, style_id=STYLE_ID))
    p = provider(spy)
    with pytest.raises(ProviderError) as ei:
        p.generate(vreq(model="recraftv4_styles_vector"), CTX)
    assert ei.value.code == "style_id_required" and not spy.requests
    r = p.generate(vreq(model="recraftv4_styles_vector", style_id=STYLE_ID, style_match="flexible"), CTX)
    assert spy.bodies[0]["style_id"] == STYLE_ID and spy.bodies[0]["style_match"] == "flexible" and "style" not in spy.bodies[0]
    assert r.style_id == STYLE_ID
    # without a style id there is no style_match
    spy2 = Spy(gen_ok())
    provider(spy2).generate(vreq(), CTX)
    assert "style_match" not in spy2.bodies[0] and "style_id" not in spy2.bodies[0]


def test_style_match_dropped_when_flag_says_unsupported():
    spy = Spy(gen_ok())
    provider(spy, flags=CapabilityFlags({"recraft.style_match_ok": False})).generate(vreq(model="recraftv4_styles_vector", style_id=STYLE_ID), CTX)
    assert "style_match" not in spy.bodies[0] and spy.bodies[0]["style_id"] == STYLE_ID


@pytest.mark.parametrize(("kw", "code"), [
    ({"size": "1:1"}, "bad_size"), ({"size": "585x559"}, "bad_size"), ({"size": "2048x2048"}, "bad_size"),
    ({"n": 7}, "bad_n"), ({"n": 0}, "bad_n"), ({"model": "recraftv3_vector"}, "bad_model"), ({"model": "recraftv4"}, "bad_model"),
    ({"prompt": ""}, "bad_prompt"), ({"style_match": "loose"}, "bad_style_match"), ({"colors": ((256, 0, 0),)}, "bad_color"),
    ({"background_rgb": (1, 2)}, "bad_color"),
])
def test_validation_rejects_without_sending(kw, code):
    spy = Spy()
    with pytest.raises(ProviderError) as ei:
        provider(spy).generate(vreq(**kw), CTX)
    assert ei.value.kind == "bad_request" and ei.value.code == code and not spy.requests


def test_pro_models_take_pro_sizes():
    spy = Spy(gen_ok())
    provider(spy).generate(vreq(model="recraftv4_1_pro_vector", size="2048x2048"), CTX)
    assert spy.bodies[0]["size"] == "2048x2048"


def test_raster_style_id_on_a_vector_model_is_refused():
    reg = R.StyleRegistry()
    reg.register("raster", "face-raster", STYLE_ID)
    with pytest.raises(ProviderError) as ei:
        provider(Spy(), registry=reg).generate(vreq(model="recraftv4_styles_vector", style_id=STYLE_ID), CTX)
    assert ei.value.code == "style_kind_mismatch"
    reg.register("vector", "face-vec", "other-id")
    assert reg.kind_of("other-id") == "vector" and reg.get("vector", "face-vec") == "other-id" and reg.get("raster", "face-vec") is None


def test_url_response_is_downloaded_from_allowlisted_host_without_auth():
    url = "https://img.recraft.ai/abc/def.svg?sig=secret"
    spy = Spy(json_response({"created": 1, "credits": 80, "data": [{"image_id": "i", "url": url}]}),
              httpx.Response(200, content=SVG))
    r = provider(spy, response_format="url").generate(vreq(), CTX)
    spy.assert_hit(2)
    assert spy.bodies[0]["response_format"] == "url"
    assert str(spy.requests[1].url) == url and "authorization" not in spy.requests[1].headers
    assert r.svgs == [SVG]


def test_url_on_unexpected_host_is_not_fetched():
    spy = Spy(json_response({"created": 1, "credits": 80, "data": [{"image_id": "i", "url": "https://evil.example.com/x.svg"}]}))
    with pytest.raises(ProviderError) as ei:
        provider(spy).generate(vreq(), CTX)
    spy.assert_hit(1)
    assert ei.value.code == "host_not_allowed"


def test_png_instead_of_svg_is_a_validation_error():
    spy = Spy(json_response({"created": 1, "credits": 80, "data": [{"image_id": "i", "b64_json": b64(png_bytes(8, 8))}]}))
    with pytest.raises(ProviderError) as ei:
        provider(spy).generate(vreq(), CTX)
    assert ei.value.kind == "validation" and ei.value.code == "not_svg_png" and ei.value.billed == "yes"


def test_no_images_and_bad_json():
    with pytest.raises(ProviderError) as e1:
        provider(Spy(json_response({"created": 1, "credits": 0, "data": []}))).generate(vreq(), CTX)
    assert e1.value.code == "no_images"
    with pytest.raises(ProviderError) as e2:
        provider(Spy(httpx.Response(200, content=b"<html>nope</html>"))).generate(vreq(), CTX)
    assert e2.value.code == "not_json"


def test_429_backs_off_honouring_retry_after_then_succeeds():
    slept = []
    spy = Spy(httpx.Response(429, json={"message": "slow down"}, headers={"retry-after": "3"}),
              httpx.Response(429, json={"message": "slow down"}), gen_ok())
    lim = RateLimiter(concurrent=1)
    p = provider(spy, limiter=lim, sleep=lambda s: slept.append(s))
    r = p.generate(vreq(), CTX)
    spy.assert_hit(3)
    assert len(r.svgs) == 1 and sum(slept) >= 3.0 and lim._blocked_until > 0


def test_429_exhausted_raises_rate_limit_with_retry_after():
    spy = Spy(*[httpx.Response(429, json={"message": "slow"}, headers={"retry-after": "1"})] * 4)
    with pytest.raises(ProviderError) as ei:
        provider(spy, rate_limit_retries=3).generate(vreq(), CTX)
    spy.assert_hit(4)
    assert ei.value.kind == "rate_limit" and ei.value.retryable and ei.value.retry_after_s == 1.0


def test_5xx_retried_at_most_twice():
    spy = Spy(httpx.Response(500, json={"message": "boom"}), httpx.Response(503, text="down"), gen_ok())
    assert provider(spy).generate(vreq(), CTX).svgs
    spy.assert_hit(3)
    spy2 = Spy(*[httpx.Response(500, json={"message": "boom"})] * 3)
    with pytest.raises(ProviderError) as ei:
        provider(spy2).generate(vreq(), CTX)
    spy2.assert_hit(3)
    assert ei.value.kind == "server"


@pytest.mark.parametrize(("status", "body", "kind"), [
    (401, {"message": "invalid token"}, "auth"), (403, {"message": "forbidden"}, "permission"),
    (400, {"message": "style_id is required"}, "bad_request"), (402, {"message": "not enough API units"}, "billing"),
    (400, {"message": "Insufficient API units balance"}, "billing"),
])
def test_client_errors_are_not_retried(status, body, kind):
    spy = Spy(httpx.Response(status, json=body))
    with pytest.raises(ProviderError) as ei:
        provider(spy).generate(vreq(), CTX)
    spy.assert_hit(1)
    assert ei.value.kind == kind and ei.value.billed == "no" and not ei.value.retryable


def test_timeout_and_connection_errors():
    with pytest.raises(ProviderError) as e1:
        provider(Spy(httpx.ReadTimeout("t"))).generate(vreq(), CTX)
    assert e1.value.kind == "timeout"
    with pytest.raises(ProviderError) as e2:
        provider(Spy(httpx.ConnectError("c"))).generate(vreq(), CTX)
    assert e2.value.kind == "network"


def test_images_per_minute_bucket_counts_n():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    lim = RateLimiter(ipm=6, concurrent=1, clock=lambda: now[0], sleep=sleep)
    spy = Spy(gen_ok(4), gen_ok(4))
    p = provider(spy, limiter=lim)
    p.generate(vreq(n=4), CTX)
    p.generate(vreq(n=4), CTX)
    assert sum(slept) > 0        # 8 images against a 6/min bucket


# ----- styles -----------------------------------------------------------------------------------------------------

def test_create_style_multipart_registers_vector_style():
    spy = Spy(json_response({"id": STYLE_ID}))
    p = provider(spy)
    sid = p.create_style([png_bytes(64, 64), png_bytes(64, 64, (1, 2, 3, 255))], model="recraftv4_styles_vector", style="vector_illustration")
    spy.assert_hit(1)
    raw = spy.requests[0].content
    assert sid == STYLE_ID and str(spy.requests[0].url).endswith("/v1/styles")
    assert b'name="file1"' in raw and b'name="file2"' in raw and b"vector_illustration" in raw and b"recraftv4_styles_vector" in raw
    assert p.styles.kind_of(STYLE_ID) == "vector"


def test_create_style_rejects_svg_wrong_pairing_and_too_many():
    p = provider(Spy())
    with pytest.raises(ProviderError) as e1:
        p.create_style([SVG], model="recraftv4_styles_vector", style="vector_illustration")
    assert e1.value.code == "style_ref_not_png"
    with pytest.raises(ProviderError) as e2:
        p.create_style([png_bytes(8, 8)], model="recraftv4_styles_vector", style="any")
    assert e2.value.code == "style_model_mismatch"
    with pytest.raises(ProviderError) as e3:
        p.create_style([png_bytes(8, 8)] * 11, model="recraftv4_styles", style="any")
    assert e3.value.code == "style_refs_limits"
    with pytest.raises(ProviderError) as e4:
        p.create_style([png_bytes(8, 8)], model="recraftv3", style="any")   # type: ignore[arg-type]
    assert e4.value.code == "bad_model"


# ----- vectorize and removeBackground -------------------------------------------------------------------------------

def test_vectorize_posts_file_field_and_shape_limit():
    spy = Spy(json_response({"image": {"b64_json": b64(SVG)}}, headers={"x-request-id": "v1"}))
    p = provider(spy)
    out = p.vectorize(png_bytes(300, 300), max_num_shapes=40)
    spy.assert_hit(1)
    raw = spy.requests[0].content
    assert out == SVG and str(spy.requests[0].url).endswith("/v1/images/vectorize")
    assert b'name="file"' in raw and b'name="max_num_shapes"' in raw and b"40" in raw and b'name="limit_num_shapes"' in raw
    assert b"svg_compression" not in raw


def test_vectorize_field_name_fallback_stores_flag():
    spy = Spy(httpx.Response(400, json={"message": "missing file"}), json_response({"image": {"b64_json": b64(SVG)}}))
    flags = CapabilityFlags()
    p = provider(spy, flags=flags)
    assert p.vectorize(png_bytes(300, 300)) == SVG
    spy.assert_hit(2)
    assert b'name="file"' in spy.requests[0].content and b'name="image"' in spy.requests[1].content
    assert flags.get("recraft.file_field_name") == "image"
    # the stored flag is used first from now on
    spy2 = Spy(json_response({"image": {"b64_json": b64(SVG)}}))
    provider(spy2, flags=flags).vectorize(png_bytes(300, 300))
    assert b'name="image"' in spy2.requests[0].content


def test_remove_background_returns_png_and_checks_input_limits():
    out = png_bytes(300, 300, (1, 2, 3, 0))
    spy = Spy(json_response({"image": {"b64_json": b64(out)}}))
    p = provider(spy)
    assert p.remove_background(png_bytes(300, 300)) == out
    with pytest.raises(ProviderError) as e1:
        p.remove_background(png_bytes(100, 100))          # below 256 px
    assert e1.value.code == "input_limits"
    with pytest.raises(ProviderError) as e2:
        p.vectorize(b"not a png")
    assert e2.value.code == "input_not_png"
    spy2 = Spy(json_response({"image": {"b64_json": b64(SVG)}}))
    with pytest.raises(ProviderError) as e3:
        provider(spy2).remove_background(png_bytes(300, 300))
    assert e3.value.code == "not_png"


def test_cost_sink_and_repr_hide_key():
    seen = []
    spy = Spy(gen_ok())
    p = provider(spy, cost_sink=seen.append)
    p.generate(vreq(), CTX)
    assert len(seen) == 1 and "rc-key" not in repr(p)
