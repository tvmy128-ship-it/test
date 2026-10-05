"""OCR and stray-text detection (A_OCR; POL-04, requirement "no text on the assets").

``rapidocr`` (with onnxruntime) is an **optional** dependency. ``detect_text`` uses it when it is installed and its models load;
otherwise it falls back to the glyph-shape detector in ``imaging/glyph.py`` and says so (``engine == "glyph_fallback"``,
``degraded == True``). When neither can run the check **fails closed** (``ran=False``): the pipeline treats that as a failed hard check.

* Any text box with a recognition score >= ``ocr.rec_score_min`` (0.5) and a side >= ``ocr.min_box_px`` (8 px) fails the asset,
  unless the spec contains text (``allow_text=True``).
* Transparent images are checked composited on mid-grey, on white and on black (an OCR model sees nothing on transparent black), and
  small images are upscaled with nearest-neighbour.
* The engine is created lazily and cached; tests inject a fake with ``set_engine``.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import CheckUnavailable, build_result, fail_closed
from duoskin.imaging import files as F
from duoskin.imaging import glyph as G

Engine = Literal["rapidocr", "glyph_fallback", "none"]
_BACKGROUNDS = ((128, 128, 128), (255, 255, 255), (0, 0, 0))
MIN_OCR_SIDE = 320                      # smaller images are upscaled before OCR

_ENGINE: Any = None
_ENGINE_ERROR: str = ""


@dataclass(frozen=True)
class TextBox:
    x0: int
    y0: int
    x1: int
    y1: int
    text: str
    score: float


@dataclass
class OcrResult:
    engine: Engine
    boxes: list[TextBox] = field(default_factory=list)
    degraded: bool = False
    glyph_score: float | None = None
    note: str = ""

    @property
    def found_text(self) -> bool:
        return bool(self.boxes) or (self.glyph_score is not None and self.glyph_score >= float(TH.get("img.glyph_score_max")))


# ------------------------------------------------------------------ engine management
def rapidocr_installed() -> bool:
    """True when a rapidocr package and onnxruntime can be imported (without loading any model)."""
    has_ort = importlib.util.find_spec("onnxruntime") is not None
    return has_ort and (importlib.util.find_spec("rapidocr") is not None or importlib.util.find_spec("rapidocr_onnxruntime") is not None)


def set_engine(engine: Callable[..., Any] | None) -> None:
    """Inject an OCR callable (tests) or reset to lazy loading (``None``). The callable takes an RGB ``ndarray`` and returns what
    rapidocr returns: ``(list[[box, text, score]], elapse)`` (old API) or an object with ``boxes``/``txts``/``scores`` (new API)."""
    global _ENGINE, _ENGINE_ERROR
    _ENGINE = engine
    _ENGINE_ERROR = ""


def get_engine() -> Callable[..., Any] | None:
    """The cached OCR engine, or ``None`` when rapidocr is not installed or its models fail to load."""
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERROR:
        return None
    if not rapidocr_installed():
        _ENGINE_ERROR = "rapidocr or onnxruntime is not installed"
        return None
    try:
        if importlib.util.find_spec("rapidocr") is not None:
            from rapidocr import RapidOCR  # type: ignore[import-not-found]
        else:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found,no-redef]
        _ENGINE = RapidOCR()
    except Exception as e:  # noqa: BLE001 - a missing DLL or model must never crash a check
        _ENGINE_ERROR = f"{type(e).__name__}: {e}"
        return None
    return _ENGINE


def engine_status() -> str:
    """Human-readable state for ``duoskin doctor`` (CHK-S04)."""
    if _ENGINE is not None:
        return "rapidocr ready"
    return _ENGINE_ERROR or ("rapidocr installed, not loaded yet" if rapidocr_installed() else "rapidocr not installed")


# ------------------------------------------------------------------ detection
def _flatten_variants(im: Image.Image) -> list[np.ndarray]:
    rgba = im.convert("RGBA")
    has_alpha = (np.asarray(rgba)[..., 3] < 255).any()
    bgs = _BACKGROUNDS if has_alpha else (_BACKGROUNDS[1],)
    out = []
    for bg in bgs:
        flat = F.flatten(rgba, bg)
        if min(flat.size) < MIN_OCR_SIDE:
            k = -(-MIN_OCR_SIDE // min(flat.size))
            flat = flat.resize((flat.width * k, flat.height * k), Image.Resampling.NEAREST)
        out.append(np.asarray(flat))
    return out


def _parse(result: Any) -> list[tuple[list[list[float]], str, float]]:
    """Normalise both rapidocr result shapes to ``[(box_points, text, score)]``."""
    if result is None:
        return []
    if isinstance(result, tuple) and len(result) == 2 and not hasattr(result, "boxes"):
        result = result[0]                                  # old API: (list, elapse)
    if result is None:
        return []
    if hasattr(result, "boxes"):                            # new API object
        boxes = result.boxes if result.boxes is not None else []
        txts = result.txts if getattr(result, "txts", None) is not None else []
        scores = result.scores if getattr(result, "scores", None) is not None else []
        return [(np.asarray(b).tolist(), str(t), float(s)) for b, t, s in zip(boxes, txts, scores, strict=False)]
    out = []
    for item in result:
        box, text, score = item[0], item[1], item[2]
        out.append((np.asarray(box).tolist(), str(text), float(score)))
    return out


def run_ocr(im: Image.Image, engine: Callable[..., Any]) -> list[TextBox]:
    """Run the engine on each flattened variant and merge the boxes (coordinates mapped back to the input size)."""
    min_score = float(TH.get("ocr.rec_score_min"))
    min_px = float(TH.get("ocr.min_box_px"))
    w0, h0 = im.size
    found: list[TextBox] = []
    for arr in _flatten_variants(im):
        sx, sy = w0 / arr.shape[1], h0 / arr.shape[0]
        for pts, text, score in _parse(engine(arr)):
            if score < min_score or not text.strip():
                continue
            xs = [p[0] * sx for p in pts]
            ys = [p[1] * sy for p in pts]
            x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
            if min(x1 - x0, y1 - y0) < min_px:
                continue
            found.append(TextBox(x0, y0, x1, y1, text, score))
    return _dedupe(found)


def _dedupe(boxes: list[TextBox]) -> list[TextBox]:
    """Merge boxes found on several backgrounds (IoU > 0.5), keeping the highest score."""
    out: list[TextBox] = []
    for b in sorted(boxes, key=lambda b: -b.score):
        for o in out:
            iw = min(o.x1, b.x1) - max(o.x0, b.x0)
            ih = min(o.y1, b.y1) - max(o.y0, b.y0)
            if iw > 0 and ih > 0:
                inter = iw * ih
                union = (o.x1 - o.x0) * (o.y1 - o.y0) + (b.x1 - b.x0) * (b.y1 - b.y0) - inter
                if inter / max(union, 1) > 0.5:
                    break
        else:
            out.append(b)
    return out


def detect_text(im: Image.Image, *, engine: Literal["auto", "rapidocr", "glyph"] = "auto") -> OcrResult:
    """Find text in an image: rapidocr when it works, else the glyph fallback; raises ``CheckUnavailable`` when neither can run.

    ``engine="rapidocr"`` never falls back (strict mode); ``engine="glyph"`` forces the fallback.
    """
    note = "glyph fallback requested; "
    if engine in ("auto", "rapidocr"):
        eng = get_engine()
        if eng is None:
            if engine == "rapidocr":
                raise CheckUnavailable(f"rapidocr unavailable: {engine_status()}")
            note = f"OCR unavailable ({engine_status()}); "
        else:
            try:
                return OcrResult("rapidocr", boxes=run_ocr(im, eng))
            except Exception as e:
                if engine == "rapidocr":
                    raise CheckUnavailable(f"rapidocr failed: {type(e).__name__}: {e}") from e
                note = f"rapidocr failed ({type(e).__name__}); "
    try:
        rep = G.glyph_score(im)
    except Exception as e:
        raise CheckUnavailable(f"neither OCR nor the glyph detector could run: {type(e).__name__}: {e}") from e
    boxes = [TextBox(*b, text="", score=rep.score) for b in rep.boxes()] if rep.score >= float(TH.get("img.glyph_score_max")) else []
    return OcrResult("glyph_fallback", boxes=boxes, degraded=True, glyph_score=rep.score, note=note + "using the glyph-shape detector")


def check_no_text(im: Image.Image, *, allow_text: bool = False, engine: Literal["auto", "rapidocr", "glyph"] = "auto",
                  subject_sha: str = "") -> CheckResult:
    """A_OCR (CHK-A06, HARD, class stray_text): no text box with confidence >= 0.5 unless the spec has text.

    With rapidocr missing the glyph fallback decides and the evidence starts with ``degraded``. With neither available the result is
    ``ran=False`` (fail closed).
    """
    try:
        res = detect_text(im, engine=engine)
    except CheckUnavailable as e:
        return fail_closed("A_OCR", str(e), subject_sha)
    n = len(res.boxes)
    ok = allow_text or n == 0
    label = f"degraded ({res.engine}): " if res.degraded else f"{res.engine}: "
    if res.engine == "glyph_fallback":
        ev = f"{label}glyph score {res.glyph_score:.2f}; {res.note}"
    else:
        shown = ", ".join(f"'{b.text[:12]}' {b.score:.2f}" for b in res.boxes[:4])
        ev = f"{label}{n} text box(es) {shown}"
    return build_result("A_OCR", passed=ok, subject_sha=subject_sha, metric="text_boxes", value=float(n),
                        threshold=TH.describe("ocr.rec_score_min", ">=") + "; 0 boxes allowed", evidence=ev, fix_hint="regenerate")


def check_no_text_any(images: Sequence[Image.Image], **kw: Any) -> CheckResult:
    """A_OCR over several crops (every component crop of an asset); the first failure wins, a not-run counts as not-run."""
    last: CheckResult | None = None
    for im in images:
        last = check_no_text(im, **kw)
        if not last.passed:
            return last
    return last if last is not None else fail_closed("A_OCR", "no images to check")
