"""imaging/glyph.py and imaging/ocr.py: stray-text detection, rapidocr optional, glyph fallback, fail closed."""
from __future__ import annotations

import math

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from duoskin.checks.runner import run_check
from duoskin.imaging import glyph as G
from duoskin.imaging import ocr as O


@pytest.fixture(autouse=True)
def _reset_engine():
    O.set_engine(None)
    yield
    O.set_engine(None)


def text_img(txt, size=64, fg=(20, 20, 20, 255), bg=(0, 0, 0, 0), canvas=(512, 256), pos=(40, 80)):
    im = Image.new("RGBA", canvas, bg)
    ImageDraw.Draw(im).text(pos, txt, font=ImageFont.load_default(size=size), fill=fg)
    return im


def _negatives():
    out = {}
    im = Image.new("RGBA", (512, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([120, 90, 220, 190], fill=(30, 30, 30, 255))
    d.ellipse([290, 90, 390, 190], fill=(30, 30, 30, 255))
    out["two blobs"] = im
    im = Image.new("RGBA", (512, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for cx in (130, 256, 382):
        pts = [(cx + (60 if i % 2 == 0 else 26) * math.cos(math.pi / 2 + i * math.pi / 5), 128 - (60 if i % 2 == 0 else 26) * math.sin(math.pi / 2 + i * math.pi / 5)) for i in range(10)]
        d.polygon(pts, fill=(220, 40, 40, 255))
    out["row of stars"] = im
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([100, 100, 400, 400], fill=(240, 120, 160, 255), outline=(60, 20, 30, 255), width=8)
    d.ellipse([210, 210, 290, 290], fill=(250, 220, 60, 255))
    out["flower"] = im
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([150, 100, 350, 420], fill=(40, 120, 160, 255))
    d.ellipse([220, 200, 280, 300], fill=(10, 10, 10, 255))
    out["eye"] = im
    im = Image.new("RGBA", (512, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).arc([40, 40, 460, 300], 200, 340, fill=(10, 10, 10, 255), width=14)
    out["lash arc"] = im
    im = Image.new("RGBA", (512, 512), (240, 240, 240, 255))
    d = ImageDraw.Draw(im)
    for x in range(20, 500, 40):
        d.rectangle([x, 50, x + 14, 460], fill=(200, 30, 30, 255))
    out["stripes"] = im
    im = Image.new("RGBA", (512, 512), (240, 240, 240, 255))
    d = ImageDraw.Draw(im)
    for x in range(40, 500, 70):
        for y in range(40, 500, 70):
            d.ellipse([x, y, x + 30, y + 30], fill=(30, 30, 160, 255))
    out["polka dots"] = im
    g = np.zeros((512, 512, 4), np.uint8)
    g[..., 3] = 255
    g[..., 0] = np.linspace(0, 255, 512)[None, :]
    g[..., 2] = np.linspace(255, 0, 512)[:, None]
    out["gradient"] = Image.fromarray(g, "RGBA")
    n = np.random.RandomState(0).randint(0, 255, (512, 512, 4)).astype(np.uint8)
    n[..., 3] = 255
    out["noise"] = Image.fromarray(n, "RGBA")
    return out


# ---------------------------------------------------------------- glyph detector
@pytest.mark.parametrize("txt,size", [("HELLO", 64), ("SALE 50%", 48), ("ABC", 30), ("2024", 50), ("MINE", 28)])
@pytest.mark.parametrize("bg", [(0, 0, 0, 0), (240, 240, 240, 255), (30, 60, 120, 255)])
def test_glyph_flags_text_on_any_background(txt, size, bg):
    fg = (20, 20, 20, 255) if bg[0] > 100 or bg[3] == 0 else (250, 250, 250, 255)
    rep = G.glyph_score(text_img(txt, size, fg, bg))
    assert rep.score >= 0.3 and rep.longest_row >= 2
    assert G.check_glyph(text_img(txt, size, fg, bg)).passed is False


def test_glyph_ignores_shapes_that_are_not_text():
    for name, im in _negatives().items():
        rep = G.glyph_score(im)
        assert rep.score < 0.3, name
        assert G.check_glyph(im).passed, name


def test_glyph_score_grows_with_more_letters_and_reports_boxes():
    short = G.glyph_score(text_img("AB", 70))
    long = G.glyph_score(text_img("ABCDEF", 50))
    assert long.score >= short.score and long.score == 1.0
    boxes = long.boxes()
    assert len(boxes) >= 6 and all(0 <= x0 < x1 <= 512 and 0 <= y0 < y1 <= 256 for x0, y0, x1, y1 in boxes)


def test_glyph_large_images_are_downscaled_but_boxes_map_back():
    big = text_img("HELLO", 256, canvas=(2048, 1024), pos=(100, 300))
    rep = G.glyph_score(big)
    assert rep.scale < 1 and rep.score >= 0.3
    assert max(b[2] for b in rep.boxes()) > 800                                      # coordinates are in the original image


def test_glyph_check_result_shape():
    r = G.check_glyph(text_img("HELLO"))
    assert r.check_id == "A_GLYPH" and r.kind == "hard" and r.metric == "glyph_score" and "longest row" in r.evidence


# ---------------------------------------------------------------- OCR with a (fake) rapidocr engine
def fake_old_api(arr):
    return ([[[[10, 10], [200, 10], [200, 50], [10, 50]], "SALE", 0.93]], 0.01)


class NewApiResult:
    def __init__(self):
        self.boxes = [[[10, 10], [200, 10], [200, 50], [10, 50]], [[5, 5], [9, 5], [9, 9], [5, 9]]]
        self.txts = ["HELLO", "x"]
        self.scores = [0.91, 0.99]


def test_rapidocr_old_and_new_result_shapes_are_understood():
    im = text_img("A")
    O.set_engine(fake_old_api)
    r = O.detect_text(im)
    assert r.engine == "rapidocr" and not r.degraded and [b.text for b in r.boxes] == ["SALE"]
    O.set_engine(lambda arr: NewApiResult())
    r = O.detect_text(im)
    assert [b.text for b in r.boxes] == ["HELLO"]                                      # the 4x4 px box is under ocr.min_box_px


def test_low_confidence_and_empty_text_are_ignored():
    O.set_engine(lambda arr: ([[[[0, 0], [50, 0], [50, 30], [0, 30]], "noise", 0.3], [[[0, 0], [50, 0], [50, 30], [0, 30]], "  ", 0.99]], 0.0))
    assert O.check_no_text(text_img("A")).passed
    O.set_engine(lambda arr: ([], 0.0))
    assert O.check_no_text(text_img("A")).passed
    O.set_engine(lambda arr: None)
    assert O.check_no_text(text_img("A")).passed


def test_check_no_text_fails_when_ocr_finds_text_and_allow_text_overrides():
    O.set_engine(fake_old_api)
    r = O.check_no_text(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
    assert not r.passed and r.check_id == "A_OCR" and r.kind == "hard" and "rapidocr" in r.evidence
    assert r.ran and r.value == 1.0
    assert O.check_no_text(Image.new("RGBA", (64, 64), (0, 0, 0, 0)), allow_text=True).passed


def test_ocr_sees_transparent_images_on_several_backgrounds_and_maps_boxes_back():
    seen = []

    def engine(arr):
        seen.append(arr.shape)
        return ([[[[0, 0], [arr.shape[1], 0], [arr.shape[1], arr.shape[0]], [0, arr.shape[0]]], "T", 0.9]], 0.0)

    O.set_engine(engine)
    r = O.detect_text(Image.new("RGBA", (100, 50), (0, 0, 0, 0)))
    assert len(seen) == 3                                                              # grey, white and black composites
    assert min(min(s[:2]) for s in seen) >= O.MIN_OCR_SIDE                             # small images are upscaled
    assert len(r.boxes) == 1 and (r.boxes[0].x1, r.boxes[0].y1) == (100, 50)           # merged and mapped back to the input size
    seen.clear()
    O.detect_text(Image.new("RGB", (400, 400), (255, 255, 255)))
    assert len(seen) == 1


# ---------------------------------------------------------------- degraded fallback and fail-closed
def test_without_rapidocr_the_glyph_fallback_runs_and_is_labelled_degraded():
    assert O.rapidocr_installed() is False or True
    O.set_engine(None)
    # force "not installed" regardless of the environment
    import duoskin.imaging.ocr as mod

    orig = mod.rapidocr_installed
    mod.rapidocr_installed = lambda: False
    try:
        mod._ENGINE_ERROR = ""
        bad = O.check_no_text(text_img("HELLO"))
        assert bad.ran and not bad.passed and bad.evidence.startswith("degraded (glyph_fallback)")
        assert "not installed" in bad.evidence
        good = O.check_no_text(_negatives()["flower"])
        assert good.ran and good.passed and "degraded" in good.evidence
        assert O.engine_status() == "rapidocr or onnxruntime is not installed"
    finally:
        mod.rapidocr_installed = orig
        mod._ENGINE_ERROR = ""


def test_strict_mode_does_not_fall_back():
    import duoskin.imaging.ocr as mod

    orig = mod.rapidocr_installed
    mod.rapidocr_installed = lambda: False
    try:
        mod._ENGINE_ERROR = ""
        r = O.check_no_text(_negatives()["flower"], engine="rapidocr")
        assert not r.ran and not r.passed and r.status == "not_run" and "unavailable" in r.evidence
    finally:
        mod.rapidocr_installed = orig
        mod._ENGINE_ERROR = ""


def test_engine_exception_falls_back_in_auto_but_fails_closed_in_strict_mode():
    def broken(arr):
        raise RuntimeError("onnx session crashed")

    O.set_engine(broken)
    r = O.check_no_text(_negatives()["flower"])
    assert r.ran and r.passed and "degraded" in r.evidence and "RuntimeError" in r.evidence
    strict = O.check_no_text(_negatives()["flower"], engine="rapidocr")
    assert not strict.ran and not strict.passed


def test_neither_ocr_nor_glyph_can_run_fails_closed(monkeypatch):
    def boom(im):
        raise RuntimeError("opencv DLL missing")

    monkeypatch.setattr(G, "glyph_score", boom)
    monkeypatch.setattr(O, "rapidocr_installed", lambda: False)
    O._ENGINE_ERROR = ""
    r = O.check_no_text(text_img("A"))
    assert not r.ran and not r.passed and r.status == "not_run" and "neither OCR nor the glyph detector" in r.evidence
    O._ENGINE_ERROR = ""
    wrapped = run_check("A_OCR", "sha", lambda: O.check_no_text(text_img("A")))
    assert not wrapped.ran and not wrapped.passed


def test_forced_glyph_engine_and_many_crops():
    r = O.detect_text(text_img("HELLO"), engine="glyph")
    assert r.engine == "glyph_fallback" and r.degraded and r.found_text and r.boxes
    crops = [_negatives()["flower"], text_img("HELLO"), _negatives()["eye"]]
    res = O.check_no_text_any(crops, engine="glyph")
    assert not res.passed                                                              # the first failing crop wins
    assert O.check_no_text_any([_negatives()["flower"], _negatives()["eye"]], engine="glyph").passed
    assert not O.check_no_text_any([]).ran
