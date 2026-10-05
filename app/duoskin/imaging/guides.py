"""Code-drawn guides and shared inputs (PROMPT_BIBLE §8: "code draws layout, the model fills in"; APP_SPEC §10.3).

Guides are *inputs* to image edits: the model never has to invent layout. Each guide returns the image to send as Image 1 **and** the mask
(alpha 0 = editable) plus the geometry that the checks (``A_SIL_GUIDE``, ``A_PASTE``, ``A_GUIDE_LEFT``, CHK-G1-01) read back.

* ``guide_concept_char`` (I1): 1536x1024 on #F2F2F2, two 768x1024 slots (front, back), a blocky figure at 120 px/stud (arms-included width
  4 studs = 480 px, height 5.2 studs = 624 px, feet at y=964, head top at y=340), colour-blocked from the figure's cuts, plus a swatch strip
  (5 squares per slot). No face, no hair, no text.
* ``guide_bald_head`` (I4): 1024x1536 white, a cube head at 280 px/stud, mid-grey (switched to another colour when the hair colours are
  grey, silver or white). The Hair box is editable except the lower 55% of the head's front face.
* ``guide_face_part`` / ``guide_face_part_incanvas`` (I3): the rig geometry of one face part in mid-grey, with its dilated mask.
* ``guide_panel`` (I8): a mid-grey panel inside a recipe mask.

Also here: the 4-up concept sheet (C2), part-crop preparation (§8.3), side-by-side sheets with a 32 px gutter, and the Claude/Gemini image
preparation (grey and checkerboard composites, nearest-neighbour upscale, long edge <= 2576).

Swatch strip note: the bible says "5 squares of 40 px at y=990-1014", but a 40 px square cannot fit 24 px of height. The strip uses
24 px squares on a 40 px pitch (centred, y=990..1013), which fits the 1024 px slot and stays outside the editable area.
"""
from __future__ import annotations

import io
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result
from duoskin.imaging import face_canvas as FC
from duoskin.imaging import masks as M
from duoskin.imaging import palette as P

BG_HEX = "#f2f2f2"
GUIDE_GREY = "#9a9a9a"
PX_PER_STUD = 120
SLOT_W, SLOT_H = 768, 1024
CANVAS_W, CANVAS_H = 2 * SLOT_W, SLOT_H
HEAD_TOP, FEET_Y, NECK_Y = 340, 964, 484
FIG_W = 4 * PX_PER_STUD                       # arms included
SWATCH_Y, SWATCH_SIZE, SWATCH_PITCH = 990, 24, 40
SIDE_MARGIN = int(1.2 * PX_PER_STUD)          # 144 px editable to each side of the figure
TOP_MARGIN = 2 * PX_PER_STUD                  # 240 px above the head top
BOTTOM_MARGIN = 16
Sleeve = Literal["sleeveless", "short", "three_quarter", "long"]
LegCut = Literal["short", "above_knee", "knee", "full"]
Hem = Literal["hip_untucked", "waist_tucked", "crop"]
BlockLayout = Literal["solid", "raglan_split", "colour_block"]

SLEEVE_COVER = {"sleeveless": 0.0, "short": 0.35, "three_quarter": 0.65, "long": 0.85}   # of the 2-stud arm; hands stay skin
LEG_COVER = {"short": 0.30, "above_knee": 0.50, "knee": 0.65, "full": 1.0}


# ------------------------------------------------------------------ the figure
@dataclass
class FigureSpec:
    """What a figure is painted with: colours (hex) and the cuts that decide how much of each limb they cover."""

    skin: str
    top_base: str
    bottom_base: str
    top_second: str | None = None
    legwear: str | None = None
    shoes: str | None = None
    sleeve: Sleeve = "long"
    hem: Hem = "waist_tucked"
    leg: LegCut = "full"
    block_layout: BlockLayout = "solid"
    swatches: Sequence[str] = field(default_factory=tuple)      # hair, top, bottom, shoes, accent (5 hex)


@dataclass
class Slot:
    name: str
    x0: int
    cx: int
    figure_box: tuple[int, int, int, int]
    editable_box: tuple[int, int, int, int]


@dataclass
class Guide:
    """An image to send (RGB or RGBA), its mask, and the geometry for the checks."""

    guide_id: str
    image: Image.Image
    editable: np.ndarray                                   # bool (H, W): True = the model may change it
    slots: dict[str, Slot] = field(default_factory=dict)
    body_masks: dict[str, np.ndarray] = field(default_factory=dict)    # per slot: torso + arms + legs (below the neckline)
    head_masks: dict[str, np.ndarray] = field(default_factory=dict)
    protected_face: np.ndarray | None = None
    meta: dict[str, object] = field(default_factory=dict)

    def mask_png(self) -> bytes:
        """The OpenAI edit mask: RGBA PNG, alpha 0 = editable."""
        return M.make_mask(self.editable)

    def image_png(self) -> bytes:
        buf = io.BytesIO()
        self.image.save(buf, "PNG")
        return buf.getvalue()


def _box(cx: int, y0: int, y1: int, half_w: int) -> tuple[int, int, int, int]:
    return cx - half_w, y0, cx + half_w, y1


def _fill(arr: np.ndarray, box: tuple[int, int, int, int], rgb: tuple[int, int, int]) -> None:
    x0, y0, x1, y1 = box
    arr[y0:y1, x0:x1] = rgb


def figure_geometry(cx: int) -> dict[str, tuple[int, int, int, int]]:
    """Part boxes (x0, y0, x1, y1) of the standard blocky figure centred on column ``cx`` (1 stud = 120 px)."""
    s = PX_PER_STUD
    head = (cx - int(0.6 * s), HEAD_TOP, cx + int(0.6 * s), NECK_Y)
    torso = (cx - s, NECK_Y, cx + s, NECK_Y + 2 * s)
    return {
        "head": head, "torso": torso,
        "arm_l": (cx - 2 * s, NECK_Y, cx - s, NECK_Y + 2 * s), "arm_r": (cx + s, NECK_Y, cx + 2 * s, NECK_Y + 2 * s),
        "leg_l": (cx - s, NECK_Y + 2 * s, cx, FEET_Y), "leg_r": (cx, NECK_Y + 2 * s, cx + s, FEET_Y),
    }


def draw_figure(arr: np.ndarray, cx: int, fig: FigureSpec) -> tuple[np.ndarray, np.ndarray]:
    """Colour-block one figure into ``arr`` (an RGB canvas). Returns ``(body_mask, head_mask)`` as bool canvas masks."""
    g = figure_geometry(cx)
    skin = P.hex_to_rgb(fig.skin)
    top = P.hex_to_rgb(fig.top_base)
    second = P.hex_to_rgb(fig.top_second) if fig.top_second else top
    bottom = P.hex_to_rgb(fig.bottom_base)
    s = PX_PER_STUD
    h, w = arr.shape[:2]
    body = np.zeros((h, w), bool)
    head = np.zeros((h, w), bool)
    _fill(arr, g["head"], skin)
    head[g["head"][1]:g["head"][3], g["head"][0]:g["head"][2]] = True
    # torso
    t = g["torso"]
    _fill(arr, t, top)
    if fig.block_layout == "colour_block":
        _fill(arr, (t[0], t[1] + s, t[2], t[3]), second)
    if fig.hem == "crop":
        _fill(arr, (t[0], t[3] - s // 2, t[2], t[3]), skin)
    elif fig.hem == "waist_tucked":
        _fill(arr, (t[0], t[3] - s // 6, t[2], t[3]), bottom)
    # arms
    cover = SLEEVE_COVER[fig.sleeve]
    for key in ("arm_l", "arm_r"):
        a = g[key]
        _fill(arr, a, skin)
        if cover > 0:
            _fill(arr, (a[0], a[1], a[2], a[1] + round(cover * 2 * s)), second if fig.block_layout == "raglan_split" else top)
    # legs
    lc = LEG_COVER[fig.leg]
    for key in ("leg_l", "leg_r"):
        lg = g[key]
        _fill(arr, lg, P.hex_to_rgb(fig.legwear) if fig.legwear else skin)
        _fill(arr, (lg[0], lg[1], lg[2], lg[1] + round(lc * 2 * s)), bottom)
        if fig.shoes:
            _fill(arr, (lg[0], lg[3] - int(0.4 * s), lg[2], lg[3]), P.hex_to_rgb(fig.shoes))
    for key in ("torso", "arm_l", "arm_r", "leg_l", "leg_r"):
        b = g[key]
        body[b[1]:b[3], b[0]:b[2]] = True
    return body, head


def _swatch_strip(arr: np.ndarray, x0: int, colours: Sequence[str]) -> None:
    n = len(colours)
    if not n:
        return
    total = (n - 1) * SWATCH_PITCH + SWATCH_SIZE
    sx = x0 + (SLOT_W - total) // 2
    for i, hx in enumerate(colours):
        arr[SWATCH_Y:SWATCH_Y + SWATCH_SIZE, sx + i * SWATCH_PITCH:sx + i * SWATCH_PITCH + SWATCH_SIZE] = P.hex_to_rgb(hx)


def _build_concept(figs: Sequence[FigureSpec], names: Sequence[str], guide_id: str) -> Guide:
    """``len(figs)`` slots of 768x1024 side by side, each built exactly like a ``guide_concept_char`` slot."""
    width = SLOT_W * len(figs)
    arr = np.zeros((CANVAS_H, width, 3), np.uint8)
    arr[:] = P.hex_to_rgb(BG_HEX)
    editable = np.zeros((CANVAS_H, width), bool)
    slots: dict[str, Slot] = {}
    bodies: dict[str, np.ndarray] = {}
    heads: dict[str, np.ndarray] = {}
    for i, (name, fig) in enumerate(zip(names, figs, strict=True)):
        x0 = i * SLOT_W
        cx = x0 + SLOT_W // 2
        body, head = draw_figure(arr, cx, fig)
        _swatch_strip(arr, x0, fig.swatches)
        fx0, fx1 = cx - FIG_W // 2, cx + FIG_W // 2
        fig_box = (fx0, HEAD_TOP, fx1, FEET_Y)
        ed_box = (max(x0, fx0 - SIDE_MARGIN), HEAD_TOP - TOP_MARGIN, min(x0 + SLOT_W, fx1 + SIDE_MARGIN), FEET_Y + BOTTOM_MARGIN)
        editable[ed_box[1]:ed_box[3], ed_box[0]:ed_box[2]] = True
        slots[name] = Slot(name, x0, cx, fig_box, ed_box)
        bodies[name], heads[name] = body, head
    return Guide(guide_id, Image.fromarray(arr, "RGB"), editable, slots, bodies, heads, None,
                 {"size": (width, CANVAS_H), "px_per_stud": PX_PER_STUD, "neckline_y": NECK_Y})


def guide_concept_char(fig: FigureSpec) -> Guide:
    """The I1 guide for one character: front figure in slot 1, back figure (same colours) in slot 2 (1536x1024)."""
    return _build_concept([fig, fig], ("front", "back"), "guide_concept_char")


def guide_concept_front(fig: FigureSpec) -> Guide:
    """I1f: one 768x1024 slot of ``guide_concept_char`` (the front figure)."""
    return _build_concept([fig], ("front",), "guide_concept_front")


def guide_concept_back(fig: FigureSpec) -> Guide:
    """I1b: one 768x1024 slot (the back figure, same colours)."""
    return _build_concept([fig], ("back",), "guide_concept_back")


def guide_concept_joint(fig_a: FigureSpec, fig_b: FigureSpec) -> Guide:
    """I1j: 3072x1024, four slots in the order A front, A back, B front, B back, each from its own character's spec."""
    return _build_concept([fig_a, fig_a, fig_b, fig_b], ("a_front", "a_back", "b_front", "b_back"), "guide_concept_joint")


def check_guide_geometry(g: Guide, *, subject_sha: str = "") -> CheckResult:
    """CHK-G1-01 (ASSERT): the concept guide is made of 768x1024 slots (two on 1536x1024, one, or four on 3072x1024), each figure is at most
    480 px wide, the mask boxes do not overlap, and the swatch strip and background stay protected."""
    problems = []
    boxes = list(g.slots.values())
    if g.image.size != (SLOT_W * len(boxes), CANVAS_H) or not boxes:
        problems.append(f"canvas {g.image.size} for {len(boxes)} slot(s)")
    for sl in boxes:
        if sl.figure_box[2] - sl.figure_box[0] > FIG_W:
            problems.append(f"{sl.name} figure wider than {FIG_W}px")
    for a, b in itertools.pairwise(boxes):
        if min(a.editable_box[2], b.editable_box[2]) > max(a.editable_box[0], b.editable_box[0]):
            problems.append("mask boxes overlap")
    if g.editable[SWATCH_Y - 1:, :].any():
        problems.append("swatch strip is not protected")
    ok = not problems
    return build_result("CHK-G1-01", passed=ok, subject_sha=subject_sha, metric="guide_geometry", evidence="; ".join(problems) or "guide geometry holds")


# ------------------------------------------------------------------ bald head guide (I4)
GREY_CANDIDATES = ("#9a9a9a", "#6f8fb0", "#b08a8f", "#7fa88a", "#c9a45a", "#5f5f8f")
# Fallback search when no listed candidate is far enough (grey + white + black hair together, say): a deterministic hue ring at several
# saturations and values, so a guide colour that is >= 30 dE from every hair colour exists for any realistic hair set.
_RING_HUES = tuple(range(0, 360, 30))
_RING_SV = ((0.55, 0.45), (0.85, 0.45), (0.55, 0.70), (0.85, 0.70), (0.55, 0.95), (0.85, 0.95))


def _hue_ring() -> tuple[str, ...]:
    import colorsys

    out = []
    for s_, v_ in _RING_SV:
        for h_ in _RING_HUES:
            r, g, b = colorsys.hsv_to_rgb(h_ / 360.0, s_, v_)
            out.append(P.rgb_to_hex((round(r * 255), round(g * 255), round(b * 255))))
    return tuple(out)


def choose_guide_grey(hair_hexes: Sequence[str], *, min_de: float | None = None) -> str:
    """#9A9A9A unless a hair colour is within dE 30 of it (grey, silver, white hair); then the first candidate that is >= 30 from every hair
    colour (HAIR-04 assert), then the first colour of the hue ring that is, else the farthest one."""
    lim = float(TH.get("hair.guide_de_min")) if min_de is None else min_de
    hair = P.palette_lab(hair_hexes) if len(hair_hexes) else np.zeros((0, 3))
    best, best_d = GREY_CANDIDATES[0], -1.0
    for c in (*GREY_CANDIDATES, *_hue_ring()):
        d = float(P.deltaE2000(P.hex_to_lab(c)[None, :], hair).min()) if len(hair) else 1e9
        if d >= lim:
            return c
        if d > best_d:
            best, best_d = c, d
    return best


def guide_bald_head(hair_hexes: Sequence[str] = ()) -> Guide:
    """The I4 guide: a cube head front-on at the Hair-box scale (1 stud = 280 px, head 1.2 studs = 336 px), centred, top at y=560.

    The mask is the Hair box (3 x 5 studs, 2 up and 3 down from the head top) minus the lower 55% of the head's front face.
    """
    w, h = 1024, 1536
    stud = 280
    grey = choose_guide_grey(hair_hexes)
    arr = np.full((h, w, 3), 255, np.uint8)
    head_w = int(1.2 * stud)
    cx = w // 2
    top = 560
    hx0, hx1 = cx - head_w // 2, cx + head_w // 2
    arr[top:top + head_w, hx0:hx1] = P.hex_to_rgb(grey)
    editable = np.zeros((h, w), bool)
    bx0, bx1 = cx - int(1.5 * stud), cx + int(1.5 * stud)
    by0, by1 = top - 2 * stud, top + 3 * stud
    editable[max(0, by0):min(h, by1), max(0, bx0):min(w, bx1)] = True
    protected = np.zeros((h, w), bool)
    p0 = top + round(head_w * (1.0 - float(TH.get("hair.face_protect_frac"))))
    protected[p0:top + head_w, hx0:hx1] = True
    editable &= ~protected
    head_mask = np.zeros((h, w), bool)
    head_mask[top:top + head_w, hx0:hx1] = True
    return Guide("guide_bald_head", Image.fromarray(arr, "RGB"), editable, {}, {}, {"head": head_mask}, protected,
                 {"guide_grey": grey, "hair_box": (bx0, max(0, by0), bx1, by1), "head_box": (hx0, top, hx1, top + head_w)})


# ------------------------------------------------------------------ face part guides (I3)
FACE_PARTS = ("iris", "lash_upper", "brow", "mouth_closed", "mouth_open", "closed_lid_line")


def _part_shape(part: str, canvas: FC.FaceCanvas, spec: FC.FaceSpec) -> np.ndarray:
    """The rig geometry of ``part`` in canvas coordinates as a bool mask (size x size)."""
    e = canvas.eye(spec.eye_shape)
    size = canvas.size
    pen_box = (0, 0, size, size)
    pen = FC._Pen(pen_box)
    if part == "iris":
        pen.ellipse(e.iris_cx, e.iris_cy, e.iris_w / 2, e.iris_h / 2)
    elif part == "lash_upper":
        pen.polyline([tuple(p) for p in e.lash_arc], 40)
    elif part == "closed_lid_line":
        pen.polyline([tuple(p) for p in e.closed_lid_arc], 40)
    elif part == "brow":
        bl = np.asarray(canvas.brow["baseline"], float)
        pts = [(canvas.eye_centre[0] + x, canvas.eye_centre[1] + float(canvas.brow["centre_dy"]) + y) for x, y in bl]
        pen.polyline(pts, 54)
    elif part == "mouth_closed":
        x0, y0, x1, y1 = canvas.mouth(spec.mouth_style).closed_box
        pen.ellipse((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, max(24.0, (y1 - y0) / 2))
    elif part == "mouth_open":
        pen.polygon([tuple(p) for p in canvas.mouth(spec.mouth_style).open_polygon])
    else:
        raise KeyError(f"unknown face part {part!r}; known: {FACE_PARTS}")
    return pen.mask() >= 0.5


def _fit_shape(mask: np.ndarray, size: int, width_frac: float) -> np.ndarray:
    """Crop the shape and scale it so its width is ``width_frac`` of ``size`` (aspect kept, centred)."""
    ys, xs = np.nonzero(mask)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    scale = min(width_frac * size / crop.shape[1], 0.9 * size / crop.shape[0])
    im = Image.fromarray((crop * 255).astype(np.uint8)).resize((max(1, round(crop.shape[1] * scale)), max(1, round(crop.shape[0] * scale))),
                                                               Image.Resampling.BILINEAR)
    out = Image.new("L", (size, size), 0)
    out.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return np.asarray(out) >= 128


def guide_face_part(part: str, spec: FC.FaceSpec | None = None, canvas: FC.FaceCanvas | None = None, *, size: int = 1024,
                    dilate_px: int = 24) -> Guide:
    """The I3 guide for one face part: the rig geometry in mid-grey (#9A9A9A) at about 70% of the width on a transparent canvas; the mask is
    the grey shape dilated by 24 px (always sent with its explicit mask, U26)."""
    cv = canvas or FC.load_canvas()
    sp = spec or FC.FaceSpec()
    shape = _fit_shape(_part_shape(part, cv, sp), size, 0.70)
    arr = np.zeros((size, size, 4), np.uint8)
    arr[shape] = (*P.hex_to_rgb(GUIDE_GREY), 255)
    editable = P.dilate(shape, dilate_px)
    return Guide(f"guide_face_part_{part}", Image.fromarray(arr, "RGBA"), editable, meta={"part": part, "shape_px": int(shape.sum())})


def guide_face_part_incanvas(part: str, spec: FC.FaceSpec | None = None, canvas: FC.FaceCanvas | None = None, *,
                             concept_face: Image.Image | None = None, style_sample: Image.Image | None = None) -> Guide:
    """The in-canvas variant for when mask + several images is refused (D17): 1536x1024, the left 1024x1024 slot is the transparent guide
    with the grey shape, the right 512x1024 column is opaque #F2F2F2 holding the concept face crop (top 512 px) and a style-sheet
    face-part sample (bottom 512 px). Only the dilated grey shape in the left slot is editable. Crop the output to the left slot."""
    left = guide_face_part(part, spec, canvas)
    arr = np.zeros((1024, 1536, 4), np.uint8)
    arr[:, :1024] = np.asarray(left.image)
    arr[:, 1024:] = (*P.hex_to_rgb(BG_HEX), 255)
    img = Image.fromarray(arr, "RGBA")
    for im, y in ((concept_face, 0), (style_sample, 512)):
        if im is not None:
            fit = im.convert("RGBA")
            fit.thumbnail((512, 512), Image.Resampling.LANCZOS)
            img.alpha_composite(fit, (1024 + (512 - fit.width) // 2, y + (512 - fit.height) // 2))
    editable = np.zeros((1024, 1536), bool)
    editable[:, :1024] = left.editable
    return Guide(f"guide_face_part_incanvas_{part}", img, editable, meta={"part": part, "output_crop": (0, 0, 1024, 1024)})


# ------------------------------------------------------------------ garment panel (I8)
def guide_panel(recipe_mask: np.ndarray) -> Guide:
    """The I8 guide: mid-grey #808080 inside the recipe mask, white outside; the mask is the recipe mask."""
    h, w = recipe_mask.shape
    arr = np.full((h, w, 3), 255, np.uint8)
    arr[recipe_mask] = (128, 128, 128)
    return Guide("guide_panel", Image.fromarray(arr, "RGB"), recipe_mask.copy(), meta={"size": (w, h)})


# ------------------------------------------------------------------ frame and accessory-box guides (I2/I5/I6 without a concept crop; I5g)
FRAME_SIZES = {"square": (1024, 1024), "tall": (816, 1632)}
FRAME_MARGIN = 0.10
FRAME_MARGIN_I5 = 0.12
ACC_BOX_FILL_MAX = 0.76
ACC_BOX_MASK_PAD = 0.04


def guide_frame(aspect: Literal["square", "tall"] = "square", *, margin: float | None = None) -> Guide:
    """``guide_frame_<aspect>``: an empty transparent framing canvas. Nothing is drawn; the mask protects the margin band (10% on each side,
    12% for I5) and leaves the inside editable, and A_MARGIN checks the result."""
    w, h = FRAME_SIZES[aspect]
    m = FRAME_MARGIN if margin is None else margin
    editable = np.zeros((h, w), bool)
    mx, my = round(w * m), round(h * m)
    editable[my:h - my, mx:w - mx] = True
    return Guide(f"guide_frame_{aspect}", Image.new("RGBA", (w, h), (0, 0, 0, 0)), editable, meta={"margin": m, "size": (w, h)})


def guide_acc_box(item_studs: tuple[float, float], box_studs: tuple[float, float], *, size: int = 1024) -> Guide:
    """``guide_acc_box_<attachment>`` (I5g): the planned silhouette box on opaque #F2F2F2, drawn as a mid-grey rounded rectangle, centred,
    filling at most 76% of the long side. ``item_studs`` is the accessory's planned width x height in studs, ``box_studs`` the face of its
    Classic box (so the whole box would fill 76%). The mask is the box dilated by 4% of the width."""
    pps = ACC_BOX_FILL_MAX * size / max(box_studs)
    w = min(item_studs[0] * pps, ACC_BOX_FILL_MAX * size)
    h = min(item_studs[1] * pps, ACC_BOX_FILL_MAX * size)
    img = Image.new("RGB", (size, size), P.hex_to_rgb(BG_HEX))
    x0, y0 = (size - w) / 2, (size - h) / 2
    ImageDraw.Draw(img).rounded_rectangle([x0, y0, x0 + w - 1, y0 + h - 1], radius=min(w, h) * 0.18, fill=P.hex_to_rgb(GUIDE_GREY))
    shape = np.asarray(img)[..., 0] == P.hex_to_rgb(GUIDE_GREY)[0]
    editable = P.dilate(shape, round(ACC_BOX_MASK_PAD * size))
    return Guide("guide_acc_box", img, editable, meta={"box_px": (round(x0), round(y0), round(x0 + w), round(y0 + h)), "px_per_stud": pps})


# ------------------------------------------------------------------ guide_regions.json (never sent to a model)
# Default attachment points in studs from the neckline centre (x: image-right positive in the front view, y: up positive). They are an
# approximation of the R15 block rig; pass ``attachments`` (read from mannequin_blocky.json) to use the real ones.
DEFAULT_ATTACHMENTS: dict[str, tuple[float, float]] = {
    "hat": (0.0, 1.1), "hair": (0.0, 1.1), "face_front": (0.0, 0.5), "face_center": (0.0, 0.5), "neck": (0.0, 0.0),
    "right_collar": (-1.0, -0.05), "left_collar": (1.0, -0.05), "right_shoulder": (-1.5, -0.1), "left_shoulder": (1.5, -0.1),
    "body_front": (0.0, -1.0), "body_back": (0.0, -1.0), "waist_front": (0.0, -2.0), "waist_center": (0.0, -2.0), "waist_back": (0.0, -2.0),
}
SHOE_BAND_STUDS = 0.4


def build_guide_regions(g: Guide, attachments: Mapping[str, tuple[float, float]] | None = None) -> dict[str, object]:
    """The content of ``guide_regions.json`` for a ``guide_concept_char``: per figure the pixel box of every template region the view
    shows (the character's right limb is image-left in the front view and image-right in the back view), the head box, the Hair box
    (3 x 5 studs, 2 up and 3 down from the head top), the shoe band and one point per attachment. Code maps ``Print.region`` to a box
    through this file and never guesses."""
    att = dict(DEFAULT_ATTACHMENTS)
    att.update(attachments or {})
    s = PX_PER_STUD
    out: dict[str, object] = {"px_per_stud": s, "figures": {}, "attachments_source": "default" if attachments is None else "given"}
    figures: dict[str, dict[str, object]] = {}
    for name, sl in g.slots.items():
        geo = figure_geometry(sl.cx)
        view = "f" if name.endswith("front") else "b"
        right_arm, left_arm = ("arm_l", "arm_r") if view == "f" else ("arm_r", "arm_l")
        right_leg, left_leg = ("leg_l", "leg_r") if view == "f" else ("leg_r", "leg_l")
        sign = 1.0 if view == "f" else -1.0
        head = geo["head"]
        head_top = head[1]
        hair_box = (sl.cx - int(1.5 * s), head_top - 2 * s, sl.cx + int(1.5 * s), head_top + 3 * s)
        shoes = {k: (geo[k][0], geo[k][3] - int(SHOE_BAND_STUDS * s), geo[k][2], geo[k][3]) for k in ("leg_l", "leg_r")}
        figures[name] = {
            "regions_shirt": {f"torso_{view}": list(geo["torso"]), f"rlimb_{view}": list(geo[right_arm]), f"llimb_{view}": list(geo[left_arm])},
            "regions_pants": {f"rlimb_{view}": list(geo[right_leg]), f"llimb_{view}": list(geo[left_leg])},
            "head_box": list(head), "hair_box": list(hair_box),
            "shoe_band": {"right": list(shoes["leg_l" if view == "f" else "leg_r"]), "left": list(shoes["leg_r" if view == "f" else "leg_l"])},
            "attachments": {k: [round(sl.cx + sign * dx * s), round(NECK_Y - dy * s)] for k, (dx, dy) in att.items()},
        }
    out["figures"] = figures
    return out


def write_guide_regions(g: Guide, path: str, attachments: Mapping[str, tuple[float, float]] | None = None) -> dict[str, object]:
    """Write ``guide_regions.json`` (UTF-8) next to a concept guide and return its content."""
    import json
    from pathlib import Path

    data = build_guide_regions(g, attachments)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return data


# ------------------------------------------------------------------ C2: the 4-up sheet and labels
def assemble_concept_sheet(a: Image.Image, b: Image.Image) -> Image.Image:
    """The 3072x1024 concept sheet, four 768x1024 slots in the order A front, A back, B front, B back, with no rescaling (C2)."""
    for name, im in (("A", a), ("B", b)):
        if im.size != (CANVAS_W, CANVAS_H):
            raise ValueError(f"character {name} concept is {im.size}, expected {(CANVAS_W, CANVAS_H)}")
    sheet = Image.new("RGB", (2 * CANVAS_W, CANVAS_H), P.hex_to_rgb(BG_HEX))
    sheet.paste(a.convert("RGB"), (0, 0))
    sheet.paste(b.convert("RGB"), (CANVAS_W, 0))
    return sheet


def downscale_for_judges(sheet: Image.Image, size: tuple[int, int] = (2304, 768)) -> Image.Image:
    """Uniform downscale of the 4-up sheet for the judges (3072x1024 -> 2304x768)."""
    return sheet.resize(size, Image.Resampling.LANCZOS)


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def add_sheet_labels(sheet: Image.Image, labels: Sequence[str] = ("A · front", "A · back", "B · front", "B · back")) -> Image.Image:
    """A copy with the slot labels drawn by code. Call it **after** every check: the checks never see labels (CHK-A06 would find text)."""
    out = sheet.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    f = _font(max(16, sheet.width // 90))
    slot_w = sheet.width // len(labels)
    for i, text in enumerate(labels):
        d.text((i * slot_w + 16, 12), text, fill=(70, 70, 70), font=f)
    return out


# ------------------------------------------------------------------ §8.3 reference preparation
def prepare_part_crop(concept: Image.Image, box: tuple[int, int, int, int], *, long_edge: int = 768, bg_hex: str = BG_HEX) -> Image.Image:
    """Concept crop for a part asset: cut the box, upscale with Lanczos so the long edge is 512-1024 px, keep the flat #F2F2F2 background
    (transparent areas are flattened onto it, since Image 1 goes opaque, U26)."""
    if not 512 <= long_edge <= 1024:
        raise ValueError("long_edge must be between 512 and 1024")
    crop = concept.convert("RGBA").crop(box)
    flat = Image.new("RGBA", crop.size, P.hex_to_rgb(bg_hex) + (255,))
    flat.alpha_composite(crop)
    k = long_edge / max(flat.size)
    return flat.convert("RGB").resize((max(1, round(flat.width * k)), max(1, round(flat.height * k))), Image.Resampling.LANCZOS)


def side_by_side(images: Sequence[Image.Image], *, gutter: int = 32, bg_hex: str = "#ffffff") -> Image.Image:
    """Several references on one canvas with a 32 px gutter (a call that needs more than 2 references)."""
    if not images:
        raise ValueError("no images")
    h = max(im.height for im in images)
    w = sum(im.width for im in images) + gutter * (len(images) - 1)
    out = Image.new("RGB", (w, h), P.hex_to_rgb(bg_hex))
    x = 0
    for im in images:
        out.paste(im.convert("RGB"), (x, (h - im.height) // 2))
        x += im.width + gutter
    return out


def checkerboard(size: tuple[int, int], cell: int = 8, a: int = 204, b: int = 255) -> Image.Image:
    yy, xx = np.mgrid[:size[1], :size[0]]
    v = np.where(((yy // cell) + (xx // cell)) % 2 == 0, a, b).astype(np.uint8)
    return Image.fromarray(np.stack([v, v, v], -1), "RGB")


def vlm_image(im: Image.Image, *, min_side: int | None = None, max_long_edge: int | None = None, with_checkerboard: bool = True) -> Image.Image:
    """Prepare an image for a Claude or Gemini call (bible §8.3): composite on #808080 and, side by side, on an 8 px checkerboard (so
    transparency is visible), nearest-neighbour upscale so each side is at least 256 px, long edge at most 2576 px."""
    lo = int(TH.get("vlm.min_side_px")) if min_side is None else min_side
    hi = int(TH.get("vlm.max_long_edge")) if max_long_edge is None else max_long_edge
    rgba = im.convert("RGBA")
    k = max(1, -(-lo // min(rgba.size)))
    if k > 1:
        rgba = rgba.resize((rgba.width * k, rgba.height * k), Image.Resampling.NEAREST)
    grey = Image.new("RGBA", rgba.size, (128, 128, 128, 255))
    grey.alpha_composite(rgba)
    panels = [grey.convert("RGB")]
    if with_checkerboard and (np.asarray(rgba)[..., 3] < 255).any():
        cb = checkerboard(rgba.size).convert("RGBA")
        cb.alpha_composite(rgba)
        panels.append(cb.convert("RGB"))
    out = side_by_side(panels, gutter=16, bg_hex="#808080") if len(panels) > 1 else panels[0]
    if max(out.size) > hi:
        s = hi / max(out.size)
        out = out.resize((max(1, round(out.width * s)), max(1, round(out.height * s))), Image.Resampling.LANCZOS)
    return out


READABILITY_PX = 100
READABILITY_PX_BADGE = 80


def readability_preview(im: Image.Image, *, long_edge: int | None = None, badge: bool = False) -> Image.Image:
    """The small version a judge sees for the "readable at 100 px (80 px for badges)" rule: area downscale of the premultiplied image so
    the long edge is that many pixels, composited on grey, then a nearest-neighbour upscale back to at least 256 px (``vlm_image``)."""
    target = long_edge or (READABILITY_PX_BADGE if badge else READABILITY_PX)
    k = target / max(im.size)
    small = im.convert("RGBA").convert("RGBa").resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.Resampling.BOX).convert("RGBA")
    return vlm_image(small, with_checkerboard=False)


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def zone_masks(g: Guide, slot: str = "front") -> Mapping[str, np.ndarray]:
    """Bool masks of the colour zones of a concept guide slot (head, torso, arms, legs) for the palette extraction and A_SWATCH."""
    sl = g.slots[slot]
    geo = figure_geometry(sl.cx)
    out: dict[str, np.ndarray] = {}
    for name, box in geo.items():
        m = np.zeros(g.editable.shape, bool)
        m[box[1]:box[3], box[0]:box[2]] = True
        out[name] = m
    return out
