"""MockImages: determinism, drawing, guide edits, FINALIZE, transparency, faults, run_many (APP_SPEC 7.8, 16)."""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image
from prov_helpers import png_bytes

from duoskin.providers.base import CallCtx, ProviderError
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock.images import MockImages
from duoskin.providers.openai_images import ImageRequest, NamedPng, has_real_alpha, valid_size

MODEL = "gpt-image-2.5-flare-2026-09-08"
CTX = CallCtx.null()


def req(**kw) -> ImageRequest:
    d = {"model": MODEL, "prompt": "A flat sticker print, teal and gold.", "size": "1024x1024", "quality": "low", "background": "opaque", "n": 1, **kw}
    return ImageRequest(**d)


def mask_png(w=1024, h=1024, box=(200, 600, 200, 600)) -> bytes:
    m = np.zeros((h, w, 4), np.uint8)
    m[..., 3] = 255
    y0, y1, x0, x1 = box
    m[y0:y1, x0:x1, 3] = 0
    buf = io.BytesIO()
    Image.fromarray(m, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def decode(b: bytes) -> Image.Image:
    return Image.open(io.BytesIO(b))


def test_generate_returns_n_pngs_of_exactly_the_requested_size():
    m = MockImages()
    for size in ("1024x1024", "1536x1024", "816x1632", "2048x1152"):
        r = m.generate(req(size=size, n=3), CTX)
        assert len(r.images) == 3 and r.size == size and r.model == MODEL
        assert all(decode(b).size == tuple(map(int, size.split("x"))) for b in r.images)
        assert r.request_id.startswith("mock-openai-") and len(r.raw_sha256) == 3


def test_outputs_are_a_pure_function_of_the_request():
    a = MockImages().generate(req(nonce="n1", n=2), CTX)
    b = MockImages().generate(req(nonce="n1", n=2), CTX)
    c = MockImages().generate(req(nonce="n2", n=2), CTX)
    d = MockImages().generate(req(prompt="Something else, purple.", n=2), CTX)
    assert a.images == b.images and a.raw_sha256 == b.raw_sha256 and a.request_id == b.request_id
    assert c.raw_sha256 != a.raw_sha256 and c.request_id != a.request_id          # "Reimagine" = a new nonce
    assert d.raw_sha256 != a.raw_sha256


def test_variants_within_one_call_differ():
    r = MockImages().generate(req(n=4, prompt="A sticker in red, blue and green."), CTX)
    assert len(set(r.raw_sha256)) >= 3


def test_prompt_colours_are_used():
    r = MockImages().generate(req(prompt="A crimson star", background="transparent"), CTX)
    arr = np.asarray(decode(r.images[0]).convert("RGBA"))
    opaque = arr[arr[..., 3] == 255][:, :3].mean(axis=0)
    assert opaque[0] > opaque[2] + 40                                     # reddish


def test_transparent_calls_have_real_alpha_and_margin():
    r = MockImages().generate(req(background="transparent", n=2), CTX)
    assert r.alpha_present == [True, True] and not r.warnings
    arr = np.asarray(decode(r.images[0]).convert("RGBA"))
    a = arr[..., 3]
    assert a[0, 0] == 0 and ((a > 0) & (a < 255)).any()                   # anti-aliased edge
    ys, xs = np.nonzero(a > 10)
    assert ys.min() > 40 and xs.min() > 40 and ys.max() < 1024 - 40 and xs.max() < 1024 - 40        # nothing touches the edge


def test_opaque_calls_have_no_alpha():
    r = MockImages().generate(req(), CTX)
    assert r.alpha_present == [False] and decode(r.images[0]).convert("RGBA").getpixel((0, 0))[3] == 255


def test_concept_calls_draw_a_blocky_front_and_back_sheet():
    r = MockImages().generate(req(prompt="A character concept sheet, front and back: red jacket, navy trousers, black hair", size="1536x1024", tag="I1"), CTX)
    arr = np.asarray(decode(r.images[0]).convert("RGB")).astype(int)
    assert decode(r.images[0]).size == (1536, 1024)
    left, right = arr[:, :768], arr[:, 768:]
    red = np.array([200, 45, 50])
    assert (np.abs(left - red).sum(axis=-1) < 30).any() and (np.abs(right - red).sum(axis=-1) < 30).any()      # both views wear the red jacket
    assert not np.array_equal(left, right)                                                                       # the face is only on the front


def test_reimagine_changes_the_concept_picture_not_only_the_skin_tone():
    """"Reimagine" changes only the nonce. The mock used to vary just the skin tone, so a person who pressed it often saw the same picture again
    (found by clicking the app through, tests/e2e_ui): the drafts now differ inside the silhouette too (emblem place, fringe), same colours."""
    prompt = "A character concept sheet, front and back: red jacket, navy trousers, black hair"
    pics = {}
    for nonce in ("a", "b", "c", "d", "e", "f"):
        r = MockImages().generate(req(prompt=prompt, size="1536x1024", tag="I1", nonce=nonce), CTX)
        pics[nonce] = np.asarray(decode(r.images[0]).convert("RGB")).astype(int)
    distinct = {v.tobytes() for v in pics.values()}
    assert len(distinct) >= 5, "six nonces give at least five different pictures"
    base = pics["a"]
    for other in pics.values():                                                                                  # the silhouette (what the guide check reads) stays the same
        bg = np.array([242, 242, 242])
        assert abs(int((np.abs(base - bg).sum(axis=-1) > 36).sum()) - int((np.abs(other - bg).sum(axis=-1) > 36).sum())) < 0.01 * base.shape[0] * base.shape[1]


def test_guide_edit_paints_the_mask_area_and_leaves_everything_else_untouched():
    base = png_bytes(1024, 1024, (240, 240, 240, 255))
    r = MockImages().edit(req(prompt="Fill the area in green.", images=(NamedPng("guide.png", base),), mask=NamedPng("mask.png", mask_png()), tag="I4"), CTX)
    out = np.asarray(decode(r.images[0]).convert("RGBA"))
    ref = np.asarray(decode(base).convert("RGBA"))
    assert (out[200:600, 200:600, :3] != ref[200:600, 200:600, :3]).any(axis=-1).all()
    inv = np.ones((1024, 1024), bool)
    inv[200:600, 200:600] = False
    assert np.array_equal(out[inv], ref[inv])                           # paste-back and the ring check pass because nothing moved
    assert out[400, 400, 1] > out[400, 400, 0]                          # green wins over red


def test_finalize_returns_a_lightly_sharpened_copy_that_keeps_alpha():
    # a mock part picture is one flat colour (nothing to sharpen), so the draft here has an outline and a highlight like a real drawing
    import random

    from duoskin.providers.mock import _draw as D

    draft = D.to_png(D.shape_rgba(1024, 1024, (40, 160, 90), random.Random(1)))
    allz = np.zeros((1024, 1024, 4), np.uint8)
    buf = io.BytesIO()
    Image.fromarray(allz, "RGBA").save(buf, "PNG")
    fin = MockImages().edit(req(prompt="PURPOSE: Final-quality redraw of an approved print.\nIMAGES: Image 1 = approved draft.", background="transparent",
                                images=(NamedPng("draft.png", draft),), mask=NamedPng("m.png", buf.getvalue()), quality="high",
                                model="gpt-image-2.5-sunburst-2026-09-08"), CTX).images[0]
    a, b = np.asarray(decode(draft).convert("RGBA")).astype(int), np.asarray(decode(fin).convert("RGBA")).astype(int)
    assert np.array_equal(a[..., 3], b[..., 3]) and 0 < np.abs(a[..., :3] - b[..., :3]).mean() < 6
    assert has_real_alpha(fin)


def test_reference_role_image1_gives_a_fresh_asset_of_the_requested_size():
    r = MockImages().edit(req(prompt="A heart print, pink", images=(NamedPng("crop.png", png_bytes(600, 400)),), image1_role="reference", size="816x816"), CTX)
    assert decode(r.images[0]).size == (816, 816)


def test_same_preflight_as_the_real_adapter():
    m = MockImages()
    with pytest.raises(ProviderError) as e1:
        m.generate(req(size="585x559"), CTX)
    assert e1.value.code == "illegal_size"
    with pytest.raises(ProviderError) as e2:
        m.generate(req(quality="auto"), CTX)                       # type: ignore[arg-type]
    assert e2.value.code == "bad_quality"
    with pytest.raises(ProviderError) as e3:
        m.generate(req(n=5), CTX)
    assert e3.value.code == "n_exceeds_request_cap"
    with pytest.raises(ProviderError) as e4:
        m.edit(req(images=(NamedPng("a.png", png_bytes(400, 300)),)), CTX)
    assert e4.value.code == "image1_size_mismatch"


def test_mask_rules_follow_the_capability_flags():
    base = png_bytes(1024, 1024, (240, 240, 240, 255))
    m = MockImages()
    r = m.edit(req(images=(NamedPng("a.png", base), NamedPng("b.png", base)), mask=NamedPng("m.png", mask_png())), CTX)
    assert r.adjustments == ["mask_dropped:mask_multi_ok=false"]
    assert m.probe()["openai.mask_multi_ok"] is True and m.flags.get("openai.mask_multi_ok") is True
    r2 = m.edit(req(images=(NamedPng("a.png", base), NamedPng("b.png", base)), mask=NamedPng("m.png", mask_png())), CTX)
    assert r2.adjustments == []


def test_usage_and_cost_are_realistic_and_labelled_mock():
    seen = []
    m = MockImages(cost_sink=seen.append)
    r = m.generate(req(quality="high", n=2), CTX)
    assert r.usage["output_tokens"] == 2 * 1756 and r.usage_present and r.usage["input_tokens_details"]["text_tokens"] > 0
    assert r.cost["provider"] == "mock" and r.cost["usd"] == pytest.approx(2 * 0.0527 + 0.0001, rel=0.1)
    assert seen == m.costs and len(seen) == 1
    assert m.requests[0].provider == "openai" and m.requests[0].operation == "images.generate" and len(m.requests) == 1


def test_run_many_splits_into_balanced_requests_with_nonce_per_batch():
    m = MockImages()
    r = m.run_many(req(nonce="N", prompt="A sticker in red"), 6, CTX)
    assert len(r.images) == 6 and r.batch_sizes == [3, 3] and r.n_total == 6 and len(m.requests) == 2
    assert [b["batch_index"] for b in r.batches] == [0, 1]
    again = MockImages().run_many(req(nonce="N", prompt="A sticker in red"), 6, CTX)
    assert again.raw_sha256 == r.raw_sha256
    m2 = MockImages(ipm=2)
    assert m2.run_many(req(), 5, CTX).batch_sizes == [2, 2, 1]


# ----- faults ------------------------------------------------------------------------------------------------------------

def test_moderation_fault_raises_the_real_error_once():
    m = MockImages(faults=FaultInjector("openai:moderation_blocked@first"))
    with pytest.raises(ProviderError) as ei:
        m.generate(req(), CTX)
    assert ei.value.kind == "moderation" and ei.value.billed == "no" and ei.value.code == "moderation_blocked"
    assert len(m.generate(req(), CTX).images) == 1


def test_size_drift_fault_goes_through_the_real_size_check():
    m = MockImages(faults=FaultInjector("openai:size_drift@I2"))
    assert len(m.generate(req(tag="I1"), CTX).images) == 1
    with pytest.raises(ProviderError) as ei:
        m.generate(req(tag="I2"), CTX)
    assert ei.value.kind == "validation" and ei.value.code == "size_drift" and ei.value.billed == "yes" and ei.value.cost["provider"] == "mock"


def test_timeout_and_other_error_faults():
    m = MockImages(faults=FaultInjector("openai:timeout@I4,openai:insufficient_quota@I5,openai:org_unverified@I6"))
    for tag, kind in (("I4", "timeout"), ("I5", "billing"), ("I6", "permission")):
        with pytest.raises(ProviderError) as ei:
            m.generate(req(tag=tag), CTX)
        assert ei.value.kind == kind


def test_opaque_alpha_and_usage_none_faults_are_for_the_caller_to_detect():
    m = MockImages(faults=FaultInjector("openai:opaque_alpha@first,openai:usage_none@n2"))
    r = m.generate(req(background="transparent"), CTX)
    assert r.alpha_present == [False] and "transparent_requested_but_opaque" in r.warnings
    r2 = m.generate(req(), CTX)
    assert r2.usage is None and not r2.usage_present and r2.cost["basis"] == "estimate"


def test_env_faults_reach_a_mock_without_an_injected_injector(monkeypatch):
    monkeypatch.setenv("DUOSKIN_MOCK_FAULTS", "openai:moderation_blocked@first")
    with pytest.raises(ProviderError):
        MockImages().generate(req(), CTX)
    assert valid_size(1024, 1024)
