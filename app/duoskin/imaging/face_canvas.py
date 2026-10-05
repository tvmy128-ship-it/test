"""The face lane: 2D face canvas, face parts, layer composer, face checks (APP_SPEC §10.6, PROMPT_BIBLE §4 and §11.3, FACE-01..16).

Two paths, one composer:

* **No head base** (``builtin_kits/face_canvas_default.json``): the face is painted on the cube head's front face. The tile shows the
  composite on 5 skin tones with 2D expression previews (neutral, blink = closed-lid layer, mouth open, happy = raised brows + open
  mouth). The output is a *face layer pack*: one PNG per part and code layer.
* **With a head base** [DEPENDS: kit]: the same composer runs on ``kits/head_base/<variant>/face_canvas.json`` (same schema, same eye-shape
  and mouth ids) and ``imaging/uvwarp.py`` warps the finished texture into the head UV.

Conventions: image-space naming everywhere. "R" is image-right. Eye parts are generated as ``*_imgR`` (outer end at image-right) and the
image-left eye is the **exact mirror** of the right one (``ImageOps.mirror`` about the canvas centre), never a second warp. Highlights are
the exception: they are drawn at the **same image-space offset** in both eyes and never mirrored (FACE-04). ``smirk_side`` is never mirrored.

Layers (canvas-sized RGBA, bottom to top): ``shading``, ``blush``, ``sclera``, ``iris``, ``highlights`` (eyeball group, under the lid),
``lash``, ``lower_ticks``, ``closed_lid`` (lid group), ``brow``, ``nose``, ``mouth_closed``, ``mouth_open``. In the blink preview a
``lid_cover`` (the opening polygon, skin-coloured) is drawn over the eyeball group, so "iris pixels = 0" proves the lid geometry covers it.
Lines are single-colour: every pixel with alpha > 0 carries the feature's exact RGB and only alpha varies.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result
from duoskin.imaging import checks as C
from duoskin.imaging import colournames as CN
from duoskin.imaging import palette as P

DEFAULT_CANVAS_PATH = Path(__file__).resolve().parent.parent / "builtin_kits" / "face_canvas_default.json"
SS = 4                                  # supersampling for every drawn primitive
Expression = Literal["neutral", "blink", "mouth_open", "happy"]
EXPRESSIONS: tuple[Expression, ...] = ("neutral", "blink", "mouth_open", "happy")
LAYER_ORDER = ("shading", "blush", "sclera", "iris", "highlights", "lash", "lower_ticks", "closed_lid", "brow", "nose",
               "mouth_closed", "mouth_open")
EYEBALL_LAYERS = ("sclera", "iris", "highlights")
LINE_LAYERS = ("lash", "lower_ticks", "closed_lid", "brow", "mouth_closed")
FACE_FIELDS_DISTINCT = ("eye_shape", "iris_style", "highlight_style", "lash_style", "brow_style", "mouth_style", "cheek_mark")
WHITE = "#ffffff"
SHADE_HEX = "#0a0a0a"                   # near-black, used with low alpha (FACE-07)
SHADE_ALPHA = 0.22


# ================================================================== canvas data
def _arr(v: Any) -> np.ndarray:
    return np.asarray(v, dtype=np.float64).reshape(-1, 2)


@dataclass(frozen=True)
class EyeShape:
    """One eye-shape variant: opening polygon, iris box, lash arc, closed-lid arc, lid cover and zone (canvas px, image-right eye)."""

    id: str
    opening: np.ndarray
    iris_w: float
    iris_h: float
    iris_cx: float
    iris_cy: float
    lash_arc: np.ndarray
    closed_lid_arc: np.ndarray
    lid_cover: np.ndarray
    lower_edge: np.ndarray
    zone: np.ndarray
    centre: tuple[float, float]
    description: str = ""


@dataclass(frozen=True)
class MouthStyle:
    id: str
    asymmetric: bool
    kind: str
    closed_box: tuple[float, float, float, float]
    open_polygon: np.ndarray
    description: str = ""


@dataclass
class FaceCanvas:
    """A face canvas layout (builtin 2D default, or a head-base variant's ``face_canvas.json`` with the same schema)."""

    size: int
    density: int
    final_px: int
    mirror_x: float
    min_line_px_final: float
    eye_centre: tuple[float, float]
    eye_shapes: dict[str, EyeShape]
    mouths: dict[str, MouthStyle]
    brow: dict[str, Any]
    highlight_offset: tuple[float, float]
    nose_centre: tuple[float, float]
    nose_zone: np.ndarray
    blush: dict[str, Any]
    mouth_centre: tuple[float, float]
    mouth_zone: np.ndarray
    lower_ticks: dict[str, Any]
    kit: dict[str, Any]
    source: str = "builtin"
    sha256: str = ""

    # -------------------------------------------------- kit enums (EyeShapeKit / MouthKit exist before any head base)
    @property
    def eye_shape_kit(self) -> list[str]:
        return sorted(self.eye_shapes)

    @property
    def mouth_kit(self) -> list[str]:
        return sorted(self.mouths)

    def eye(self, shape: str) -> EyeShape:
        try:
            return self.eye_shapes[shape]
        except KeyError:
            raise KeyError(f"unknown eye_shape {shape!r}; kit has {self.eye_shape_kit}") from None

    def mouth(self, style: str) -> MouthStyle:
        try:
            return self.mouths[style]
        except KeyError:
            raise KeyError(f"unknown mouth_style {style!r}; kit has {self.mouth_kit}") from None

    # -------------------------------------------------- mirroring
    def mirror_points(self, pts: np.ndarray) -> np.ndarray:
        out = np.array(pts, dtype=np.float64, copy=True)
        out[:, 0] = 2.0 * self.mirror_x - out[:, 0]
        return out

    def eye_centre_l(self) -> tuple[float, float]:
        return 2.0 * self.mirror_x - self.eye_centre[0], self.eye_centre[1]

    # -------------------------------------------------- zones (FACE-02 2D equivalent: the "landmark zones" of the default canvas)
    def zone_polygons(self, eye_shape: str) -> dict[str, np.ndarray]:
        e = self.eye(eye_shape)
        bz = _arr(self.brow["zone"])
        return {
            "eye_R": e.zone, "eye_L": self.mirror_points(e.zone),
            "brow_R": bz, "brow_L": self.mirror_points(bz),
            "nose": self.nose_zone, "mouth": self.mouth_zone,
            "blush_R": _arr(self.blush["zone_R"]), "blush_L": self.mirror_points(_arr(self.blush["zone_R"])),
        }

    def zone_mask(self, eye_shape: str, ss: int = 1) -> np.ndarray:
        """Bool ``(size, size)`` union of the landmark zones."""
        m = np.zeros((self.size, self.size), bool)
        for poly in self.zone_polygons(eye_shape).values():
            m |= _poly_bool(self.size, poly)
        return m

    def validate(self) -> list[str]:
        """Problems that would make this canvas unusable by the composer (empty list = valid)."""
        p: list[str] = []
        if abs(self.mirror_x - self.size / 2) > 1e-6:
            p.append("mirror_x must be the canvas centre (the left eye is an exact image mirror)")
        for need in ("narrow", "round", "sleepy"):
            if need not in self.eye_shapes:
                p.append(f"missing eye shape {need}")
        for need in ("smile_line", "cat_w", "smirk_side", "flat_line", "small_o", "open_grin", "fang_smile"):
            if need not in self.mouths:
                p.append(f"missing mouth style {need}")
        for e in self.eye_shapes.values():
            if len(e.opening) < 8:
                p.append(f"{e.id}: opening polygon too short")
        return p


def _poly_bool(size: int, poly: np.ndarray) -> np.ndarray:
    im = Image.new("L", (size, size), 0)
    ImageDraw.Draw(im).polygon([(float(x), float(y)) for x, y in poly], fill=255)
    return np.asarray(im) > 0


def _build_canvas(data: Mapping[str, Any], source: str, sha: str) -> FaceCanvas:
    cx0, cy0 = data["eye_centre_R"]
    shapes: dict[str, EyeShape] = {}
    for sid, e in data["eye_shapes"].items():
        off = np.array([cx0, cy0])
        iris = e["iris"]
        shapes[sid] = EyeShape(
            id=sid, opening=_arr(e["opening"]) + off, iris_w=float(iris["w"]), iris_h=float(iris["h"]),
            iris_cx=cx0 + float(iris.get("dx", 0.0)), iris_cy=cy0 + float(iris.get("dy", 0.0)),
            lash_arc=_arr(e["lash_arc"]) + off, closed_lid_arc=_arr(e["closed_lid_arc"]) + off, lid_cover=_arr(e["lid_cover"]) + off,
            lower_edge=_arr(e["lower_edge"]) + off, zone=_arr(e["zone"]) + off, centre=(cx0, cy0), description=e.get("description", ""))
    mouths = {mid: MouthStyle(mid, bool(m["asymmetric"]), m["kind"], tuple(float(v) for v in m["closed_box"]),   # type: ignore[arg-type]
                              _arr(m["open"]), m.get("description", "")) for mid, m in data["mouth"]["styles"].items()}
    cv = FaceCanvas(
        size=int(data["canvas_px"]), density=int(data["density"]), final_px=int(data["final_texel_px"]), mirror_x=float(data["mirror_x"]),
        min_line_px_final=float(data["min_line_px_final"]), eye_centre=(float(cx0), float(cy0)), eye_shapes=shapes, mouths=mouths,
        brow=dict(data["brow"]), highlight_offset=tuple(data["highlight"]["offset"]),   # type: ignore[arg-type]
        nose_centre=tuple(data["nose"]["centre"]), nose_zone=_arr(data["nose"]["zone"]),   # type: ignore[arg-type]
        blush=dict(data["blush"]), mouth_centre=tuple(data["mouth"]["centre"]), mouth_zone=_arr(data["mouth"]["zone"]),   # type: ignore[arg-type]
        lower_ticks=dict(data["lower_ticks"]), kit=dict(data["kit"]), source=source, sha256=sha)
    bad = cv.validate()
    if bad:
        raise ValueError(f"invalid face canvas {source}: {'; '.join(bad)}")
    return cv


@lru_cache(maxsize=8)
def _load_cached(path: str) -> FaceCanvas:
    import hashlib

    raw = Path(path).read_bytes()
    return _build_canvas(json.loads(raw.decode("utf-8")), path, hashlib.sha256(raw).hexdigest())


def load_canvas(path: str | Path | None = None) -> FaceCanvas:
    """Load a face canvas: ``kits/head_base/<variant>/face_canvas.json`` when given, else the builtin 2D default."""
    return _load_cached(str(path or DEFAULT_CANVAS_PATH))


def eye_shape_kit(path: str | Path | None = None) -> list[str]:
    """The ``EyeShapeKit`` values (``models/kitenums.py`` builds the enum from this until a head base provides its own)."""
    return load_canvas(path).eye_shape_kit


def mouth_kit(path: str | Path | None = None) -> list[str]:
    """The ``MouthKit`` values."""
    return load_canvas(path).mouth_kit


# ================================================================== drawing helpers (supersampled, bbox-local)
class _Pen:
    """Draws anti-aliased shapes into a local ``L`` mask: coordinates are canvas px, drawing happens at ``SS`` times the size."""

    def __init__(self, bbox: tuple[int, int, int, int]):
        self.x0, self.y0, x1, y1 = bbox
        self.w, self.h = max(1, x1 - self.x0), max(1, y1 - self.y0)
        self.img = Image.new("L", (self.w * SS, self.h * SS), 0)
        self.d = ImageDraw.Draw(self.img)

    def p(self, pt: Sequence[float]) -> tuple[float, float]:
        return (float(pt[0]) - self.x0) * SS, (float(pt[1]) - self.y0) * SS

    def polygon(self, pts: Iterable[Sequence[float]], fill: int = 255) -> None:
        self.d.polygon([self.p(q) for q in pts], fill=fill)

    def polyline(self, pts: Sequence[Sequence[float]], width: float, fill: int = 255) -> None:
        q = [self.p(x) for x in pts]
        w = max(1, round(width * SS))
        self.d.line(q, fill=fill, width=w, joint="curve")
        r = w / 2.0
        for cx, cy in (q[0], q[-1]):
            self.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, fill: int = 255) -> None:
        x, y = self.p((cx, cy))
        self.d.ellipse([x - rx * SS, y - ry * SS, x + rx * SS, y + ry * SS], fill=fill)

    def mask(self) -> np.ndarray:
        """``(h, w) float32`` coverage in [0, 1] at canvas resolution."""
        small = self.img.resize((self.w, self.h), Image.Resampling.BOX)
        return np.asarray(small, dtype=np.float32) / 255.0


def _bbox_of_points(pts: np.ndarray, pad: float, size: int) -> tuple[int, int, int, int]:
    x0 = max(0, math.floor(pts[:, 0].min() - pad))
    y0 = max(0, math.floor(pts[:, 1].min() - pad))
    x1 = min(size, math.ceil(pts[:, 0].max() + pad))
    y1 = min(size, math.ceil(pts[:, 1].max() + pad))
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


def _layer(size: int, bbox: tuple[int, int, int, int], coverage: np.ndarray, hex_colour: str, alpha: float = 1.0) -> Image.Image:
    """A canvas-sized RGBA layer: flat ``hex_colour`` RGB everywhere that alpha > 0, alpha = coverage * ``alpha``."""
    arr = np.zeros((size, size, 4), np.uint8)
    x0, y0 = bbox[0], bbox[1]
    h, w = coverage.shape
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(size, x0 + w), min(size, y0 + h)
    if sx1 <= sx0 or sy1 <= sy0:
        return Image.fromarray(arr, "RGBA")
    cov = coverage[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
    r, g, b = P.hex_to_rgb(hex_colour)
    a = np.clip(np.rint(cov * alpha * 255.0), 0, 255).astype(np.uint8)
    sub = arr[sy0:sy1, sx0:sx1]
    on = a > 0
    sub[..., 0][on], sub[..., 1][on], sub[..., 2][on] = r, g, b
    sub[..., 3] = a
    return Image.fromarray(arr, "RGBA")


def _empty(size: int) -> Image.Image:
    return Image.new("RGBA", (size, size), (0, 0, 0, 0))


def _mirror(layer: Image.Image) -> Image.Image:
    return ImageOps.mirror(layer)


def _both(layer_r: Image.Image) -> Image.Image:
    """Right-eye layer plus its exact image mirror."""
    return Image.alpha_composite(layer_r, _mirror(layer_r))


def _poly_coverage(size: int, poly: np.ndarray, pad: float = 2.0) -> tuple[tuple[int, int, int, int], np.ndarray]:
    bbox = _bbox_of_points(poly, pad, size)
    pen = _Pen(bbox)
    pen.polygon(poly)
    return bbox, pen.mask()


def _crop_alpha(img: Image.Image) -> Image.Image | None:
    bb = C.bbox_of(C.alpha_of(img) >= 8)
    return None if bb is None else img.convert("RGBA").crop(bb)


def _resize_pm(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    w, h = max(1, size[0]), max(1, size[1])
    return img.convert("RGBa").resize((w, h), Image.Resampling.BOX if w <= img.width else Image.Resampling.LANCZOS).convert("RGBA")


def _paste_centred(canvas_size: int, part: Image.Image, cx: float, cy: float) -> Image.Image:
    out = _empty(canvas_size)
    out.alpha_composite(part, (round(cx - part.width / 2), round(cy - part.height / 2)))
    return out


def _clip(layer: Image.Image, poly: np.ndarray, size: int) -> Image.Image:
    bbox, cov = _poly_coverage(size, poly, pad=2.0)
    m = np.zeros((size, size), np.float32)
    m[bbox[1]:bbox[1] + cov.shape[0], bbox[0]:bbox[0] + cov.shape[1]] = cov
    arr = np.array(layer, dtype=np.float32)
    arr[..., 3] *= m
    return Image.fromarray(np.rint(arr).astype(np.uint8), "RGBA")


def _flat_colour(layer: Image.Image, hex_colour: str) -> Image.Image:
    """Every pixel with alpha > 0 gets exactly ``hex_colour`` (anti-aliased edges keep the RGB, only alpha varies)."""
    arr = np.array(layer.convert("RGBA"), dtype=np.uint8)
    on = arr[..., 3] > 0
    arr[on, 0], arr[on, 1], arr[on, 2] = P.hex_to_rgb(hex_colour)
    arr[~on, :3] = 0
    return Image.fromarray(arr, "RGBA")


def _scale_alpha(layer: Image.Image, k: float) -> Image.Image:
    arr = np.array(layer, dtype=np.float32)
    arr[..., 3] *= k
    return Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8), "RGBA")


def _shift(layer: Image.Image, dx: int, dy: int) -> Image.Image:
    out = _empty(layer.width)
    out.alpha_composite(layer, (dx, dy))
    return out


# ================================================================== arc-length warp (lash, brow, closed-lid line)
def warp_along_polyline(part: Image.Image, poly: np.ndarray, canvas_size: int, *, length_scale: float = 1.0) -> Image.Image:
    """Place an ``*_imgR`` part along a polyline (image-right eye, inner end first) by an arc-length warp.

    The part's x axis follows the arc length, its y axis the normal (up = outwards for the lash). The part is first resized (area
    filter) so that its width equals ``length_scale`` times the arc length, then sampled bilinearly, so the result is not aliased.
    The part's vertical reference is the median body centre of its central columns. Returns a canvas-sized RGBA layer.
    """
    import cv2
    from scipy.spatial import cKDTree

    crop = _crop_alpha(part)
    if crop is None:
        return _empty(canvas_size)
    pts = np.asarray(poly, dtype=np.float64)
    seg = np.hypot(*np.diff(pts, axis=0).T)
    total = float(seg.sum())
    if total < 4:
        return _empty(canvas_size)
    k = total * length_scale / crop.width
    scaled = _resize_pm(crop, (round(crop.width * k), round(crop.height * k)))
    bw, bh = scaled.size
    a = np.asarray(scaled, dtype=np.float32)
    alpha = a[..., 3] / 255.0
    cols = slice(int(bw * 0.15), max(int(bw * 0.15) + 1, int(bw * 0.85)))
    ys = np.arange(bh)[:, None]
    wsum = alpha[:, cols].sum(axis=0)
    cen = (alpha[:, cols] * ys).sum(axis=0) / np.maximum(wsum, 1e-6)
    y_ref = float(np.median(cen[wsum > 0])) if (wsum > 0).any() else bh / 2.0
    # dense samples of the polyline plus straight extensions at both ends
    ext = max(0.0, (bw - total) / 2.0) + 4.0
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s_grid = np.arange(-ext, total + ext + 1.0, 1.0)
    px = np.interp(s_grid, cum, pts[:, 0])
    py = np.interp(s_grid, cum, pts[:, 1])
    t0 = (pts[1] - pts[0]) / max(np.hypot(*(pts[1] - pts[0])), 1e-9)
    t1 = (pts[-1] - pts[-2]) / max(np.hypot(*(pts[-1] - pts[-2])), 1e-9)
    lo = s_grid < 0
    hi = s_grid > total
    px[lo] = pts[0, 0] + t0[0] * s_grid[lo]
    py[lo] = pts[0, 1] + t0[1] * s_grid[lo]
    px[hi] = pts[-1, 0] + t1[0] * (s_grid[hi] - total)
    py[hi] = pts[-1, 1] + t1[1] * (s_grid[hi] - total)
    samp = np.stack([px, py], 1)
    tang = np.gradient(samp, axis=0)
    tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]
    nrm = np.stack([tang[:, 1], -tang[:, 0]], 1)          # up for a left-to-right tangent (image y points down)
    reach = max(y_ref, bh - y_ref) + 3.0
    x0 = max(0, math.floor(samp[:, 0].min() - reach))
    y0 = max(0, math.floor(samp[:, 1].min() - reach))
    x1 = min(canvas_size, math.ceil(samp[:, 0].max() + reach))
    y1 = min(canvas_size, math.ceil(samp[:, 1].max() + reach))
    gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
    q = np.stack([gx.ravel(), gy.ravel()], 1)
    _dist, idx = cKDTree(samp).query(q)
    along = ((q - samp[idx]) * tang[idx]).sum(1)
    d_up = ((q - samp[idx]) * nrm[idx]).sum(1)
    s = s_grid[idx] + along
    u = s - (total / 2.0) + bw / 2.0 - 0.5
    v = y_ref - d_up - 0.5
    ok = (np.abs(along) < 2.0) | ((idx > 0) & (idx < len(samp) - 1))
    ok &= (u > -1) & (u < bw) & (v > -1) & (v < bh)
    mx = np.where(ok, u, -10.0).astype(np.float32).reshape(gx.shape)
    my = np.where(ok, v, -10.0).astype(np.float32).reshape(gx.shape)
    pre = a.copy()
    pre[..., :3] *= alpha[..., None]
    out = cv2.remap(pre, mx, my, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    oa = out[..., 3]
    rgb = np.where(oa[..., None] > 1e-3, out[..., :3] * 255.0 / np.maximum(oa[..., None], 1e-6), 0.0)
    res = np.zeros((canvas_size, canvas_size, 4), np.uint8)
    res[y0:y1, x0:x1, :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    res[y0:y1, x0:x1, 3] = np.clip(np.rint(oa), 0, 255).astype(np.uint8)
    return Image.fromarray(res, "RGBA")


# ================================================================== the face spec and parts
@dataclass
class FaceSpec:
    """The ``Face`` block of a spec with its palette references resolved to hex (``None`` for ``"none"``)."""

    eye_shape: str = "round"
    iris_style: str = "oval_solid"
    highlight_style: str = "dual_dot"
    lash_style: str = "clean_line"
    brow_style: str = "thin_arched"
    mouth_style: str = "smile_line"
    nose_style: str = "none"
    cheek_mark: str = "none"
    default_expression: str = "neutral"
    iris: str = "#3b6fb5"
    iris_dark: str = "#22335c"
    pupil: str = "#2b2b33"
    sclera: str = "#fafafa"
    lash: str = "#2b2b33"
    brow: str = "#3b2a22"
    mouth_line: str = "#2b2b33"
    mouth_inner: str = "#7a2e35"
    tongue: str = "#f2735e"
    teeth: str | None = "#fafafa"
    blush: str | None = None

    @classmethod
    def from_spec(cls, face: Mapping[str, Any], palette: Mapping[str, str]) -> FaceSpec:
        """Build from the spec's ``face`` dict and a ``palette id -> hex`` map (the ``*_ref`` fields)."""
        def ref(key: str) -> str | None:
            v = face.get(key)
            if v in (None, "", "none"):
                return None
            if v not in palette:
                raise KeyError(f"{key} references unknown palette id {v!r}")
            return P.normalise_hex(palette[v])

        kw: dict[str, Any] = {k: face[k] for k in ("eye_shape", "iris_style", "highlight_style", "lash_style", "brow_style", "mouth_style",
                                                    "nose_style", "cheek_mark", "default_expression") if k in face}
        for field_name, key in (("iris", "iris_ref"), ("iris_dark", "iris_dark_ref"), ("pupil", "pupil_ref"), ("sclera", "sclera_ref"),
                                ("lash", "lash_ref"), ("brow", "brow_ref"), ("mouth_line", "mouth_line_ref"), ("mouth_inner", "mouth_inner_ref"),
                                ("tongue", "tongue_ref"), ("teeth", "teeth_ref"), ("blush", "blush_ref")):
            if key in face:
                kw[field_name] = ref(key)
        return cls(**kw)

    def feature_grammar(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in FACE_FIELDS_DISTINCT}


@dataclass
class FaceParts:
    """The generated parts (R1 / I3 outputs, or code-parametric fallbacks). A missing part is drawn by code."""

    iris_imgR: Image.Image | None = None
    lash_upper_imgR: Image.Image | None = None
    closed_lid_line_imgR: Image.Image | None = None
    brow_imgR: Image.Image | None = None
    mouth_closed: Image.Image | None = None
    mouth_open: Image.Image | None = None
    sources: dict[str, str] = field(default_factory=dict)      # part -> "ai" | "code"


# ------------------------------------------------------------------ code-parametric parts (the last rung of the route)
def _part_canvas(size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (size[0] * SS, size[1] * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _finish(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return img.convert("RGBa").resize(size, Image.Resampling.BOX).convert("RGBA")


def _line_mask_part(size: tuple[int, int], shapes: Sequence[tuple[str, Any, float]], hex_colour: str) -> Image.Image:
    """A single-colour part: ``shapes`` are ``("line", pts, width)``, ``("poly", pts, 0)`` or ``("ellipse", (cx, cy, rx, ry), 0)``."""
    m = Image.new("L", (size[0] * SS, size[1] * SS), 0)
    d = ImageDraw.Draw(m)
    for kind, data, width in shapes:
        if kind == "poly":
            d.polygon([(x * SS, y * SS) for x, y in data], fill=255)
        elif kind == "ellipse":
            cx, cy, rx, ry = data
            d.ellipse([(cx - rx) * SS, (cy - ry) * SS, (cx + rx) * SS, (cy + ry) * SS], fill=255)
        else:
            q = [(x * SS, y * SS) for x, y in data]
            w = max(1, round(width * SS))
            d.line(q, fill=255, width=w, joint="curve")
            r = w / 2
            for cx, cy in (q[0], q[-1]):
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    cov = np.asarray(m.resize(size, Image.Resampling.BOX), dtype=np.float32) / 255.0
    arr = np.zeros((size[1], size[0], 4), np.uint8)
    r, g, b = P.hex_to_rgb(hex_colour)
    arr[..., 0], arr[..., 1], arr[..., 2] = r, g, b
    arr[..., 3] = np.rint(cov * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _taper_polygon(centre: np.ndarray, widths: np.ndarray) -> list[tuple[float, float]]:
    """Outline of a variable-width stroke along ``centre`` (N, 2) with half-widths ``widths / 2`` (N,)."""
    tang = np.gradient(centre, axis=0)
    tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]
    nrm = np.stack([-tang[:, 1], tang[:, 0]], 1)
    top = centre + nrm * (widths / 2)[:, None]
    bot = centre - nrm * (widths / 2)[:, None]
    return [tuple(p) for p in np.vstack([top, bot[::-1]])]


def parametric_iris(spec: FaceSpec, size: int = 512) -> Image.Image:
    """The iris part for ``spec.iris_style``: a full oval (the lid covers the excess), flat colours, no highlights."""
    w, h = size * 0.86, size * 0.94
    cx, cy = size / 2, size / 2
    img, d = _part_canvas((size, size))

    def oval(rx: float, ry: float, colour: str, dy: float = 0.0) -> None:
        d.ellipse([(cx - rx) * SS, (cy + dy - ry) * SS, (cx + rx) * SS, (cy + dy + ry) * SS], fill=P.hex_to_rgb(colour) + (255,))

    st = spec.iris_style
    if st == "round_small_pupil":
        r = size * 0.36
        oval(r, r, spec.iris)
        oval(r * 0.3, r * 0.3, spec.pupil)
        return _finish(img, (size, size))
    oval(w / 2, h / 2, spec.iris)
    if st == "oval_top_band":
        band = Image.new("L", img.size, 0)
        ImageDraw.Draw(band).rectangle([0, 0, img.width, (cy - h / 2 + h / 3) * SS], fill=255)
        dark = Image.new("RGBA", img.size, P.hex_to_rgb(spec.iris_dark) + (255,))
        shape = Image.new("L", img.size, 0)
        ImageDraw.Draw(shape).ellipse([(cx - w / 2) * SS, (cy - h / 2) * SS, (cx + w / 2) * SS, (cy + h / 2) * SS], fill=255)
        mask = Image.fromarray(np.minimum(np.asarray(band), np.asarray(shape)), "L")
        img.paste(dark, (0, 0), mask)
    elif st == "oval_two_step":
        half = Image.new("L", img.size, 0)
        ImageDraw.Draw(half).rectangle([0, 0, img.width, cy * SS], fill=255)
        dark = Image.new("RGBA", img.size, P.hex_to_rgb(spec.iris_dark) + (255,))
        shape = Image.new("L", img.size, 0)
        ImageDraw.Draw(shape).ellipse([(cx - w / 2) * SS, (cy - h / 2) * SS, (cx + w / 2) * SS, (cy + h / 2) * SS], fill=255)
        img.paste(dark, (0, 0), Image.fromarray(np.minimum(np.asarray(half), np.asarray(shape)), "L"))
    elif st == "oval_ring":
        oval(w / 2, h / 2, spec.iris_dark)
        oval(w / 2 * 0.74, h / 2 * 0.76, spec.iris)
    # pupil
    if st == "vertical_slit":
        oval(w * 0.09, h * 0.36, spec.pupil)
    else:
        oval(w * 0.22, h * 0.25, spec.pupil)
    return _finish(img, (size, size))


def parametric_lash(spec: FaceSpec, size: tuple[int, int] = (1024, 512)) -> Image.Image:
    """The upper-lash part (``lash_upper_imgR``): one colour, inner end image-left, outer end (and flicks) image-right. Chunky by design."""
    w, h = size
    t = np.linspace(0, 1, 40)
    x = w * (0.10 + 0.80 * t)
    base = h * 0.62 - np.sin(np.pi * t) * h * 0.10 + 0.0 * t
    centre = np.stack([x, base], 1)
    shapes: list[tuple[str, Any, float]] = []
    st = spec.lash_style
    if st == "heavy_line_lower_ticks":
        widths = 96 - 40 * np.abs(2 * t - 1) ** 2
    elif st == "clean_line":
        widths = 70 - 38 * np.abs(2 * t - 1) ** 3
    else:
        widths = 66 - 30 * np.abs(2 * t - 1) ** 3
    shapes.append(("poly", _taper_polygon(centre, widths), 0))
    ox, oy = centre[-1]
    if st == "outer_flick_1":
        shapes.append(("poly", [(ox - 60, oy - 20), (ox + 80, oy - 120), (ox + 40, oy + 10), (ox - 40, oy + 36)], 0))
    elif st == "outer_flicks_3":
        for ang, ln in ((-62, 130), (-34, 150), (-8, 130)):
            a = math.radians(ang)
            tip = (ox - 30 + math.cos(a) * ln, oy + math.sin(a) * ln)
            shapes.append(("poly", [(ox - 70, oy - 18), tip, (ox - 20 + math.cos(a + 0.35) * 30, oy + 40)], 0))
    elif st == "wing":
        shapes.append(("poly", [(ox - 90, oy - 24), (ox + 70, oy - 190), (ox + 44, oy - 20), (ox - 30, oy + 44)], 0))
    return _line_mask_part(size, shapes, spec.lash)


def parametric_closed_lid_line(spec: FaceSpec, size: tuple[int, int] = (1024, 512)) -> Image.Image:
    """The closed-lid line: the lash colour and taper, curved downward like a smile (code-parametric, bible §4.2)."""
    w, h = size
    t = np.linspace(0, 1, 40)
    centre = np.stack([w * (0.10 + 0.80 * t), h * 0.40 + np.sin(np.pi * t) * h * 0.14], 1)
    widths = 66 - 30 * np.abs(2 * t - 1) ** 3
    return _line_mask_part(size, [("poly", _taper_polygon(centre, widths), 0)], spec.lash)


def parametric_brow(spec: FaceSpec, size: tuple[int, int] = (1024, 512)) -> Image.Image:
    """The brow part (``brow_imgR``): thick inner end at image-left, thin outer point at image-right."""
    w, h = size
    st = spec.brow_style
    n = 40
    t = np.linspace(0, 1, n)
    length = {"short_round": 0.55}.get(st, 0.82)
    x = w * (0.10 + length * t)
    y = np.full(n, h * 0.5)
    if st == "thin_arched":
        y = h * 0.55 - np.sin(np.pi * t) * h * 0.16
        widths = 90 - 66 * t ** 1.4
    elif st == "straight_thick":
        widths = 130 - 56 * t
    elif st == "short_round":
        y = h * 0.52 - np.sin(np.pi * t) * h * 0.08
        widths = 120 - 40 * t ** 3
    elif st == "angled_up":
        y = h * 0.66 - t * h * 0.26
        widths = 110 - 62 * t
    else:   # soft_worried: inner end raised, outer end low
        y = h * 0.34 + t * h * 0.24
        widths = 100 - 50 * t
    widths = np.maximum(widths, 36)
    return _line_mask_part(size, [("poly", _taper_polygon(np.stack([x, y], 1), widths), 0)], spec.brow)


def parametric_mouth_closed(spec: FaceSpec, size: int = 1024) -> Image.Image:
    """The closed mouth as one-colour line art centred in a square frame (``smirk_side`` keeps its asymmetry)."""
    s = size
    w = 62.0
    st = spec.mouth_style
    shapes: list[tuple[str, Any, float]] = []
    cx, cy = s / 2, s / 2
    t = np.linspace(-1, 1, 41)
    if st == "smile_line":
        pts = np.stack([cx + t * s * 0.36, cy - s * 0.06 + (1 - t ** 2) * s * 0.10], 1)
        shapes.append(("line", pts, w))
    elif st == "cat_w":
        left = np.stack([cx - s * 0.17 + (t + 1) / 2 * s * 0.17, cy - s * 0.02 + np.sin((t + 1) / 2 * np.pi) * s * 0.10], 1)
        right = np.stack([cx + (t + 1) / 2 * s * 0.17, cy - s * 0.02 + np.sin((t + 1) / 2 * np.pi) * s * 0.10], 1)
        shapes += [("line", left, w), ("line", right, w)]
    elif st == "smirk_side":
        u = (t + 1) / 2
        pts = np.stack([cx - s * 0.30 + u * s * 0.62, cy + s * 0.02 - u ** 2.2 * s * 0.12 + np.sin(u * np.pi) * s * 0.05], 1)
        shapes.append(("line", pts, w))
    elif st == "flat_line":
        shapes.append(("line", np.array([[cx - s * 0.30, cy], [cx + s * 0.30, cy]]), w))
    elif st == "small_o":
        a = np.linspace(0, 2 * np.pi, 48)
        shapes.append(("line", np.stack([cx + np.cos(a) * s * 0.16, cy + np.sin(a) * s * 0.16], 1), w))
    elif st == "open_grin":
        pts = np.stack([cx + t * s * 0.42, cy - s * 0.10 + (1 - t ** 2) * s * 0.16], 1)
        shapes.append(("line", pts, w + 10))
    else:   # fang_smile
        pts = np.stack([cx + t * s * 0.38, cy - s * 0.08 + (1 - t ** 2) * s * 0.12], 1)
        shapes.append(("line", pts, w))
        for sx in (-0.14, 0.14):
            x0 = cx + sx * s
            y0 = cy - s * 0.08 + (1 - (sx / 0.38) ** 2) * s * 0.12
            shapes.append(("poly", [(x0 - 38, y0), (x0 + 38, y0), (x0, y0 + 120)], 0))
    return _line_mask_part((s, s), shapes, spec.mouth_line)


def parametric_mouth_open(spec: FaceSpec, canvas: FaceCanvas, size: int = 1024) -> Image.Image:
    """The open-mouth interior (not lips, so several colours are allowed): inner colour, tongue, optional teeth, in a square frame."""
    poly = canvas.mouth(spec.mouth_style).open_polygon
    c = poly.mean(0)
    span = float(max(np.ptp(poly[:, 0]), np.ptp(poly[:, 1])))
    k = size * 0.84 / span
    pts = (poly - c) * k + size / 2
    img, _d = _part_canvas((size, size))
    shape = Image.new("L", img.size, 0)
    ImageDraw.Draw(shape).polygon([(x * SS, y * SS) for x, y in pts], fill=255)
    base = Image.new("RGBA", img.size, P.hex_to_rgb(spec.mouth_inner) + (255,))
    layers: list[tuple[Image.Image, Image.Image]] = [(base, shape)]
    top, bot = float(pts[:, 1].min()), float(pts[:, 1].max())
    left, right = float(pts[:, 0].min()), float(pts[:, 0].max())
    tongue = Image.new("L", img.size, 0)
    ImageDraw.Draw(tongue).ellipse([(left + (right - left) * 0.22) * SS, (bot - (bot - top) * 0.46) * SS,
                                    (right - (right - left) * 0.22) * SS, (bot + (bot - top) * 0.20) * SS], fill=255)
    layers.append((Image.new("RGBA", img.size, P.hex_to_rgb(spec.tongue) + (255,)), Image.fromarray(np.minimum(np.asarray(tongue), np.asarray(shape)), "L")))
    if spec.teeth:
        teeth = Image.new("L", img.size, 0)
        ImageDraw.Draw(teeth).rectangle([(left + (right - left) * 0.12) * SS, top * SS, (right - (right - left) * 0.12) * SS, (top + (bot - top) * 0.26) * SS], fill=255)
        layers.append((Image.new("RGBA", img.size, P.hex_to_rgb(spec.teeth) + (255,)), Image.fromarray(np.minimum(np.asarray(teeth), np.asarray(shape)), "L")))
    for colour_img, mask in layers:
        img.paste(colour_img, (0, 0), mask)
    return _finish(img, (size, size))


def parametric_parts(spec: FaceSpec, canvas: FaceCanvas) -> FaceParts:
    """Every face part drawn by code (the last rung of the face route, and the fixture for tests)."""
    return FaceParts(iris_imgR=parametric_iris(spec), lash_upper_imgR=parametric_lash(spec), closed_lid_line_imgR=parametric_closed_lid_line(spec),
                     brow_imgR=parametric_brow(spec), mouth_closed=parametric_mouth_closed(spec), mouth_open=parametric_mouth_open(spec, canvas),
                     sources={k: "code" for k in ("iris_imgR", "lash_upper_imgR", "closed_lid_line_imgR", "brow_imgR", "mouth_closed", "mouth_open")})


def fill_missing_parts(parts: FaceParts | None, spec: FaceSpec, canvas: FaceCanvas) -> FaceParts:
    """Parts as given, with every missing one drawn by code."""
    code = parametric_parts(spec, canvas)
    if parts is None:
        return code
    out = FaceParts(sources=dict(parts.sources))
    for name in ("iris_imgR", "lash_upper_imgR", "closed_lid_line_imgR", "brow_imgR", "mouth_closed", "mouth_open"):
        given = getattr(parts, name)
        setattr(out, name, given if given is not None else getattr(code, name))
        out.sources.setdefault(name, "ai" if given is not None else "code")
    return out


# ================================================================== code layers
def _thicken_to(layer: Image.Image, hex_colour: str, min_px: float) -> Image.Image:
    """Normalise line width (bible C4 step 4): if the thinnest sustained stroke is under ``min_px``, dilate until it is not."""
    import cv2

    mask = C.alpha_of(layer) >= 128
    if not mask.any():
        return layer
    have = C.min_stroke_px(mask, cap=math.ceil(min_px) + 2)
    if have >= min_px:
        return layer
    r = math.ceil((min_px - have) / 2.0)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    a = np.asarray(C.alpha_of(layer))
    grown = cv2.dilate(a, k)
    arr = np.zeros((*a.shape, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = P.hex_to_rgb(hex_colour)
    arr[..., 3] = np.maximum(a, grown)
    return Image.fromarray(arr, "RGBA")


def _highlight_layer(canvas: FaceCanvas, e: EyeShape, style: str, centre: tuple[float, float]) -> Image.Image:
    """Catchlight for one eye at the **same image-space offset** from the iris centre (white only, never mirrored).

    Returned unclipped: the composer clips it to the eye opening, the catchlight check measures the designed offset."""
    size = canvas.size
    if style == "none_matte":
        return _empty(size)
    iw, ih = e.iris_w, e.iris_h
    ox, oy = canvas.highlight_offset
    cx, cy = centre
    bbox = (int(cx - iw), int(cy - ih), int(cx + iw), int(cy + ih))
    pen = _Pen(bbox)
    big = (cx + ox * iw, cy + oy * ih)
    r = 0.16 * iw
    if style == "single_large":
        pen.ellipse(big[0], big[1], r * 1.2, r * 1.2)
    elif style == "dual_dot":
        pen.ellipse(big[0], big[1], r, r)
        pen.ellipse(cx + 0.16 * iw, cy + 0.14 * ih, r * 0.5, r * 0.5)
    elif style == "triple_dot":
        pen.ellipse(big[0], big[1], r, r)
        pen.ellipse(cx + 0.18 * iw, cy + 0.12 * ih, r * 0.55, r * 0.55)
        pen.ellipse(cx + 0.02 * iw, cy + 0.30 * ih, r * 0.38, r * 0.38)
    elif style == "sparkle_star":
        rr = r * 1.5
        pts = []
        for i in range(8):
            ang = math.pi / 4 * i - math.pi / 2
            rad = rr if i % 2 == 0 else rr * 0.38
            pts.append((big[0] + math.cos(ang) * rad, big[1] + math.sin(ang) * rad))
        pen.polygon(pts)
        pen.ellipse(cx + 0.16 * iw, cy + 0.14 * ih, r * 0.45, r * 0.45)
    elif style == "crescent_rim":
        pen.ellipse(cx, cy, iw * 0.40, ih * 0.40, 255)
        pen.ellipse(cx + iw * 0.10, cy + ih * 0.10, iw * 0.37, ih * 0.37, 0)
    else:
        raise KeyError(f"unknown highlight_style {style!r}")
    return _layer(size, bbox, pen.mask(), WHITE)


def _nose_layer(canvas: FaceCanvas, spec: FaceSpec) -> Image.Image:
    size = canvas.size
    cx, cy = canvas.nose_centre
    st = spec.nose_style
    if st == "none":
        return _empty(size)
    bbox = (int(cx - 40), int(cy - 40), int(cx + 40), int(cy + 40))
    pen = _Pen(bbox)
    if st == "dot":
        pen.ellipse(cx, cy, 11, 11)
        return _flat_colour(_layer(size, bbox, pen.mask(), spec.mouth_line), spec.mouth_line)
    if st == "tiny_hook":
        pen.polyline([(cx - 14, cy - 14), (cx - 14, cy + 6), (cx + 6, cy + 14)], 12)
        return _flat_colour(_layer(size, bbox, pen.mask(), spec.mouth_line), spec.mouth_line)
    # shadow_tick: skin-tone shading, near-black at low alpha
    pen.polyline([(cx - 4, cy - 18), (cx - 12, cy + 12)], 14)
    return _layer(size, bbox, pen.mask(), SHADE_HEX, SHADE_ALPHA)


def _blush_layer(canvas: FaceCanvas, spec: FaceSpec) -> Image.Image:
    size = canvas.size
    if spec.cheek_mark == "none" or not spec.blush:
        return _empty(size)
    cx, cy = canvas.blush["centre_R"]
    rx, ry = float(canvas.blush["rx"]), float(canvas.blush["ry"])
    bbox = (int(cx - rx - 8), int(cy - ry - 8), int(cx + rx + 8), int(cy + ry + 8))
    pen = _Pen(bbox)
    if spec.cheek_mark == "blush_soft":
        pen.ellipse(cx, cy, rx, ry)
    elif spec.cheek_mark == "blush_hatch":
        for i in range(3):
            x = cx - rx * 0.55 + i * rx * 0.55
            pen.polyline([(x, cy + ry * 0.55), (x + rx * 0.30, cy - ry * 0.55)], 14)
    else:
        raise KeyError(f"unknown cheek_mark {spec.cheek_mark!r}")
    return _both(_layer(size, bbox, pen.mask(), spec.blush, min(0.35, float(TH.get("face.blush_alpha_max")))))


def _shading_layer(canvas: FaceCanvas, e: EyeShape) -> Image.Image:
    """Near-black, low-alpha lid shadow just above each eye opening (always darkens, FACE-07)."""
    size = canvas.size
    top = e.lash_arc
    bbox = _bbox_of_points(top, 40, size)
    pen = _Pen(bbox)
    up = top + np.array([0.0, -26.0])
    pen.polyline([tuple(p) for p in up], 30)
    lay = _layer(size, bbox, pen.mask(), SHADE_HEX, SHADE_ALPHA)
    lay = _clip_out(lay, e.opening, size)
    return _both(lay)


def _clip_out(layer: Image.Image, poly: np.ndarray, size: int) -> Image.Image:
    bbox, cov = _poly_coverage(size, poly, pad=2.0)
    m = np.ones((size, size), np.float32)
    m[bbox[1]:bbox[1] + cov.shape[0], bbox[0]:bbox[0] + cov.shape[1]] -= cov
    arr = np.array(layer, dtype=np.float32)
    arr[..., 3] *= m
    return Image.fromarray(np.rint(arr).astype(np.uint8), "RGBA")


def _lower_ticks_layer(canvas: FaceCanvas, e: EyeShape, spec: FaceSpec) -> Image.Image:
    if spec.lash_style != "heavy_line_lower_ticks":
        return _empty(canvas.size)
    size = canvas.size
    n = int(canvas.lower_ticks["count"])
    ln = float(canvas.lower_ticks["len"])
    low = e.lower_edge
    bbox = _bbox_of_points(low, ln + 20, size)
    pen = _Pen(bbox)
    for i in range(n):
        idx = int(len(low) * (0.70 + 0.14 * i))
        px, py = low[min(idx, len(low) - 1)]
        pen.polyline([(px, py + 4), (px + ln * 0.35, py + ln)], 12)
    return _both(_flat_colour(_layer(size, bbox, pen.mask(), spec.lash), spec.lash))


# ================================================================== the composite
@dataclass
class FaceComposite:
    """The result of ``compose_face``: canvas-sized layers plus what is needed to render previews and run the checks."""

    canvas: FaceCanvas
    spec: FaceSpec
    parts: FaceParts
    layers: dict[str, Image.Image]
    eye: EyeShape
    iris_centre_r: tuple[float, float]
    iris_centre_l: tuple[float, float]
    highlights_r: Image.Image
    highlights_l: Image.Image
    lid_cover: Image.Image            # preview-only: both opening polygons (inflated), opaque
    sclera_poly_mask: np.ndarray      # bool, both eyes: the sclera polygon the lid layer must cover

    # -------------------------------------------------- states
    def _stack(self, names: Iterable[str]) -> Image.Image:
        out = _empty(self.canvas.size)
        for n in names:
            out.alpha_composite(self.layers[n])
        return out

    def state_layer_names(self, state: Expression) -> list[str]:
        base = ["shading", "blush", "sclera", "iris", "highlights", "lash", "lower_ticks", "brow", "nose"]
        if state == "neutral":
            return base + ["mouth_closed"]
        if state == "blink":
            return ["shading", "blush", "sclera", "iris", "highlights", "closed_lid", "brow", "nose", "mouth_closed"]
        if state in ("mouth_open", "happy"):
            return base + ["mouth_open"]
        raise KeyError(state)

    def _brow_for(self, state: Expression) -> Image.Image:
        if state == "happy":
            return _shift(self.layers["brow"], 0, -int(self.canvas.brow["happy_lift_px"]))
        return self.layers["brow"]

    def texture(self, state: Expression = "neutral") -> Image.Image:
        """The paint for one state on a transparent skin (what becomes the head texture). The blink variant has no eyeball or lash
        paint, only the closed-lid line: the **closed-lid layer** of the head base takes over."""
        names = self.state_layer_names(state)
        if state == "blink":
            names = [n for n in names if n not in EYEBALL_LAYERS]
        out = _empty(self.canvas.size)
        for n in names:
            out.alpha_composite(self._brow_for(state) if n == "brow" else self.layers[n])
        return out

    def preview(self, skin_hex: str, state: Expression = "neutral", *, scale: float = 1.0) -> Image.Image:
        """Opaque preview on a skin tone. In the blink state the eyeball layers are drawn first and then covered by the lid cover
        (the skin-coloured opening polygon), so any leak shows."""
        skin = P.hex_to_rgb(skin_hex)
        base = Image.new("RGBA", (self.canvas.size,) * 2, skin + (255,))
        names = self.state_layer_names(state)
        for n in names:
            if n == "closed_lid":
                cover = _flat_colour(self.lid_cover, skin_hex)
                base.alpha_composite(cover)
            base.alpha_composite(self._brow_for(state) if n == "brow" else self.layers[n])
        if scale != 1.0:
            sz = round(self.canvas.size * scale)
            base = base.resize((sz, sz), Image.Resampling.BOX if scale < 1 else Image.Resampling.NEAREST)
        return base

    def layer_pack(self) -> dict[str, Image.Image]:
        """The face layer pack of the no-head-base path: one PNG per part and code layer (``face_layers\\``)."""
        return dict(self.layers)

    def feature_alpha(self, state: Expression = "neutral") -> np.ndarray:
        return C.alpha_of(self.texture(state))


def compose_face(spec: FaceSpec, parts: FaceParts | None = None, canvas: FaceCanvas | None = None, *, normalise_lines: bool = True) -> FaceComposite:
    """Place the parts on the canvas and add the code layers (APP_SPEC §10.6 C4, steps 1-5).

    ``parts`` may be partial (missing ones are drawn by code). Right-eye layers are built once and mirrored; highlights are drawn
    separately in both eyes at the same image-space offset; line features are made single-colour and at least
    ``face.stroke_px_min`` px wide at the final texel density.
    """
    cv = canvas or load_canvas()
    size = cv.size
    e = cv.eye(spec.eye_shape)
    mouth = cv.mouth(spec.mouth_style)
    pp = fill_missing_parts(parts, spec, cv)
    min_line = float(TH.get("face.stroke_px_min")) * cv.density
    layers: dict[str, Image.Image] = {}

    # ---- eyeball group: sclera polygon, iris clipped to the opening
    bbox, cov = _poly_coverage(size, e.opening)
    layers["sclera"] = _both(_layer(size, bbox, cov, spec.sclera))
    iris_c = (e.iris_cx, e.iris_cy)
    crop = _crop_alpha(pp.iris_imgR)  # type: ignore[arg-type]
    if crop is None:
        raise ValueError("the iris part is empty")
    k = e.iris_w / crop.width
    iris_img = _resize_pm(crop, (round(crop.width * k), round(crop.height * k)))
    layers["iris"] = _both(_clip(_paste_centred(size, iris_img, *iris_c), e.opening, size))
    ic_l = (2 * cv.mirror_x - iris_c[0], iris_c[1])
    hr = _highlight_layer(cv, e, spec.highlight_style, iris_c)
    hl = _highlight_layer(cv, e, spec.highlight_style, ic_l)
    layers["highlights"] = Image.alpha_composite(_clip(hr, e.opening, size), _clip(hl, cv.mirror_points(e.opening), size))

    # ---- lid group
    lash = warp_along_polyline(pp.lash_upper_imgR, e.lash_arc, size, length_scale=1.10)  # type: ignore[arg-type]
    lash = _flat_colour(lash, spec.lash)
    closed = warp_along_polyline(pp.closed_lid_line_imgR, e.closed_lid_arc, size, length_scale=1.06)  # type: ignore[arg-type]
    closed = _flat_colour(closed, spec.lash)
    if normalise_lines:
        lash = _thicken_to(lash, spec.lash, min_line)
        closed = _thicken_to(closed, spec.lash, min_line)
    layers["lash"] = _both(lash)
    layers["closed_lid"] = _both(closed)
    layers["lower_ticks"] = _lower_ticks_layer(cv, e, spec)

    # ---- brows: placed on the baseline of the right eye, mirrored
    bl = _arr(cv.brow["baseline"])
    base_pts = np.array([[cv.eye_centre[0] + x, cv.eye_centre[1] + float(cv.brow["centre_dy"]) + y] for x, y in bl])
    brow = warp_along_polyline(pp.brow_imgR, base_pts, size, length_scale=1.0)  # type: ignore[arg-type]
    brow = _flat_colour(brow, spec.brow)
    if normalise_lines:
        brow = _thicken_to(brow, spec.brow, min_line)
    layers["brow"] = _both(brow)

    # ---- mouths
    x0, y0, x1, y1 = mouth.closed_box
    mc = pp.mouth_closed
    mcrop = _crop_alpha(mc)  # type: ignore[arg-type]
    if mcrop is None:
        raise ValueError("the closed mouth part is empty")
    ks = (x1 - x0) / mcrop.width
    mimg = _resize_pm(mcrop, (round(mcrop.width * ks), round(mcrop.height * ks)))
    mlayer = _flat_colour(_paste_centred(size, mimg, (x0 + x1) / 2, (y0 + y1) / 2), spec.mouth_line)
    layers["mouth_closed"] = _thicken_to(mlayer, spec.mouth_line, min_line) if normalise_lines else mlayer
    ocrop = _crop_alpha(pp.mouth_open)  # type: ignore[arg-type]
    if ocrop is None:
        raise ValueError("the open mouth part is empty")
    poly = mouth.open_polygon
    px0, py0, px1, py1 = poly[:, 0].min(), poly[:, 1].min(), poly[:, 0].max(), poly[:, 1].max()
    ko = min((px1 - px0) / ocrop.width, (py1 - py0) / ocrop.height) if ocrop.width and ocrop.height else 1.0
    oimg = _resize_pm(ocrop, (round(ocrop.width * ko), round(ocrop.height * ko)))
    layers["mouth_open"] = _clip(_paste_centred(size, oimg, (px0 + px1) / 2, (py0 + py1) / 2), poly, size)

    # ---- the rest of the code layers
    layers["nose"] = _nose_layer(cv, spec)
    layers["blush"] = _blush_layer(cv, spec)
    layers["shading"] = _shading_layer(cv, e)
    ordered = {n: layers[n] for n in LAYER_ORDER}

    cover_r = _poly_cover(cv, e.lid_cover)
    lid_cover = _both(cover_r)
    sclera_mask = np.asarray(C.alpha_of(ordered["sclera"])) >= 128
    return FaceComposite(cv, spec, pp, ordered, e, iris_c, ic_l, hr, hl, lid_cover, sclera_mask)


def _poly_cover(cv: FaceCanvas, poly: np.ndarray) -> Image.Image:
    bbox, cov = _poly_coverage(cv.size, poly, pad=3.0)
    return _layer(cv.size, bbox, cov, "#ffffff")


# ================================================================== tone sheet
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # Pillow without the sized default font
        return ImageFont.load_default()


def tone_sheet(comp: FaceComposite, *, expressions: Sequence[Expression] = EXPRESSIONS, tones: Sequence[CN.SkinTone] | None = None,
               cell: int = 256, label: bool = True) -> Image.Image:
    """The Gate 2 face tile for the no-head-base path: ``expressions`` as rows x the 5 skin tones as columns (labels drawn by code,
    after all checks). Cells are area-downsampled previews; the badge text says it is a 2D preview."""
    ts = list(tones or CN.load_skin_tones())
    pad = 8
    head = 34 if label else 0
    w = len(ts) * (cell + pad) + pad
    h = head + len(expressions) * (cell + pad) + pad
    sheet = Image.new("RGB", (w, h), (242, 242, 242))
    d = ImageDraw.Draw(sheet)
    f = _font(15)
    if label:
        d.text((pad, 8), "2D preview - no head base", fill=(60, 60, 60), font=f)
    for r, ex in enumerate(expressions):
        for c, t in enumerate(ts):
            im = comp.preview(t.hex, ex).convert("RGB").resize((cell, cell), Image.Resampling.BOX)
            x, y = pad + c * (cell + pad), head + pad + r * (cell + pad)
            sheet.paste(im, (x, y))
            if label:
                d.text((x + 6, y + 4), f"{ex} / {t.id}", fill=(255, 255, 255) if P.luminance_y(np.array(P.hex_to_rgb(t.hex))) < 0.3 else (20, 20, 20), font=_font(13))
    return sheet


def tone_contrast(comp: FaceComposite, tones: Sequence[CN.SkinTone] | None = None) -> dict[str, dict[str, float]]:
    """Per tone: CIEDE2000 between each line feature's colour and the skin tone (the numbers behind ``check_line_skin``)."""
    out: dict[str, dict[str, float]] = {}
    for t in tones or CN.load_skin_tones():
        out[t.id] = {name: P.de2000_hex(col, t.hex) for name, col in _line_colours(comp).items()}
    return out


def _line_colours(comp: FaceComposite) -> dict[str, str]:
    s = comp.spec
    out = {"lash": s.lash, "brow": s.brow, "mouth_closed": s.mouth_line}
    if s.nose_style in ("dot", "tiny_hook"):
        out["nose"] = s.mouth_line
    return out


# ================================================================== checks (Gate 2 face row)
def _tones(tones: Sequence[CN.SkinTone] | None) -> list[CN.SkinTone]:
    return list(tones or CN.load_skin_tones())


def check_line_colours(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_LINE_COLOURS (FACE-01, HARD): each line feature (lash, lower ticks, closed-lid line, brow, closed mouth, nose) has exactly one
    opaque colour with edge RGB intact, the shading overlay stays near-black at alpha <= 0.35, and no opaque paint lies outside the
    landmark zones."""
    problems: list[str] = []
    for name in LINE_LAYERS + (("nose",) if comp.spec.nose_style in ("dot", "tiny_hook") else ()):
        lay = comp.layers[name]
        if not (C.alpha_of(lay) > 0).any():
            continue
        cols, wrong = C.distinct_interior_colours(lay, tol=2)
        if len(cols) != int(TH.get("face.line_colours")) or wrong:
            problems.append(f"{name}: {len(cols)} colours, {wrong} stray edge px")
    sh = np.asarray(comp.layers["shading"])
    if sh[..., 3].max() > 255 * float(TH.get("face.lid_shade_alpha_max")) + 1:
        problems.append("shading alpha above the limit")
    zone = comp.canvas.zone_mask(comp.spec.eye_shape)
    opaque = np.asarray(comp.texture("neutral"))[..., 3] >= 250
    outside = int((opaque & ~P.dilate(zone, 1)).sum())
    if outside:
        problems.append(f"{outside} opaque px outside the zones")
    return build_result("F_LINE_COLOURS", passed=not problems, subject_sha=subject_sha, metric="line_colours_problems", value=float(len(problems)),
                        threshold=TH.describe("face.line_colours", "=="), evidence="; ".join(problems) or "all line features are single-colour",
                        fix_hint="code_palette_snap")


def check_zones(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_ZONES (FACE-02, 2D equivalent): every feature pixel of every state lies inside its canvas landmark zone."""
    zone = P.dilate(comp.canvas.zone_mask(comp.spec.eye_shape), 1)
    worst = 1.0
    bad = []
    for st in EXPRESSIONS:
        a = comp.feature_alpha(st) > 0
        inside = float((a & zone).sum() / max(1, a.sum()))
        worst = min(worst, inside)
        if inside < float(TH.get("face.zone_inside_min")):
            bad.append(f"{st}: {inside:.4f}")
    return build_result("F_ZONES", passed=not bad, subject_sha=subject_sha, metric="inside_zone_share", value=worst,
                        threshold=TH.describe("face.zone_inside_min", ">="), evidence="; ".join(bad) or f"all feature pixels inside the zones ({worst:.3f})",
                        fix_hint="code_recrop")


def check_lid_covers(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_LID_COVERS (FACE-03, 2D equivalent of the blink render): in the blink preview the lid cover covers the sclera polygon
    (>= ``face.lid_cover_min``) and **no iris-coloured pixel** remains in the eye zones."""
    cv = comp.canvas
    sp = comp.spec
    skin = P.choose_sentinel([sp.iris, sp.iris_dark, sp.sclera, sp.pupil], min_de=20.0) or "#00ff00"   # a "skin" that cannot be mistaken for the eye
    blink = np.asarray(comp.preview(skin, "blink"))
    cov = np.asarray(C.alpha_of(comp.lid_cover)) >= 128
    sclera = comp.sclera_poly_mask
    cover_share = float((cov & sclera).sum() / max(1, sclera.sum()))
    eye_zone = np.zeros_like(sclera)
    for key in ("eye_R", "eye_L"):
        eye_zone |= _poly_bool(cv.size, cv.zone_polygons(comp.spec.eye_shape)[key])
    lab = P.srgb_to_lab(blink[..., :3][eye_zone])
    iris_px = 0
    for h in {comp.spec.iris, comp.spec.iris_dark, comp.spec.sclera}:
        iris_px += int((P.deltaE2000(lab, P.hex_to_lab(h)) <= 6.0).sum())
    ok = cover_share >= float(TH.get("face.lid_cover_min")) and iris_px <= int(TH.get("face.blink_iris_px_max"))
    return build_result("F_LID_COVERS", passed=ok, subject_sha=subject_sha, metric="lid_cover_share", value=cover_share,
                        threshold=TH.describe("face.lid_cover_min", ">="),
                        evidence=f"cover {cover_share:.4f} of the sclera polygon; {iris_px} iris/eye-white px left in the blink preview",
                        fix_hint="code_recrop")


def highlight_offsets(comp: FaceComposite) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Centroid of the highlight paint of each eye relative to its iris centre, ``(dx, dy)`` in image space (``None`` if no highlight)."""
    out = []
    for lay, c in ((comp.highlights_r, comp.iris_centre_r), (comp.highlights_l, comp.iris_centre_l)):
        a = C.alpha_of(lay).astype(np.float64)
        if a.sum() == 0:
            out.append(None)
            continue
        ys, xs = np.mgrid[:a.shape[0], :a.shape[1]]
        out.append((float((xs * a).sum() / a.sum() + 0.5 - c[0]), float((ys * a).sum() / a.sum() + 0.5 - c[1])))
    return out[0], out[1]


def check_catchlight_sign(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_CATCHLIGHT (FACE-04, ASSERT): the highlight offset has the same sign in image space in both eyes (never mirrored), and the
    highlight paint is pure white."""
    r, l = highlight_offsets(comp)
    if r is None and l is None:
        return build_result("F_CATCHLIGHT", passed=True, subject_sha=subject_sha, metric="highlight_dx", evidence="no highlights (none_matte)")
    if r is None or l is None:
        return build_result("F_CATCHLIGHT", passed=False, subject_sha=subject_sha, metric="highlight_dx", evidence="only one eye has a highlight")
    tol = float(TH.get("face.catchlight_tol_px"))
    same_sign = (r[0] < 0) == (l[0] < 0) and (r[1] < 0) == (l[1] < 0)
    agree = abs(r[0] - l[0]) <= max(tol, 2.0) and abs(r[1] - l[1]) <= max(tol, 2.0)
    white = True
    for lay in (comp.highlights_r, comp.highlights_l):
        arr = np.asarray(lay)
        on = arr[..., 3] > 0
        white &= bool((arr[on, :3] == 255).all()) if on.any() else True
    ok = same_sign and agree and white
    return build_result("F_CATCHLIGHT", passed=ok, subject_sha=subject_sha, metric="highlight_dx_r_minus_l", value=float(r[0] - l[0]),
                        threshold=TH.describe("face.catchlight_tol_px", "<="),
                        evidence=f"offsets R {r[0]:.1f},{r[1]:.1f} L {l[0]:.1f},{l[1]:.1f}; same sign {same_sign}; white {white}")


def check_side_naming(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_SIDE_NAMING (FACE-09 / PRM-11, ASSERT): the right-eye parts follow the image-space naming (lash flick mass in the right half,
    brow's thick inner end in the left half), the eyes are placed by image coordinates (R eye centre right of the mirror axis), and the
    left eye layers are exact mirrors of the right ones."""
    cv = comp.canvas
    problems = []
    if comp.spec.lash_style not in ("clean_line", "heavy_line_lower_ticks"):
        crop = _crop_alpha(comp.parts.lash_upper_imgR)  # type: ignore[arg-type]
        if crop is not None:
            a = C.alpha_of(crop).astype(np.float64)
            cx = (np.mgrid[:a.shape[0], :a.shape[1]][1] * a).sum() / max(a.sum(), 1)
            if cx <= crop.width / 2:
                problems.append("lash flick mass is not in the image-right half")
    bcrop = _crop_alpha(comp.parts.brow_imgR)  # type: ignore[arg-type]
    if bcrop is not None:
        a = C.alpha_of(bcrop).astype(np.float64)
        cx = (np.mgrid[:a.shape[0], :a.shape[1]][1] * a).sum() / max(a.sum(), 1)
        if cx >= bcrop.width / 2:
            problems.append("brow thick end is not at image-left")
    if not comp.iris_centre_r[0] > cv.mirror_x > comp.iris_centre_l[0]:
        problems.append("eyes are not placed by image coordinates")
    for name in ("sclera", "iris", "lash", "closed_lid", "brow", "blush", "shading"):
        lay = comp.layers[name]
        if not np.array_equal(_canon(lay), _canon(_mirror(lay))):
            problems.append(f"{name} is not left-right symmetric")
    if comp.spec.mouth_style != "smirk_side" and not np.array_equal(_canon(comp.layers["mouth_closed"]), _canon(_mirror(comp.layers["mouth_closed"]))):
        # a mouth is centred; tiny AA asymmetries are fine, a visible lean is not
        a = np.asarray(C.alpha_of(comp.layers["mouth_closed"]), dtype=np.float64)
        _ys, xs = np.nonzero(a > 0)
        if len(xs) and abs(xs.mean() + 0.5 - cv.mirror_x) > 6:
            problems.append("mouth is off-centre")
    return build_result("F_SIDE_NAMING", passed=not problems, subject_sha=subject_sha, metric="side_naming_problems", value=float(len(problems)),
                        evidence="; ".join(problems) or "image-space naming holds; eyes mirror exactly")


def _canon(layer: Image.Image) -> np.ndarray:
    """RGBA array with RGB zeroed where alpha is 0 (RGB under alpha 0 is undefined)."""
    a = np.array(layer.convert("RGBA"), dtype=np.uint8)
    a[a[..., 3] == 0, :3] = 0
    return a


def _min_line_width_final(layer: Image.Image, density: int) -> tuple[float, float]:
    """Thinnest sustained stroke of ``layer`` at the final texel density and after a further 2x area downsample (px)."""
    a = layer.convert("RGBA")
    fin = a.convert("RGBa").resize((a.width // density, a.height // density), Image.Resampling.BOX).convert("RGBA")
    half = fin.convert("RGBa").resize((max(1, fin.width // 2), max(1, fin.height // 2)), Image.Resampling.BOX).convert("RGBA")
    return float(C.min_stroke_px(C.alpha_of(fin) >= 128, cap=12)), float(C.min_stroke_px(C.alpha_of(half) >= 128, cap=12))


def check_line_skin(comp: FaceComposite, tones: Sequence[CN.SkinTone] | None = None, *, subject_sha: str = "") -> CheckResult:
    """F_LINE_SKIN (FACE-06, HARD): on each of the 5 skin tones every line feature has CIEDE2000 >= 20 to the skin, its edge ring is within
    8 of clean skin, and its stroke is >= 2 px at the final texel density and still >= 1 px after a 2x area downsample."""
    cv = comp.canvas
    de_min = float(TH.get("face.line_skin_de_min"))
    ring_max = float(TH.get("face.line_edge_ring_de_max"))
    problems: list[str] = []
    worst = 1e9
    others: dict[str, np.ndarray] = {}
    for name in _line_colours(comp):
        union = np.zeros((cv.size, cv.size), bool)
        for other, lay in comp.layers.items():
            if other != name:
                union |= C.alpha_of(lay) > 0
        others[name] = P.dilate(union, 1)
    for t in _tones(tones):
        prev = np.asarray(comp.preview(t.hex, "neutral"))
        skin_lab = P.hex_to_lab(t.hex)
        for name, col in _line_colours(comp).items():
            de = P.de2000_hex(col, t.hex)
            worst = min(worst, de)
            if de < de_min:
                problems.append(f"{name} on {t.id}: dE {de:.1f} < {de_min:g}")
            core = C.alpha_of(comp.layers[name]) >= 128
            if core.any():
                ring = P.dilate(core, 3) & ~P.dilate(core, 1) & ~others[name]     # just beyond the anti-aliased edge band, on bare skin
                if ring.any():
                    ring_de = float(P.deltaE2000(P.srgb_to_lab(prev[..., :3][ring]), skin_lab).mean())
                    if ring_de > ring_max:
                        problems.append(f"{name} on {t.id}: edge ring dE {ring_de:.1f} > {ring_max:g}")
    for name in _line_colours(comp):
        if not (C.alpha_of(comp.layers[name]) > 0).any():
            continue
        w_fin, w_half = _min_line_width_final(comp.layers[name], cv.density)
        if w_fin < float(TH.get("face.stroke_px_min")):
            problems.append(f"{name}: stroke {w_fin:.0f}px < {TH.get('face.stroke_px_min')} at final density")
        if w_half < float(TH.get("face.stroke_px_min_2x_down")):
            problems.append(f"{name}: stroke {w_half:.0f}px < {TH.get('face.stroke_px_min_2x_down')} after 2x downsample")
    return build_result("F_LINE_SKIN", passed=not problems, subject_sha=subject_sha, metric="line_skin_de2000_min", value=worst,
                        threshold=TH.describe("face.line_skin_de_min", ">="), evidence="; ".join(problems[:6]) or f"worst line-to-skin dE {worst:.1f}",
                        fix_hint="revise_plan")


def _blended_y(skin_hex: str, layer: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(bare luminance, blended luminance, mask of pixels with alpha > 0)`` of a layer over a flat skin colour."""
    arr = np.asarray(layer).astype(np.float64)
    a = arr[..., 3:4] / 255.0
    skin = np.array(P.hex_to_rgb(skin_hex), dtype=np.float64)
    blended = arr[..., :3] * a + skin * (1 - a)
    return P.luminance_y(np.broadcast_to(skin, blended.shape)), P.luminance_y(blended), arr[..., 3] > 0


def check_shading_darkens(comp: FaceComposite, tones: Sequence[CN.SkinTone] | None = None, *, subject_sha: str = "") -> CheckResult:
    """F_SHADING (FACE-07, HARD): on all 5 tones, including near-black, the shading overlay makes every shading pixel darker than
    the bare skin (near-black at low alpha)."""
    bad = []
    for t in _tones(tones):
        bare, blended, on = _blended_y(t.hex, comp.layers["shading"])
        if on.any() and not bool((blended[on] < bare[on]).all()):
            bad.append(t.id)
    sh_max = float(np.asarray(comp.layers["shading"])[..., 3].max()) / 255.0
    return build_result("F_SHADING", passed=not bad, subject_sha=subject_sha, metric="shading_alpha_max", value=sh_max,
                        threshold=TH.describe("face.lid_shade_alpha_max", "<="), evidence=f"lightens on {bad}" if bad else "shading darkens every tone")


def check_blush_darkens(comp: FaceComposite, tones: Sequence[CN.SkinTone] | None = None, *, subject_sha: str = "") -> CheckResult:
    """F_BLUSH (FACE-07, SOFT): blush has L* <= 45 and alpha <= 0.35 and does not lighten any of the 5 skin tones."""
    lay = comp.layers["blush"]
    if not (C.alpha_of(lay) > 0).any():
        return build_result("F_BLUSH", passed=True, subject_sha=subject_sha, metric="blush", evidence="no blush")
    problems = []
    if comp.spec.blush and float(P.hex_to_lab(comp.spec.blush)[0]) > float(TH.get("face.blush_l_max")):
        problems.append(f"blush L* {float(P.hex_to_lab(comp.spec.blush)[0]):.0f} > {TH.get('face.blush_l_max')}")
    if np.asarray(lay)[..., 3].max() > 255 * float(TH.get("face.blush_alpha_max")) + 1:
        problems.append("blush alpha above the limit")
    for t in _tones(tones):
        bare, blended, on = _blended_y(t.hex, lay)
        if on.any() and not bool((blended[on] < bare[on]).all()):
            problems.append(f"lightens {t.id}")
    return build_result("F_BLUSH", passed=not problems, subject_sha=subject_sha, metric="blush_problems", value=float(len(problems)),
                        evidence="; ".join(problems) or "blush darkens every tone")


def hair_paint_share(texture: Image.Image, hair_hexes: Sequence[str], feature_mask: np.ndarray | None = None) -> tuple[float, str]:
    """Largest share (of the whole texture) of opaque pixels within dE ``img.palette_de_max`` of a hair palette colour that lie
    **outside** ``feature_mask`` (the known feature layers); with no mask every opaque pixel counts (a raw head texture)."""
    tex = np.asarray(texture.convert("RGBA"))
    sel = tex[..., 3] >= 128
    if feature_mask is not None:
        sel &= ~feature_mask
    if not sel.any() or not hair_hexes:
        return 0.0, ""
    lab = P.srgb_to_lab(tex[..., :3][sel])
    worst, hit = 0.0, ""
    for h in hair_hexes:
        share = float((P.deltaE2000(lab, P.hex_to_lab(h)) <= float(TH.get("img.palette_de_max"))).sum()) / tex[..., 3].size
        if share > worst:
            worst, hit = share, h
    return worst, hit


def check_no_hair_on_head(comp_or_texture: FaceComposite | Image.Image, hair_hexes: Sequence[str], *, feature_mask: np.ndarray | None = None,
                          subject_sha: str = "") -> CheckResult:
    """F_NO_HAIR (FACE-16, HARD): no cluster within dE 12 of a hair palette colour covers >= 0.5% of the head texture outside the
    feature layers, and every texel outside the landmark zones is skin-transparent or near-black shading with alpha <= 0.35.

    Given a ``FaceComposite`` the feature layers are the known features (the composer cannot paint hair, so only imported art can fail);
    given a raw head texture pass ``feature_mask=None`` to count every opaque pixel.
    """
    if isinstance(comp_or_texture, FaceComposite):
        comp = comp_or_texture
        tex_im = comp.texture("neutral")
        feat = np.zeros(C.alpha_of(tex_im).shape, bool)
        for lay in comp.layers.values():
            feat |= C.alpha_of(lay) > 0
        feat = P.dilate(feat, 2)
        zone = P.dilate(comp.canvas.zone_mask(comp.spec.eye_shape), 1)
    else:
        tex_im = comp_or_texture.convert("RGBA")
        feat = feature_mask
        zone = feature_mask if feature_mask is not None else np.zeros(C.alpha_of(tex_im).shape, bool)
    worst, hit = hair_paint_share(tex_im, hair_hexes, feat)
    tex = np.asarray(tex_im)
    stray = (tex[..., 3] > 0) & ~zone
    stray_alpha = float(tex[..., 3][stray].max()) / 255.0 if stray.any() else 0.0
    ok = worst < float(TH.get("face.hair_on_head_area_max")) and stray_alpha <= float(TH.get("face.nonfeature_alpha_max"))
    return build_result("F_NO_HAIR", passed=ok, subject_sha=subject_sha, metric="hair_colour_area_share", value=worst,
                        threshold=TH.describe("face.hair_on_head_area_max", "<"),
                        evidence=f"hair-coloured area outside the features {worst:.4f} ({hit or 'none'}); non-feature alpha max {stray_alpha:.2f}",
                        fix_hint="regenerate")


def check_mouth_interior(comp: FaceComposite, tones: Sequence[CN.SkinTone] | None = None, *, subject_sha: str = "") -> CheckResult:
    """F_MOUTH_INTERIOR (FACE-14, HARD): in the mouth-open state the opening is painted (alpha ~1) and is not a skin tone on any tone."""
    poly = comp.canvas.mouth(comp.spec.mouth_style).open_polygon
    inner = P.erode(_poly_bool(comp.canvas.size, poly), 6)
    tex = np.asarray(comp.texture("mouth_open"))
    if not inner.any():
        return build_result("F_MOUTH_INTERIOR", passed=False, subject_sha=subject_sha, metric="mouth_opening", evidence="empty opening polygon")
    alpha_share = float((tex[..., 3][inner] >= 250).mean())
    lab = P.srgb_to_lab(tex[..., :3][inner & (tex[..., 3] >= 250)]) if (inner & (tex[..., 3] >= 250)).any() else np.zeros((0, 3))
    skin_like = []
    for t in _tones(tones):
        if len(lab) and float((P.deltaE2000(lab, P.hex_to_lab(t.hex)) < 8.0).mean()) > 0.2:
            skin_like.append(t.id)
    ok = alpha_share >= float(TH.get("face.mouth_inner_min_alpha")) and not skin_like
    return build_result("F_MOUTH_INTERIOR", passed=ok, subject_sha=subject_sha, metric="mouth_interior_painted", value=alpha_share,
                        threshold=TH.describe("face.mouth_inner_min_alpha", ">="), evidence=f"painted {alpha_share:.3f}; skin-like on {skin_like or 'none'}",
                        fix_hint="regenerate")


def check_skin_transparent(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_SKIN_TRANSPARENT (FACE-12, HARD): >= 95% of the canvas skin zone (everything outside the landmark zones) is alpha 0 in every
    expression state, so skin is never baked into the head texture."""
    zone = P.dilate(comp.canvas.zone_mask(comp.spec.eye_shape), 1)
    painted = np.zeros(zone.shape, bool)
    for st in EXPRESSIONS:
        painted |= comp.feature_alpha(st) > 0
    skin_zone = ~zone
    share = float((~painted[skin_zone]).mean())
    return build_result("F_SKIN_TRANSPARENT", passed=share >= float(TH.get("face.skin_alpha_zero_min")), subject_sha=subject_sha,
                        metric="skin_alpha0_share", value=share, threshold=TH.describe("face.skin_alpha_zero_min", ">="),
                        evidence=f"{share:.3f} of the skin zone is transparent", fix_hint="code_alpha_cleanup")


def _between_allowed(lab: np.ndarray, allowed_lab: np.ndarray, tol: float) -> np.ndarray:
    """True for colours within ``tol`` of an allowed colour or of the straight blend between two allowed colours (anti-aliasing)."""
    near = np.zeros(len(lab), bool)
    n = len(allowed_lab)
    for i in range(n):
        near |= P.deltaE2000(lab, allowed_lab[i]) <= tol
    for i in range(n):
        for j in range(i + 1, n):
            a, b = allowed_lab[i], allowed_lab[j]
            ab = b - a
            t = np.clip(((lab - a) @ ab) / max(float(ab @ ab), 1e-9), 0.0, 1.0)
            near |= np.linalg.norm(lab - (a + t[:, None] * ab), axis=1) <= tol
    return near


def check_lash_lid_split(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """F_LASH_LID_SPLIT (FACE-10, HARD): the lid layer holds only the lash colour, and the eyeball layers hold only the iris, pupil,
    sclera and (white) highlight colours or anti-aliased blends of them (the pupil may share the lash colour: the parts are separate)."""
    s = comp.spec
    problems = []
    for name in ("lash", "closed_lid"):
        cols, wrong = C.distinct_interior_colours(comp.layers[name], 2)
        if len(cols) > 1 or (cols and P.de2000_rgb(cols[0], P.hex_to_rgb(s.lash)) > 3.0) or wrong:
            problems.append(f"{name} layer is not only the lash colour")
    allowed_lab = P.palette_lab([s.iris, s.iris_dark, s.pupil, s.sclera, WHITE])
    for name in ("sclera", "iris"):
        arr = np.asarray(comp.layers[name])
        solid = arr[..., 3] >= 250
        if solid.any():
            rgb = arr[..., :3][solid]
            uniq, inv = np.unique(rgb, axis=0, return_inverse=True)
            ok_u = _between_allowed(P.srgb_to_lab(uniq), allowed_lab, 6.0)
            bad_share = float((~ok_u[inv.reshape(-1)]).mean())
            if bad_share > 0.002:
                problems.append(f"{name} layer carries colours outside iris/pupil/sclera ({bad_share:.3f})")
    return build_result("F_LASH_LID_SPLIT", passed=not problems, subject_sha=subject_sha, metric="split_problems", value=float(len(problems)),
                        evidence="; ".join(problems) or "lid layer = lash colour only; eyeball layers = iris/pupil/sclera only", fix_hint="regenerate")


def face_field_diff(face_a: Mapping[str, Any] | FaceSpec, face_b: Mapping[str, Any] | FaceSpec) -> list[str]:
    """The grammar fields (of the 7 that count) on which two faces differ."""
    fa = face_a.feature_grammar() if isinstance(face_a, FaceSpec) else dict(face_a)
    fb = face_b.feature_grammar() if isinstance(face_b, FaceSpec) else dict(face_b)
    return [f for f in FACE_FIELDS_DISTINCT if fa.get(f) != fb.get(f)]


def check_ab_face_difference(face_a: Mapping[str, Any] | FaceSpec, face_b: Mapping[str, Any] | FaceSpec, *, subject_sha: str = "") -> CheckResult:
    """F_AB_FACE_DIFF (FACE-11, HARD): A and B differ in at least 3 of the 7 face grammar fields."""
    diff = face_field_diff(face_a, face_b)
    need = int(TH.get("pln.face_features_diff_min"))
    return build_result("F_AB_FACE_DIFF", passed=len(diff) >= need, subject_sha=subject_sha, metric="differing_fields", value=float(len(diff)),
                        threshold=TH.describe("pln.face_features_diff_min", ">="), evidence=f"differ in {diff}", fix_hint="revise_plan")


# ------------------------------------------------------------------ head-base-only checks (not_applicable without a head base)
def check_blink_iris(blink_renders: Mapping[str, np.ndarray], iris_hexes: Sequence[str], eye_zone: np.ndarray | None = None, *,
                     subject_sha: str = "") -> CheckResult:
    """F_BLINK_IRIS (FACE-03, HARD, head base only): iris-coloured pixels in the LeftEyeClosed / RightEyeClosed renders (each also with
    EyesLookDown) are 0. ``blink_renders`` maps a pose name to an RGB ``(H, W, 3)`` render; ``eye_zone`` is a bool mask of the eye zones."""
    total = 0
    detail = []
    for pose, img in blink_renders.items():
        arr = np.asarray(img)[..., :3]
        sel = np.ones(arr.shape[:2], bool) if eye_zone is None else eye_zone
        lab = P.srgb_to_lab(arr[sel])
        n = 0
        for h in iris_hexes:
            n += int((P.deltaE2000(lab, P.hex_to_lab(h)) <= 6.0).sum())
        total += n
        detail.append(f"{pose}:{n}")
    return build_result("F_BLINK_IRIS", passed=total <= int(TH.get("face.blink_iris_px_max")), subject_sha=subject_sha, metric="iris_px", value=float(total),
                        threshold=TH.describe("face.blink_iris_px_max", "<="), evidence=", ".join(detail), fix_hint="human")


def check_neck_seam(head_rgb: Sequence[float], torso_rgb: Sequence[float], *, subject_sha: str = "") -> CheckResult:
    """F_NECK_SEAM (CHK-B09, HARD, head base only): head and torso colours at the neck seam are within CIEDE2000 2."""
    de = P.de2000_rgb(head_rgb, torso_rgb)
    return build_result("F_NECK_SEAM", passed=de <= float(TH.get("face.neck_seam_de_max")), subject_sha=subject_sha, metric="neck_seam_de2000", value=de,
                        threshold=TH.describe("face.neck_seam_de_max", "<="), evidence=f"seam dE {de:.2f}", fix_hint="human")


def check_face_2d_profile(comp: FaceComposite, *, subject_sha: str = "") -> CheckResult:
    """CHK-A17 (HARD): the 2D face profile that runs only without a head base (APP_SPEC §10.6): every feature pixel inside its canvas
    zone, the closed-lid layer covers the sclera polygon (0 iris pixels in the blink preview), the open-mouth layer is painted, and
    the skin zone is transparent. Aggregates ``F_ZONES``, ``F_LID_COVERS``, ``F_MOUTH_INTERIOR`` and ``F_SKIN_TRANSPARENT``."""
    parts = [check_zones(comp), check_lid_covers(comp), check_mouth_interior(comp), check_skin_transparent(comp)]
    bad = [f"{r.check_id}: {r.evidence}" for r in parts if not r.passed]
    return build_result("CHK-A17", passed=not bad, subject_sha=subject_sha, metric="face_2d_problems", value=float(len(bad)),
                        evidence="; ".join(bad) or "2D face profile holds", fix_hint="code_recrop")


# ------------------------------------------------------------------ the suite
def face_check_suite(comp: FaceComposite, *, hair_hexes: Sequence[str] = (), tones: Sequence[CN.SkinTone] | None = None,
                     head_base_present: bool = False, head_base_inputs: Mapping[str, Any] | None = None,
                     other_face: Mapping[str, Any] | FaceSpec | None = None, subject_sha: str = "") -> list[CheckResult]:
    """Every face-tile check, each run through ``checks.runner.run_check`` (fail-closed).

    Without a head base the head-base checks (``F_BLINK_IRIS``, ``F_WARP_IOU``, ``F_STRETCH``, ``F_NECK_SEAM``, CHK-B09) are
    ``not_applicable`` with the reason ``no_head_base`` (set by the runner from the registry's ``requires`` flags) and the 2D equivalents
    run instead (``CHK-A17`` is the 2D profile and is itself N/A when a head base exists). With a head base, ``head_base_inputs`` supplies
    the renders: ``blink_renders``, ``iris_hexes``, ``eye_zone``, ``canvas_alpha``, ``render_alpha``, ``stretch_by_pose``,
    ``feature_mask_by_pose``, ``head_rgb``, ``torso_rgb``; a missing input fails closed.
    """
    from duoskin.checks.runner import run_check
    from duoskin.imaging import uvwarp

    hb = dict(head_base_inputs or {})
    ts = _tones(tones)
    res = [
        run_check("F_LINE_COLOURS", subject_sha, lambda: check_line_colours(comp)),
        run_check("F_ZONES", subject_sha, lambda: check_zones(comp)),
        run_check("F_LID_COVERS", subject_sha, lambda: check_lid_covers(comp)),
        run_check("F_CATCHLIGHT", subject_sha, lambda: check_catchlight_sign(comp)),
        run_check("F_SIDE_NAMING", subject_sha, lambda: check_side_naming(comp)),
        run_check("F_LINE_SKIN", subject_sha, lambda: check_line_skin(comp, ts)),
        run_check("F_SHADING", subject_sha, lambda: check_shading_darkens(comp, ts)),
        run_check("F_BLUSH", subject_sha, lambda: check_blush_darkens(comp, ts)),
        run_check("F_NO_HAIR", subject_sha, lambda: check_no_hair_on_head(comp, hair_hexes)),
        run_check("F_MOUTH_INTERIOR", subject_sha, lambda: check_mouth_interior(comp, ts)),
        run_check("F_SKIN_TRANSPARENT", subject_sha, lambda: check_skin_transparent(comp)),
        run_check("F_LASH_LID_SPLIT", subject_sha, lambda: check_lash_lid_split(comp)),
        run_check("CHK-A17", subject_sha, lambda: check_face_2d_profile(comp), head_base_present=head_base_present),
    ]
    if other_face is not None:
        res.append(run_check("F_AB_FACE_DIFF", subject_sha, lambda: check_ab_face_difference(comp.spec, other_face)))
    res += [
        run_check("F_BLINK_IRIS", subject_sha, lambda: check_blink_iris(hb["blink_renders"], hb["iris_hexes"], hb.get("eye_zone")),
                  head_base_present=head_base_present),
        run_check("F_WARP_IOU", subject_sha, lambda: uvwarp.check_warp_iou(hb["canvas_alpha"], hb["render_alpha"]), head_base_present=head_base_present),
        run_check("F_STRETCH", subject_sha, lambda: uvwarp.check_stretch(hb["stretch_by_pose"], hb.get("feature_mask_by_pose")),
                  head_base_present=head_base_present),
        run_check("F_NECK_SEAM", subject_sha, lambda: check_neck_seam(hb["head_rgb"], hb["torso_rgb"]), head_base_present=head_base_present),
    ]
    return res
