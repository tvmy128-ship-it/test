"""MockRecraft and MockGemini (APP_SPEC 7.8, 16)."""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image
from prov_helpers import png_bytes

from duoskin.providers.base import CallCtx, ProviderError
from duoskin.providers.faults import FaultInjector
from duoskin.providers.gemini import RuleSpec
from duoskin.providers.mock.gemini import MockGemini
from duoskin.providers.mock.recraft import HOSTILE_SVG, MOCK_STYLE_ID, SENTINEL_RGB, MockRecraft
from duoskin.providers.recraft import VectorRequest

CTX = CallCtx.null()


def vreq(**kw) -> VectorRequest:
    d = {"model": "recraftv4_1_vector", "prompt": "1. One round teal iris with a gold ring.", "size": "1024x1024", "n": 1, **kw}
    return VectorRequest(**d)


def svg_ok(b: bytes) -> str:
    import defusedxml.ElementTree as ET
    ET.fromstring(b)
    return b.decode()


# ----- Recraft ------------------------------------------------------------------------------------------------------------

def test_svgs_are_valid_flat_and_on_the_sentinel_background():
    r = MockRecraft().generate(vreq(n=3, tag="R1"), CTX)
    assert len(r.svgs) == 3 and r.image_ids and len(set(r.image_ids)) == 3 and r.credits == 240.0 and r.cost["provider"] == "mock"
    for b in r.svgs:
        text = svg_ok(b)
        for banned in ("<script", "href", "onload", "gradient", "<style", "class=", "<use", "<image", "foreignObject"):
            assert banned not in text, banned
        assert 'fill="#ff00ff"' in text.split("\n")[2]                 # the first shape is the full-size sentinel background
        assert text.count("fill=") >= 5 and 'viewBox="0 0 1024 1024"' in text
    assert len(set(r.svgs)) == 3


def test_background_color_and_colours_are_honoured_and_output_is_deterministic():
    a = MockRecraft().generate(vreq(background_rgb=(0, 255, 0), colors=((10, 20, 30), (200, 100, 50))), CTX)
    b = MockRecraft().generate(vreq(background_rgb=(0, 255, 0), colors=((10, 20, 30), (200, 100, 50))), CTX)
    c = MockRecraft().generate(vreq(background_rgb=(0, 255, 0), colors=((10, 20, 30), (200, 100, 50)), nonce="again"), CTX)
    assert a.svgs == b.svgs and a.svgs[0] != c.svgs[0]
    text = a.svgs[0].decode()
    assert 'fill="#00ff00"' in text and 'fill="#0a141e"' in text and 'fill="#c86432"' in text
    assert SENTINEL_RGB == (255, 0, 255)


def test_styles_model_needs_a_style_id_like_the_real_adapter():
    m = MockRecraft()
    with pytest.raises(ProviderError) as ei:
        m.generate(vreq(model="recraftv4_styles_vector"), CTX)
    assert ei.value.code == "style_id_required"
    sid = m.create_style([png_bytes(64, 64)], model="recraftv4_styles_vector", style="vector_illustration")
    assert sid == MOCK_STYLE_ID and m.styles.kind_of(sid) == "vector"
    r = m.generate(vreq(model="recraftv4_styles_vector", style_id=sid, n=2), CTX)
    assert r.style_id == sid and r.credits == 100.0 and r.cost["usd"] == pytest.approx(0.10)
    with pytest.raises(ProviderError):
        m.create_style([b"<svg/>"], model="recraftv4_styles_vector", style="vector_illustration")
    with pytest.raises(ProviderError):
        m.generate(vreq(size="1:1"), CTX)


def test_vectorize_traces_and_limits_shapes():
    im = Image.new("RGB", (300, 300), (255, 255, 255))
    for k in range(5):
        im.paste((20 * k, 100, 200 - 30 * k), (20 + 50 * k, 20 + 40 * k, 120 + 50 * k, 90 + 40 * k))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    m = MockRecraft()
    full = svg_ok(m.vectorize(buf.getvalue()))
    few = svg_ok(m.vectorize(buf.getvalue(), max_num_shapes=12))
    assert full.count("<rect") > few.count("<rect") and few.count("<rect") <= 12 and "viewBox=\"0 0 300 300\"" in full
    with pytest.raises(ProviderError):
        m.vectorize(png_bytes(100, 100))
    with pytest.raises(ProviderError):
        m.vectorize(buf.getvalue(), max_num_shapes=0)
    assert m.costs[0]["usd"] == pytest.approx(0.01)


def test_remove_background_makes_the_border_connected_backdrop_transparent():
    im = Image.new("RGB", (300, 300), (250, 250, 250))
    im.paste((200, 30, 30), (100, 100, 200, 200))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    out = np.asarray(Image.open(io.BytesIO(MockRecraft().remove_background(buf.getvalue()))).convert("RGBA"))
    assert out[5, 5, 3] == 0 and out[150, 150, 3] == 255 and tuple(out[150, 150, :3]) == (200, 30, 30)


def test_recraft_faults():
    m = MockRecraft(faults=FaultInjector("recraft:429@first,recraft:hostile_svg@R2,recraft:png_instead_of_svg@R3,recraft:style_required@R4"))
    with pytest.raises(ProviderError) as e1:
        m.generate(vreq(), CTX)
    assert e1.value.kind == "rate_limit" and e1.value.retry_after_s == 1.0
    r = m.generate(vreq(tag="R2"), CTX)
    assert r.svgs == [HOSTILE_SVG] and b"<script" in r.svgs[0] and b"onload" in r.svgs[0] and b"foreignObject" in r.svgs[0]
    with pytest.raises(ProviderError) as e3:
        m.generate(vreq(tag="R3"), CTX)
    assert e3.value.code == "not_svg_png" and e3.value.billed == "yes"
    with pytest.raises(ProviderError) as e4:
        m.generate(vreq(tag="R4"), CTX)
    assert e4.value.code == "style_id_required"


def test_recraft_records_requests_and_costs():
    m = MockRecraft()
    m.generate(vreq(), CTX)
    m.remove_background(png_bytes(300, 300))
    assert [r.operation for r in m.requests] == ["images.generate", "images.removeBackground"] and all(c["provider"] == "mock" for c in m.costs)


# ----- Gemini --------------------------------------------------------------------------------------------------------------

RULES = [RuleSpec(rule_id="a", statement="x"), RuleSpec(rule_id="b", statement="y")]


def test_judge_passes_every_rule_in_order():
    m = MockGemini()
    out = m.judge(png_bytes(300, 300), RULES, thinking_level="LOW", ctx=CTX)
    assert [(v.rule_id, v.passed) for v in out] == [("a", True), ("b", True)] and all(v.evidence for v in out)
    assert m.costs[0]["provider"] == "mock" and m.requests[0].operation == "generateContent:judge"


def test_judge_uses_the_real_validation_and_privacy_rules():
    m = MockGemini()
    with pytest.raises(ProviderError) as e1:
        m.judge(png_bytes(300, 300), RULES, thinking_level="MINIMAL", ctx=CTX)       # type: ignore[arg-type]
    assert e1.value.code == "bad_thinking_level"
    with pytest.raises(ProviderError) as e2:
        m.judge(png_bytes(300, 300), RULES, thinking_level="LOW", ctx=CTX, private=True)
    assert e2.value.code == "private_image_unbilled_key"
    assert MockGemini(key_billed=True).judge(png_bytes(300, 300), RULES, thinking_level="LOW", ctx=CTX, private=True)


def test_judge_faults():
    m = MockGemini(faults=FaultInjector("gemini:empty_text@first,gemini:fail_rule@n2"))
    assert [v.passed for v in m.judge(png_bytes(300, 300), RULES, thinking_level="LOW", ctx=CTX)] == [False, False]
    assert [v.passed for v in m.judge(png_bytes(300, 300), RULES, thinking_level="LOW", ctx=CTX)] == [False, True]


def test_image_model_returns_a_jpeg_on_white():
    m = MockGemini()
    data, kind, meta = m.generate(prompt="A teal gem", images=[png_bytes(64, 64)], aspect="16:9", size="1K", ctx=CTX)
    im = Image.open(io.BytesIO(data))
    assert kind == "jpeg" and data[:2] == b"\xff\xd8" and im.mode == "RGB" and im.size == (1024, 576)
    assert im.getpixel((2, 2))[0] > 245 and meta["synthid"] is True and meta["has_alpha"] is False and meta["cost"]["provider"] == "mock"
    again = MockGemini().generate(prompt="A teal gem", images=[png_bytes(64, 64)], aspect="16:9", size="1K", ctx=CTX)
    assert again[0] == data
    tall = MockGemini().generate(prompt="x", images=[], aspect="9:16", size="1K", ctx=CTX)
    assert Image.open(io.BytesIO(tall[0])).size == (576, 1024)


def test_image_model_faults_and_validation():
    m = MockGemini(faults=FaultInjector("gemini:image_safety@first,gemini:recitation@n2,gemini:no_image@n3"))
    for kind in ("moderation", "recitation", "other"):
        with pytest.raises(ProviderError) as ei:
            m.generate(prompt="x", images=[], aspect="1:1", size="1K", ctx=CTX)
        assert ei.value.kind == kind
    with pytest.raises(ProviderError):
        m.generate(prompt="x", images=[], aspect="1:1", size="2K", ctx=CTX)           # type: ignore[arg-type]
