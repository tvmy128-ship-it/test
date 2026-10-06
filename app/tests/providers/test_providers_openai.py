"""GPT Image adapter: request rules, batching, size drift, masks, alpha, error mapping (APP_SPEC 7.3)."""
from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image
from prov_helpers import (
    FakeClock,
    Spy,
    make_openai_client,
    noop_sleep,
    openai_error,
    openai_images_response,
    png_bytes,
)

from duoskin.providers import openai_images as O
from duoskin.providers._http import httpx
from duoskin.providers.base import CallCtx, CapabilityFlags, ProviderError, RateLimiter

MODEL = "gpt-image-2.5-flare-2026-09-08"
CTX = CallCtx.null()


def req(**kw) -> O.ImageRequest:
    d = {"model": MODEL, "prompt": "A tidy blocky character on a plain backdrop.", "size": "1024x1024", "quality": "low",
         "background": "opaque", "n": 1, **kw}
    return O.ImageRequest(**d)


def img(w=1024, h=1024, color=(10, 20, 30, 255)) -> bytes:
    return png_bytes(w, h, color)


def make(spy: Spy, **kw) -> O.OpenAIImages:
    ft = FakeClock()
    kw.setdefault("limiter", RateLimiter(concurrent=2, ipm=100, clock=ft.now, sleep=ft.sleep))
    kw.setdefault("sleep", noop_sleep)
    return O.OpenAIImages(client=make_openai_client(spy), **kw)


def ok(n=1, size="1024x1024", usage=None, **kw) -> object:
    w, h = map(int, size.split("x"))
    return openai_images_response([img(w, h)] * n, size=size, usage=usage, **kw)


def mask_png(w=1024, h=1024, editable=(100, 300, 100, 300), mode="RGBA") -> bytes:
    m = np.zeros((h, w, 4), np.uint8)
    m[..., 3] = 255
    y0, y1, x0, x1 = editable
    m[y0:y1, x0:x1, 3] = 0
    buf = io.BytesIO()
    Image.fromarray(m, "RGBA").convert(mode).save(buf, "PNG")
    return buf.getvalue()


# ----- helpers ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("w", "h", "legal"), [(1024, 1024, True), (816, 816, True), (1024, 1536, True), (816, 1632, True),
                                               (2048, 1152, True), (585, 559, False), (512, 512, False), (1024, 1030, False),
                                               (3840, 1024, False), (4096, 2048, False), (2000, 400, False), (0, 16, False)])
def test_valid_size(w, h, legal):
    assert O.valid_size(w, h) is legal


def test_plan_batches_is_ceil_n_over_4_balanced():
    assert O.plan_batches(1) == [1] and O.plan_batches(4) == [4] and O.plan_batches(5) == [3, 2]
    assert O.plan_batches(6) == [3, 3] and O.plan_batches(7) == [4, 3] and O.plan_batches(8) == [4, 4] and O.plan_batches(9) == [3, 3, 3]
    assert O.plan_batches(10) == [4, 3, 3] and O.plan_batches(6, per_request=2) == [2, 2, 2] and O.plan_batches(5, per_request=2) == [2, 2, 1]


def test_pinned_snapshot_detection():
    assert O.is_pinned_snapshot("gpt-image-2.5-sunburst-2026-09-08") and not O.is_pinned_snapshot("gpt-image-2.5-flare")
    assert not O.is_pinned_snapshot("dall-e-2") and not O.is_pinned_snapshot("gpt-image-1.5")


def test_alpha_helpers():
    assert O.has_real_alpha(png_bytes(40, 40, (0, 0, 0, 0)))
    assert not O.has_real_alpha(png_bytes(40, 40, (0, 0, 0, 255)))
    assert not O.has_real_alpha(png_bytes(40, 40, (1, 2, 3), mode="RGB"))
    flat = Image.open(io.BytesIO(O.flatten_on(png_bytes(8, 8, (0, 0, 0, 0))))).convert("RGBA")
    assert flat.getpixel((0, 0)) == (0xF2, 0xF2, 0xF2, 255)


# ----- request rules ---------------------------------------------------------------------------------------------

def test_generate_sends_exactly_the_documented_params():
    spy = Spy(ok(usage={"input_tokens": 100, "output_tokens": 196, "total_tokens": 296,
                        "input_tokens_details": {"text_tokens": 100, "image_tokens": 0}}))
    r = make(spy).generate(req(), CTX)
    spy.assert_hit(1)
    body = spy.bodies[0]
    assert spy.requests[0].url.path == "/v1/images/generations"
    assert body == {"model": MODEL, "prompt": "A tidy blocky character on a plain backdrop.", "size": "1024x1024", "quality": "low",
                    "background": "opaque", "output_format": "png", "n": 1, "user": "duoskin-local"}
    for forbidden in ("moderation", "input_fidelity", "output_compression", "stream", "response_format", "partial_images"):
        assert forbidden not in body
    assert len(r.images) == 1 and r.size == "1024x1024" and r.request_id == "req_oai_1" and r.model == MODEL
    assert r.usage["output_tokens"] == 196 and r.usage_present
    assert r.cost["provider"] == "openai" and r.cost["basis"] == "usage" and r.cost["usd"] == pytest.approx(0.0059 + 100 * 5e-6, rel=0.05)
    assert r.raw_sha256 and len(r.raw_sha256[0]) == 64 and r.alpha_present == [False]


def test_usage_none_gives_an_estimate_and_conservative_flag():
    spy = Spy(ok(n=2))
    flags = CapabilityFlags()
    r = make(spy, flags=flags).generate(req(n=2, quality="high"), CTX)
    assert r.usage is None and not r.usage_present
    assert r.cost["basis"] == "estimate" and r.cost["usd"] > 2 * 0.05
    assert flags.get("openai.usage_present") is False


def test_edit_sends_multipart_with_named_png_tuples_and_mask():
    spy = Spy(ok())
    r = make(spy, flags=CapabilityFlags({"openai.mask_multi_ok": True})).edit(
        req(images=(O.NamedPng("guide.png", img()), O.NamedPng("style.png", img())), mask=O.NamedPng("mask.png", mask_png())), CTX)
    spy.assert_hit(1)
    http = spy.requests[0]
    assert http.url.path == "/v1/images/edits" and http.headers["content-type"].startswith("multipart/form-data")
    raw = http.content
    assert raw.count(b'name="image[]"') == 2 and b'filename="guide.png"' in raw and b'filename="style.png"' in raw
    assert b'name="mask"' in raw and b"image/png" in raw
    assert b'name="model"' in raw and b'name="quality"' in raw and b'name="user"' in raw and b"duoskin-local" in raw
    assert b'name="moderation"' not in raw and b'name="input_fidelity"' not in raw
    assert r.adjustments == []


def test_generate_rejects_images_and_edit_requires_them():
    a = make(Spy())
    with pytest.raises(ProviderError) as e1:
        a.generate(req(images=(O.NamedPng("a.png", img()),)), CTX)
    assert e1.value.code == "generate_with_images"
    with pytest.raises(ProviderError) as e2:
        a.edit(req(), CTX)
    assert e2.value.code == "edit_needs_image"


@pytest.mark.parametrize(("kw", "code"), [
    ({"size": "585x559"}, "illegal_size"), ({"size": "auto"}, "bad_size"), ({"size": "1024x1030"}, "illegal_size"),
    ({"quality": "auto"}, "bad_quality"), ({"model": "gpt-image-1.5"}, "model_not_pinned"), ({"model": "dall-e-2"}, "model_not_pinned"),
    ({"n": 0}, "bad_n"), ({"n": 5}, "n_exceeds_request_cap"), ({"background": "auto"}, "bad_background"), ({"user": "me@example.com"}, "bad_user"),
    ({"prompt": "  "}, "empty_prompt"), ({"output_format": "jpeg"}, "bad_output_format"),
])
def test_preflight_rejects_code_bugs_without_sending(kw, code):
    spy = Spy()
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(**kw), CTX)
    assert ei.value.kind == "bad_request" and ei.value.code == code and ei.value.billed == "no"
    assert not spy.requests


@pytest.mark.parametrize(("mask", "code"), [
    (lambda: mask_png(mode="L"), "mask_not_rgba"),
    (lambda: mask_png(w=512, h=512), "mask_size_mismatch"),
    (lambda: mask_png(editable=(0, 0, 0, 0)), "mask_empty"),
])
def test_mask_validation(mask, code):
    with pytest.raises(ProviderError) as ei:
        make(Spy()).edit(req(images=(O.NamedPng("a.png", img()),), mask=O.NamedPng("m.png", mask())), CTX)
    assert ei.value.code == code


def test_mask_alpha_must_be_binary_and_polarity_means_alpha_zero_is_editable():
    m = np.zeros((1024, 1024, 4), np.uint8)
    m[..., 3] = 128
    buf = io.BytesIO()
    Image.fromarray(m, "RGBA").save(buf, "PNG")
    with pytest.raises(ProviderError) as ei:
        make(Spy()).edit(req(images=(O.NamedPng("a.png", img()),), mask=O.NamedPng("m.png", buf.getvalue())), CTX)
    assert ei.value.code == "mask_alpha_values"
    # an all-editable mask (alpha 0 everywhere) is allowed: FINALIZE of a transparent draft uses it (bible U26)
    allz = np.zeros((1024, 1024, 4), np.uint8)
    buf2 = io.BytesIO()
    Image.fromarray(allz, "RGBA").save(buf2, "PNG")
    spy = Spy(ok())
    make(spy).edit(req(images=(O.NamedPng("a.png", img()),), mask=O.NamedPng("m.png", buf2.getvalue())), CTX)
    spy.assert_hit(1)


def test_too_many_input_images_and_non_png_input():
    imgs = tuple(O.NamedPng(f"{i}.png", img(64, 64)) for i in range(17))
    with pytest.raises(ProviderError) as ei:
        make(Spy()).edit(req(images=imgs, image1_role="reference"), CTX)
    assert ei.value.code == "too_many_images"
    with pytest.raises(ProviderError) as e2:
        make(Spy()).edit(req(images=(O.NamedPng("a.jpg", b"\xff\xd8junk"),)), CTX)
    assert e2.value.code == "input_not_png"


def test_edit_target_must_match_requested_size_but_reference_image1_may_not():
    small = O.NamedPng("crop.png", img(400, 300))
    with pytest.raises(ProviderError) as ei:             # the default role is edit_target: Image 1 must itself be the requested size
        make(Spy()).edit(req(images=(small,), mask=O.NamedPng("m.png", mask_png(400, 300))), CTX)
    assert ei.value.code == "image1_size_mismatch"
    with pytest.raises(ProviderError):
        make(Spy()).edit(req(images=(small,)), CTX)
    spy = Spy(ok())                                      # I2/I5/I6/I10: a reference crop as Image 1 is checked against the request only
    r = make(spy).edit(req(images=(small,), image1_role="reference"), CTX)
    assert r.size == "1024x1024"


# ----- mask + several images, RGBA Image 1 (capability flags) --------------------------------------------------------

def test_mask_with_several_images_drops_mask_for_opaque_image1_when_flag_false():
    spy = Spy(ok())
    r = make(spy).edit(req(images=(O.NamedPng("a.png", img()), O.NamedPng("b.png", img())), mask=O.NamedPng("m.png", mask_png())), CTX)
    assert b'name="mask"' not in spy.requests[0].content and spy.requests[0].content.count(b'name="image[]"') == 2
    assert r.adjustments == ["mask_dropped:mask_multi_ok=false"]


def test_mask_with_several_images_drops_extras_for_transparent_image1():
    spy = Spy(ok())
    a = png_bytes(1024, 1024, (0, 0, 0, 0))
    r = make(spy).edit(req(images=(O.NamedPng("a.png", a), O.NamedPng("b.png", img())), mask=O.NamedPng("m.png", mask_png())), CTX)
    raw = spy.requests[0].content
    assert b'name="mask"' in raw and raw.count(b'name="image[]"') == 1
    assert r.adjustments == ["extra_images_dropped:mask_multi_ok=false"]


def test_rgba_image1_without_mask_is_flattened_unless_flag():
    transparent = png_bytes(1024, 1024, (0, 0, 0, 0))
    spy = Spy(ok())
    r = make(spy).edit(req(images=(O.NamedPng("a.png", transparent),)), CTX)
    raw = spy.requests[0].content
    start = raw.index(b"\x89PNG")
    sent = Image.open(io.BytesIO(raw[start:])).convert("RGBA")
    assert sent.getpixel((3, 3)) == (0xF2, 0xF2, 0xF2, 255) and r.adjustments == ["image1_flattened_on_F2F2F2:rgba_image1_ok=false"]
    spy2 = Spy(ok())
    make(spy2, flags=CapabilityFlags({"openai.rgba_image1_ok": True})).edit(req(images=(O.NamedPng("a.png", transparent),)), CTX)
    raw2 = spy2.requests[0].content
    sent2 = Image.open(io.BytesIO(raw2[raw2.index(b"\x89PNG"):])).convert("RGBA")
    assert sent2.getpixel((3, 3))[3] == 0


def test_rgba_image1_with_explicit_mask_is_sent_untouched():
    transparent = png_bytes(1024, 1024, (0, 0, 0, 0))
    spy = Spy(ok())
    r = make(spy).edit(req(images=(O.NamedPng("a.png", transparent),), mask=O.NamedPng("m.png", mask_png())), CTX)
    assert r.adjustments == [] and b'name="mask"' in spy.requests[0].content


# ----- batching and limiter -------------------------------------------------------------------------------------

def test_run_many_6_is_two_requests_of_3_each_through_the_ipm_limiter():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    lim = RateLimiter(ipm=5, concurrent=1, clock=lambda: now[0], sleep=sleep)
    spy = Spy(ok(n=3, request_id="r1"), ok(n=3, request_id="r2"))
    r = make(spy, limiter=lim).run_many(req(n=1, nonce="N"), 6, CTX)
    spy.assert_hit(2)
    assert [b["n"] for b in spy.bodies] == [3, 3] and len(r.images) == 6 and r.n_total == 6 and r.batch_sizes == [3, 3]
    assert r.request_ids == ["r1", "r2"] and [b["batch_index"] for b in r.batches] == [0, 1]
    assert sum(slept) > 0, "the second request must wait for images-per-minute tokens (3 + 3 > 5)"
    assert r.cost["usd"] > 0 and len(r.raw_sha256) == 6


def test_run_many_respects_the_ipm_setting_per_request():
    now = [0.0]

    def sleep(s):
        now[0] += s

    spy = Spy(ok(n=2), ok(n=2), ok(n=2))
    r = make(spy, limiter=RateLimiter(ipm=2, concurrent=1, clock=lambda: now[0], sleep=sleep)).run_many(req(), 6, CTX)
    assert [b["n"] for b in spy.bodies] == [2, 2, 2] and len(r.images) == 6
    with pytest.raises(ProviderError) as ei:       # CHK-P02: n <= min(IPM, 4) per request
        make(Spy(), limiter=RateLimiter(ipm=2, concurrent=1)).generate(req(n=3), CTX)
    assert ei.value.code == "n_exceeds_request_cap"


def test_run_many_sub_requests_carry_nonce_and_batch_index():
    seen = []

    class Spying(O.OpenAIImages):
        def _single(self, r, ctx, *, edit):
            seen.append((r.nonce, r.n))
            return super()._single(r, ctx, edit=edit)

    spy = Spy(ok(n=4), ok(n=4))
    Spying(client=make_openai_client(spy), limiter=RateLimiter(concurrent=1, ipm=100), sleep=noop_sleep).run_many(req(nonce="abc"), 8, CTX)
    assert seen == [("abc#0", 4), ("abc#1", 4)]


def test_run_many_partial_failure_hands_back_the_paid_images():
    spy = Spy(ok(n=3), openai_error(400, "Your request was rejected by the safety system.", "moderation_blocked"))
    with pytest.raises(ProviderError) as ei:
        make(spy).run_many(req(), 6, CTX)
    assert ei.value.kind == "moderation" and ei.value.partial is not None and len(ei.value.partial.images) == 3


def test_fewer_images_than_asked_is_a_warning_not_an_error():
    r = make(Spy(ok(n=3))).generate(req(n=4), CTX)
    assert len(r.images) == 3 and r.warnings and "asked for 4" in r.warnings[0]


def test_run_many_validates_n_total():
    for bad in (0, 33, True):
        with pytest.raises(ProviderError) as ei:
            make(Spy()).run_many(req(), bad, CTX)
        assert ei.value.code == "bad_n"


# ----- size drift ---------------------------------------------------------------------------------------------------

def test_response_size_field_drift_is_rejected():
    spy = Spy(openai_images_response([img(1024, 1024)], size="1024x1536"))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    e = ei.value
    assert e.kind == "validation" and e.code == "size_drift" and e.billed == "yes" and e.cost and e.context["got"] == "1024x1536"


def test_decoded_size_drift_is_rejected_even_when_r_size_lies():
    spy = Spy(openai_images_response([img(1024, 1536)], size="1024x1024"))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    assert ei.value.code == "size_drift" and ei.value.context["got"] == "1024x1536"


def test_decoded_size_must_equal_image1_for_edit_targets():
    # request 1024x1024, Image 1 is 1024x1024 with a mask => both must agree; a 1024x1024 output passes
    spy = Spy(ok())
    make(spy, flags=CapabilityFlags({"openai.mask_multi_ok": True})).edit(
        req(images=(O.NamedPng("a.png", img()),), mask=O.NamedPng("m.png", mask_png())), CTX)
    spy.assert_hit(1)


def test_check_response_sizes_helper_never_resizes():
    O.check_response_sizes("1024x1024", "1024x1024", [(1024, 1024)], (1024, 1024))
    with pytest.raises(ProviderError):
        O.check_response_sizes("1024x1024", None, [(1024, 1024)], (816, 816))


# ----- transparent background ------------------------------------------------------------------------------------------

def test_transparent_request_returning_opaque_is_flagged_for_the_caller():
    r = make(Spy(ok())).generate(req(background="transparent"), CTX)
    assert r.alpha_present == [False] and "transparent_requested_but_opaque" in r.warnings
    spy = Spy(openai_images_response([png_bytes(1024, 1024, (0, 0, 0, 0))], size="1024x1024"))
    r2 = make(spy).generate(req(background="transparent"), CTX)
    assert r2.alpha_present == [True] and not r2.warnings


def test_non_png_output_is_rejected():
    import base64
    resp = httpx.Response(200, json={"created": 1, "size": "1024x1024", "data": [{"b64_json": base64.b64encode(b"\xff\xd8jpeg").decode()}]})
    with pytest.raises(ProviderError) as ei:
        make(Spy(resp)).generate(req(), CTX)
    assert ei.value.code == "not_png"


# ----- error mapping ---------------------------------------------------------------------------------------------------

def test_moderation_blocked_is_unbilled_and_never_retried():
    spy = Spy(openai_error(400, "Your request was rejected by the safety system. safety_violations=[sexual]", "moderation_blocked"))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    spy.assert_hit(1)
    e = ei.value
    assert e.kind == "moderation" and e.group == "moderation_blocked" and e.billed == "no" and not e.retryable
    assert e.code == "moderation_blocked" and e.request_id == "req_oai_err"


def test_insufficient_quota_is_billing_for_429_and_400():
    for status in (429, 400):
        spy = Spy(openai_error(status, "You exceeded your current quota", "insufficient_quota", etype="insufficient_quota"))
        with pytest.raises(ProviderError) as ei:
            make(spy).generate(req(), CTX)
        assert ei.value.kind == "billing" and ei.value.group == "budget" and not ei.value.retryable


def test_org_must_be_verified_has_settings_hint():
    spy = Spy(openai_error(403, "Your organization must be verified to use the model `gpt-image-2.5-flare`."))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    assert ei.value.kind == "permission" and "verified" in ei.value.user_message.lower()


def test_unknown_parameter_is_capability_and_the_param_is_dropped_next_time():
    spy = Spy(openai_error(400, "Unknown parameter: 'user'.", "unknown_parameter", param="user"), ok())
    flags = CapabilityFlags()
    a = make(spy, flags=flags)
    with pytest.raises(ProviderError) as ei:
        a.generate(req(), CTX)
    assert ei.value.kind == "capability" and ei.value.context["param"] == "user"
    assert flags.get("openai.unsupported.user") is True
    a.generate(req(), CTX)
    assert "user" not in spy.bodies[1] and spy.bodies[1]["model"] == MODEL


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (404, "not_found"), (400, "bad_request"), (403, "permission")])
def test_other_status_codes(status, kind):
    with pytest.raises(ProviderError) as ei:
        make(Spy(openai_error(status, "nope"))).generate(req(), CTX)
    assert ei.value.kind == kind and not ei.value.retryable


def test_429_rate_limit_penalizes_and_is_retryable():
    lim = RateLimiter(concurrent=1)
    spy = Spy(openai_error(429, "Rate limit reached", "rate_limit_exceeded", headers={"retry-after": "20"}))
    with pytest.raises(ProviderError) as ei:
        make(spy, limiter=lim).generate(req(), CTX)
    assert ei.value.kind == "rate_limit" and ei.value.retryable and ei.value.retry_after_s == 20.0 and lim._blocked_until > 0


def test_server_errors_are_retried_twice_then_succeed():
    spy = Spy(openai_error(500, "boom"), openai_error(502, "bad gateway"), ok())
    r = make(spy).generate(req(), CTX)
    spy.assert_hit(3)
    assert len(r.images) == 1


def test_server_errors_exhaust_retries():
    spy = Spy(openai_error(500, "boom"), openai_error(500, "boom"), openai_error(500, "boom"))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    spy.assert_hit(3)
    assert ei.value.kind == "server" and ei.value.retryable


def test_timeout_is_not_retried_by_the_adapter_and_flags_possible_double_bill():
    spy = Spy(httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    spy.assert_hit(1)
    e = ei.value
    assert e.kind == "timeout" and e.retryable and e.billed == "unknown" and e.context["possible_double_bill"]


def test_connection_error_is_network():
    spy = Spy(httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as ei:
        make(spy).generate(req(), CTX)
    assert ei.value.kind == "network" and ei.value.retryable


def test_sdk_client_has_no_retries_and_long_timeout():
    a = O.OpenAIImages("sk-test-0000")
    assert a.client.max_retries == 0 and a.client.timeout == 900
    assert "sk-test" not in repr(a)


def test_cost_sink_gets_each_request():
    seen = []
    spy = Spy(ok(n=3), ok(n=2))
    make(spy, cost_sink=seen.append).run_many(req(), 5, CTX)
    assert len(seen) == 2 and all(c["provider"] == "openai" for c in seen)


# ----- probes ---------------------------------------------------------------------------------------------------

def test_probe_sets_flags_from_what_the_api_accepts():
    spy = Spy(ok(size="816x816", usage={"input_tokens": 5, "output_tokens": 196, "total_tokens": 201}), ok(size="816x816"))
    flags = CapabilityFlags()
    res = make(spy, flags=flags).probe()
    spy.assert_hit(2)
    assert res["openai.mask_multi_ok"] is True and res["openai.usage_present"] is True
    assert flags.get("openai.mask_multi_ok") is True and "openai.rgba_image1_ok" in res


def test_probe_records_refusal_of_mask_plus_images():
    spy = Spy(openai_error(400, "Mask is not supported with multiple images", "invalid_value"), ok(size="816x816"))
    flags = CapabilityFlags()
    res = make(spy, flags=flags).probe()
    assert res["openai.mask_multi_ok"] is False and flags.get("openai.mask_multi_ok") is False


def test_estimate_call_scales_with_n():
    assert O.estimate_call(req(n=4, quality="high")) > 3.5 * O.estimate_call(req(n=1, quality="high"))
    assert json.dumps(O.estimate_call(req())) is not None


def test_test_key_is_free_first_then_one_small_image_once():
    models = httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-image-2", "object": "model", "created": 1, "owned_by": "openai"}]})
    spy = Spy(models, ok(), models, models)
    a = make(spy)
    r = a.test_key()
    assert r["ok"] is True and "image model answered" in r["message"] and [q.url.path for q in spy.requests] == ["/v1/models", "/v1/images/generations"]
    assert spy.bodies[1]["size"] == "1024x1024" and spy.bodies[1]["quality"] == "low" and spy.bodies[1]["model"] == MODEL
    r2 = a.test_key()                                              # the paid probe runs once per key
    assert r2["ok"] is True and len(spy.requests) == 3
    assert make(Spy(models)).test_key(paid=False)["ok"] is True


def test_test_key_reports_auth_and_org_verification_in_plain_english():
    bad = make(Spy(openai_error(401, "Incorrect API key provided")))
    r = bad.test_key()
    assert r["ok"] is False and r["kind"] == "auth"
    models = httpx.Response(200, json={"object": "list", "data": []})
    org = make(Spy(models, openai_error(403, "Your organization must be verified to use the model `gpt-image-2.5-flare`.")))
    r2 = org.test_key()
    assert r2["ok"] is False and r2["kind"] == "permission" and "verified" in r2["message"].lower()
