"""Glyph-shape detector: the stray-text fallback when no OCR engine can run (PROMPT_BIBLE A_GLYPH, FAILURE_MODES POL-04).

It looks for small, thin-stroked, high-contrast marks that sit in text-like rows, without any model:

1. candidate marks = connected components of the foreground (alpha) and of the local-contrast ink (dark and light marks on their
   surroundings, found by an adaptive threshold);
2. a mark is *glyph-like* when its height is between ``ocr.min_box_px`` and a third of the image, its aspect ratio is letter-like,
   it is neither a solid blob nor a hairline (fill ratio) and its strokes are thin compared to its height;
3. glyph-like marks of similar height whose vertical centres align and which follow each other horizontally form a *row*;
4. ``score = min(1, N / 6)`` where ``N`` is the number of marks in the longest row (a row needs at least 2 marks).

The check passes when ``score < img.glyph_score_max`` (0.3). It is deliberately conservative toward flagging (the VLM ``ip_no_text``
rule has the last word) and it is a *fallback*: with rapidocr available, ``imaging/ocr.py`` runs the real detector first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result

MIN_ASPECT, MAX_ASPECT = 0.15, 2.4         # width / height of one glyph
MIN_FILL, MAX_FILL = 0.12, 0.85            # component area / bbox area
MAX_STROKE_RATIO = 0.30                    # stroke width / height
MAX_HEIGHT_FRAC = 0.34                     # of the shorter image side
ROW_HEIGHT_RATIO = (0.55, 1.8)
ROW_CENTRE_TOL = 0.40                      # of the taller glyph height
ROW_GAP = 1.2                              # horizontal gap, in glyph heights
MAX_ANALYSIS_SIDE = 1024                   # larger images are area-downsampled first
ROW_FULL_SCORE = 6                         # members in a row for score 1.0
INK_CONTRAST = 28                          # black-hat / top-hat response (grey levels) that counts as a stroke


@dataclass(frozen=True)
class Mark:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class GlyphReport:
    score: float
    candidates: int
    glyphs: int
    rows: list[list[Mark]] = field(default_factory=list)
    scale: float = 1.0

    @property
    def longest_row(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def boxes(self) -> list[tuple[int, int, int, int]]:
        """Boxes of the marks in text-like rows, in the original image coordinates."""
        s = 1.0 / self.scale
        return [(int(m.x0 * s), int(m.y0 * s), int(m.x1 * s), int(m.y1 * s)) for r in self.rows for m in r]


def _ink_masks(rgba: np.ndarray) -> list[np.ndarray]:
    """Candidate foreground masks: the alpha foreground (a cut-out whose pieces are the marks) and the thin dark / light strokes
    found by black-hat and top-hat transforms at three scales. Large regions and their edges give no response, so only strokes that
    are narrower than the kernel survive, which is exactly what letters are made of."""
    import cv2

    a = rgba[..., 3]
    vis = a >= 128
    flat = rgba[..., :3].astype(np.float32)
    # composite on mid-grey so transparent pixels have no contrast of their own
    mix = flat * (a[..., None] / 255.0) + 128.0 * (1.0 - a[..., None] / 255.0)
    gray = np.clip(0.299 * mix[..., 0] + 0.587 * mix[..., 1] + 0.114 * mix[..., 2], 0, 255).astype(np.uint8)
    short = min(gray.shape)
    masks: list[np.ndarray] = []
    if (a < 128).mean() > 0.02:
        masks.append(vis)
    sizes = sorted({max(5, round(short * f) | 1) for f in (0.012, 0.025, 0.05)})
    inside = cv2.erode(vis.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    for k in sizes:
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        for op in (cv2.MORPH_BLACKHAT, cv2.MORPH_TOPHAT):
            resp = cv2.morphologyEx(gray, op, ker)
            masks.append((resp > INK_CONTRAST) & inside)
    return masks


def _components(mask: np.ndarray) -> list[tuple[Mark, np.ndarray]]:
    from scipy import ndimage as ndi

    lab, n = ndi.label(mask, structure=np.ones((3, 3), bool))
    out = []
    if n == 0:
        return out
    objs = ndi.find_objects(lab)
    for i, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        out.append((Mark(x0, y0, x1, y1), lab[sl] == i))
    return out


def _iou(a: Mark, b: Mark) -> float:
    iw = min(a.x1, b.x1) - max(a.x0, b.x0)
    ih = min(a.y1, b.y1) - max(a.y0, b.y0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / float(a.w * a.h + b.w * b.h - inter)


def _glyph_like(mark: Mark, comp: np.ndarray, short_side: int, min_h: int) -> bool:
    from scipy import ndimage as ndi

    h, w = mark.h, mark.w
    if h < min_h or h > MAX_HEIGHT_FRAC * short_side or w < 2:
        return False
    if not (MIN_ASPECT <= w / h <= MAX_ASPECT):
        return False
    fill = comp.sum() / float(h * w)
    if not (MIN_FILL <= fill <= MAX_FILL):
        return False
    dist = ndi.distance_transform_edt(np.pad(comp, 1))
    stroke = 2.0 * float(dist.max())
    return stroke / h <= MAX_STROKE_RATIO


def _rows(glyphs: list[Mark]) -> list[list[Mark]]:
    n = len(glyphs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = glyphs[i], glyphs[j]
            hmax = max(a.h, b.h)
            ratio = a.h / b.h
            if not (ROW_HEIGHT_RATIO[0] <= ratio <= ROW_HEIGHT_RATIO[1]):
                continue
            if abs(a.cy - b.cy) > ROW_CENTRE_TOL * hmax:
                continue
            gap = max(a.x0, b.x0) - min(a.x1, b.x1)
            if gap > ROW_GAP * hmax:
                continue
            parent[find(i)] = find(j)
    groups: dict[int, list[Mark]] = {}
    for i, g in enumerate(glyphs):
        groups.setdefault(find(i), []).append(g)
    return [sorted(g, key=lambda m: m.x0) for g in groups.values() if len(g) >= 2]


def glyph_score(im: Image.Image) -> GlyphReport:
    """The pseudo-glyph score of an image in [0, 1] (see the module docstring) with the rows that produced it."""
    rgba_im = im.convert("RGBA")
    w0, h0 = rgba_im.size
    scale = 1.0
    if max(w0, h0) > MAX_ANALYSIS_SIDE:
        scale = MAX_ANALYSIS_SIDE / max(w0, h0)
        rgba_im = rgba_im.convert("RGBa").resize((round(w0 * scale), round(h0 * scale)), Image.Resampling.BOX).convert("RGBA")
    rgba = np.asarray(rgba_im)
    short = min(rgba.shape[:2])
    min_h = max(4, round(float(TH.get("ocr.min_box_px")) * scale))
    glyphs: list[Mark] = []
    candidates = 0
    for m in _ink_masks(rgba):
        for mark, comp in _components(m):
            candidates += 1
            if not _glyph_like(mark, comp, short, min_h):
                continue
            if any(_iou(mark, g) > 0.5 for g in glyphs):
                continue                                           # the same mark found at another scale
            glyphs.append(mark)
    rows = _rows(glyphs)
    n = max((len(r) for r in rows), default=0)
    score = 0.0 if n < 2 else min(1.0, n / ROW_FULL_SCORE)
    return GlyphReport(score=float(score), candidates=candidates, glyphs=len(glyphs), rows=rows, scale=scale)


def check_glyph(im: Image.Image, *, subject_sha: str = "") -> CheckResult:
    """A_GLYPH (HARD, class stray_text): no row of text-like marks (score < ``img.glyph_score_max``)."""
    rep = glyph_score(im)
    lim = float(TH.get("img.glyph_score_max"))
    return build_result("A_GLYPH", passed=rep.score < lim, subject_sha=subject_sha, metric="glyph_score", value=rep.score,
                        threshold=TH.describe("img.glyph_score_max", "<"),
                        evidence=f"score {rep.score:.2f}: longest row {rep.longest_row} of {rep.glyphs} glyph-like marks "
                                 f"({rep.candidates} candidates)", fix_hint="regenerate")
