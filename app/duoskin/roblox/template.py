"""Classic 585x559 Shirt / Pants template: regions, crops, seams, gap fill, bleed, labels and a box preview.

Everything here is derived from ``template_regions.json`` (APP_SPEC §5.2, FAILURE_MODES §5.2, bible §6.1):

* ``REGIONS`` are **inclusive** boxes ``(x0, y0, x1, y1)`` verified pixel for pixel against the official PNGs
  (``tests/clothing/test_template_golden.py``). Use ``crop()``, never ``a[y0:y1, x0:x1]`` (CLO-01).
* Region orientation lives only in the JSON (CLO-05): every face has a 3D frame (outward ``normal``, image ``right``,
  image ``down``) in the three.js frame (Y up, the character faces +Z, the character's RIGHT is -X). Every adjacency
  below (``ADJACENCY``: 36 seams, 12 per body part, including the rotated cap edges) is *derived* from those frames by
  ``derive_adjacency()`` and checked against the stored list at import, so the JSON cannot drift.
* "rlimb"/"llimb" are the CHARACTER's right/left arm (Shirt) or leg (Pants) (CLO-06). ``char_side_to_image_side()``
  is the one helper that maps a character side to a template/image side.

Pure functions only; nothing here touches the network or the disk except reading the JSON once.
"""
from __future__ import annotations

import json
import struct
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal, NamedTuple

import numpy as np

Box = tuple[int, int, int, int]
Side = Literal["top", "bottom", "left", "right"]
SIDES: tuple[Side, ...] = ("top", "bottom", "left", "right")
Kind = Literal["shirt", "pants"]

_JSON_PATH = Path(__file__).with_name("template_regions.json")


def _read_json() -> dict:
    return json.loads(_JSON_PATH.read_text(encoding="utf-8"))


DATA: dict = _read_json()
WIDTH: int = int(DATA["size"][0])
HEIGHT: int = int(DATA["size"][1])
SIZE_WH: tuple[int, int] = (WIDTH, HEIGHT)
GAP_PX: int = int(DATA["gap_px"])

REGIONS: dict[str, Box] = {k: tuple(int(v) for v in r["box"]) for k, r in DATA["regions"].items()}  # type: ignore[misc]
R = REGIONS  # the name used in FAILURE_MODES §5.2
SIZE: dict[str, tuple[int, int]] = {k: (x1 - x0 + 1, y1 - y0 + 1) for k, (x0, y0, x1, y1) in REGIONS.items()}  # (w, h)
REGION_ORDER: tuple[str, ...] = tuple(REGIONS)
REGION_IDS: dict[str, int] = {k: i + 1 for i, k in enumerate(REGION_ORDER)}   # label-map value of each region
REGION_BY_ID: dict[int, str] = {v: k for k, v in REGION_IDS.items()}
PART_OF: dict[str, str] = {k: r["part"] for k, r in DATA["regions"].items()}
FACE_OF: dict[str, str] = {k: r["face"] for k, r in DATA["regions"].items()}
PARTS: dict[str, tuple[str, ...]] = {p: tuple(k for k in REGION_ORDER if PART_OF[k] == p) for p in DATA["parts"]}
SIDE_CYCLE: dict[str, tuple[str, ...]] = {p: tuple(v["side_cycle"]) for p, v in DATA["parts"].items()}
LIMB_PARTS = ("rlimb", "llimb")

assert SIZE["torso_f"] == (128, 128) and SIZE["torso_u"] == (128, 64) and SIZE["torso_r"] == (64, 128)
assert SIZE["rlimb_u"] == (64, 64) and SIZE["rlimb_f"] == (64, 128) and SIZE["llimb_d"] == (64, 64)

# ---- rows and bands (bible §6.1, FAILURE_MODES §4.1; read from the JSON so one file holds the geometry) ----
_ROWS = DATA["rows"]
TORSO_SPLIT_ROW: int = int(_ROWS["torso_split"])                       # UpperTorso rows 74-169, LowerTorso 170-201
UPPER_TORSO_ROWS: tuple[int, int] = tuple(_ROWS["upper_torso"])        # type: ignore[assignment]
LOWER_TORSO_ROWS: tuple[int, int] = tuple(_ROWS["lower_torso"])        # type: ignore[assignment]
TORSO_ROWS: tuple[int, int] = (UPPER_TORSO_ROWS[0], LOWER_TORSO_ROWS[1])
LIMB_ROWS: tuple[int, int] = tuple(_ROWS["limb_rows"])                 # type: ignore[assignment]
LIMB_SPLIT_ROWS: tuple[float, float] = tuple(_ROWS["limb_splits"])     # type: ignore[assignment]  # (418.5, 467)
LIMB_BANDS: tuple[tuple[int, int], ...] = tuple(tuple(b) for b in _ROWS["limb_bands"])  # type: ignore[misc]
DASHED_ROWS: tuple[int, int] = tuple(_ROWS["dashed"])                  # type: ignore[assignment]  # official guides 407, 446
SHOE_TOP_ROW_RANGE: tuple[int, int] = tuple(_ROWS["shoe_top_row_range"])   # type: ignore[assignment]
HIDDEN_LEG_ROWS: tuple[int, int] = tuple(_ROWS["hidden_leg_rows"])     # type: ignore[assignment]  # [UNVERIFIED] FM-T1
TORSO_BANDS: tuple[tuple[int, int], ...] = tuple(tuple(b) for b in _ROWS["torso_bands"])  # type: ignore[misc]  # >=2 px off row 170
FORBIDDEN_ROWS_TORSO: tuple[int, int] = tuple(_ROWS["torso_forbidden"][0])  # type: ignore[assignment]  # edges/details may not lie here
FORBIDDEN_ROWS_LIMB: tuple[tuple[int, int], ...] = tuple(tuple(b) for b in _ROWS["limb_forbidden"])  # type: ignore[misc]
SPLIT_MARGIN_PX: int = int(_ROWS["split_margin_px"])
BEVEL_INSET_PX: int = int(_ROWS["bevel_inset_px"])
OPEN_SIDE_BLEED_PX: tuple[int, int] = tuple(_ROWS["open_side_bleed_px"])   # type: ignore[assignment]  # (2, 4)
DEFAULT_BLEED_PX: int = 3


# --------------------------------------------------------------------------------------------------------------------
# crop / paste
# --------------------------------------------------------------------------------------------------------------------
def crop(a: np.ndarray, k: str) -> np.ndarray:
    """The pixels of region ``k`` as a **view** of ``a`` (H x W or H x W x C). Slices inclusively (CLO-01)."""
    x0, y0, x1, y1 = REGIONS[k]
    out = a[y0:y1 + 1, x0:x1 + 1]
    assert (out.shape[1], out.shape[0]) == SIZE[k], (k, out.shape)
    return out


def paste(a: np.ndarray, k: str, patch: np.ndarray) -> np.ndarray:
    """Write ``patch`` (shaped like ``crop(a, k)``) into region ``k`` of ``a`` in place and return ``a``."""
    dst = crop(a, k)
    if patch.shape != dst.shape:
        raise ValueError(f"patch for {k} must have shape {dst.shape}, got {patch.shape}")
    dst[...] = patch
    return a


def region_slices(k: str) -> tuple[slice, slice]:
    """``(rows, cols)`` slices of region ``k`` for a (H, W, ...) array."""
    x0, y0, x1, y1 = REGIONS[k]
    return slice(y0, y1 + 1), slice(x0, x1 + 1)


def region_slices_scaled(k: str, scale: int) -> tuple[slice, slice]:
    """``(rows, cols)`` slices of region ``k`` on a canvas that is ``scale`` times bigger (author at 4x)."""
    x0, y0, x1, y1 = REGIONS[k]
    return slice(y0 * scale, (y1 + 1) * scale), slice(x0 * scale, (x1 + 1) * scale)


def blank_template() -> np.ndarray:
    """A fully transparent 585x559 RGBA uint8 canvas. The official template PNG is only a guide overlay (CLO-18)."""
    return np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)


@lru_cache(maxsize=1)
def _region_label_map() -> np.ndarray:
    m = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    for k, i in REGION_IDS.items():
        crop(m, k)[...] = i
    m.setflags(write=False)
    return m


def region_label_map() -> np.ndarray:
    """uint8 (559, 585): the id (``REGION_IDS``, 1..18) of the region each pixel belongs to, 0 outside every region."""
    return _region_label_map()


def region_mask(k: str) -> np.ndarray:
    """bool (559, 585) mask of region ``k``."""
    return region_label_map() == REGION_IDS[k]


def region_at(x: int, y: int) -> str | None:
    """The region containing pixel (x, y), or None."""
    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        return None
    i = int(region_label_map()[y, x])
    return REGION_BY_ID.get(i)


def paint_region_ids() -> np.ndarray:
    """RGBA test image where every pixel of region k carries ``REGION_IDS[k]`` in R (G=B=0, A=255); 0 elsewhere.

    Round trip used by CLO-01: ``crop(paint_region_ids(), k)[..., 0] == REGION_IDS[k]`` everywhere and nothing is
    painted outside the boxes."""
    img = blank_template()
    for k, i in REGION_IDS.items():
        c = crop(img, k)
        c[..., 0] = i
        c[..., 3] = 255
    return img


# --------------------------------------------------------------------------------------------------------------------
# 3D frames and adjacency (derived)
# --------------------------------------------------------------------------------------------------------------------
class Frame(NamedTuple):
    normal: tuple[int, int, int]
    right: tuple[int, int, int]
    down: tuple[int, int, int]


FRAMES: dict[str, Frame] = {k: Frame(tuple(f["normal"]), tuple(f["right"]), tuple(f["down"]))  # type: ignore[arg-type]
                            for k, f in DATA["faces"].items()}
PART_DIMS_PX: dict[str, tuple[int, int, int]] = {p: tuple(v["dims_px"]) for p, v in DATA["parts"].items()}  # type: ignore[misc]


def _neg(v: tuple[int, int, int]) -> tuple[int, int, int]:
    return (-v[0], -v[1], -v[2])


def _axis(v: tuple[int, int, int]) -> int:
    return next(i for i, c in enumerate(v) if c)


def region_frame(k: str) -> Frame:
    return FRAMES[FACE_OF[k]]


def _outward(frame: Frame, side: str) -> tuple[int, int, int]:
    return {"top": _neg(frame.down), "bottom": frame.down, "left": _neg(frame.right), "right": frame.right}[side]


def _index_dir(frame: Frame, side: str) -> tuple[int, int, int]:
    """3D direction in which the pixel index along an edge grows (x for top/bottom, y for left/right)."""
    return frame.right if side in ("top", "bottom") else frame.down


class Seam(NamedTuple):
    """Region ``a``'s ``side_a`` edge meets region ``b``'s ``side_b`` edge.

    ``reverse``: pixel i along a's edge (left to right for top/bottom, top to bottom for left/right) touches pixel
    ``n-1-i`` of b's edge. ``kind``: ``side`` (vertical wrap), ``cap`` (UP/DOWN with FRONT) or ``cap_rot`` (a rotated
    cap edge). ``gap``: the two regions are image-adjacent with the 2-px gap (so gap filling applies)."""
    a: str
    side_a: str
    b: str
    side_b: str
    reverse: bool
    kind: str
    gap: bool
    edge: int          # 1..12: the 3D cube edge number (numbered-edge texture)


_EDGE_KEYS: list[frozenset[tuple[int, int, int]]] = []


def _edge_number(na: tuple[int, int, int], nb: tuple[int, int, int]) -> int:
    key = frozenset((na, nb))
    if not _EDGE_KEYS:
        ns = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
        for i, p in enumerate(ns):
            for q in ns[i + 1:]:
                if _axis(p) != _axis(q):
                    _EDGE_KEYS.append(frozenset((p, q)))
        _EDGE_KEYS.sort(key=lambda s: sorted(s))
    return _EDGE_KEYS.index(key) + 1


def _gap_between(box_a: Box, side_a: str, box_b: Box, side_b: str) -> bool:
    opposite = {("top", "bottom"), ("bottom", "top"), ("left", "right"), ("right", "left")}
    if (side_a, side_b) not in opposite:
        return False
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    sep = GAP_PX + 1
    if side_a == "right":
        return (ay0, ay1) == (by0, by1) and bx0 - ax1 == sep
    if side_a == "left":
        return (ay0, ay1) == (by0, by1) and ax0 - bx1 == sep
    if side_a == "bottom":
        return (ax0, ax1) == (bx0, bx1) and by0 - ay1 == sep
    return (ax0, ax1) == (bx0, bx1) and ay0 - by1 == sep


def derive_adjacency(regions: dict[str, Box] | None = None, part_of: dict[str, str] | None = None,
                     face_of: dict[str, str] | None = None) -> list[Seam]:
    """Derive every seam of the three boxes from the face frames (36 seams: 12 per body part)."""
    regions = REGIONS if regions is None else regions
    part_of = PART_OF if part_of is None else part_of
    face_of = FACE_OF if face_of is None else face_of
    rank = {"u": 0, "f": 1, "b": 1, "l": 1, "r": 1, "d": 2}
    found: dict[frozenset, tuple] = {}
    for a in regions:
        fa = FRAMES[face_of[a]]
        for sa in SIDES:
            out = _outward(fa, sa)
            for b in regions:
                if part_of[b] != part_of[a] or b == a:
                    continue
                fb = FRAMES[face_of[b]]
                if fb.normal != out:
                    continue
                sb = next(s for s in SIDES if _outward(fb, s) == fa.normal)
                rev = _index_dir(fa, sa) != _index_dir(fb, sb)
                key = frozenset(((a, sa), (b, sb)))
                found.setdefault(key, (a, sa, b, sb, rev))
    seams: list[Seam] = []
    for (a, sa, b, sb, rev) in found.values():
        ra, rb = rank[face_of[a]], rank[face_of[b]]
        # canonical orientation: the cap (u first, d last) is `a` for U and `b` for D; two sides: a's side is "right"
        if (ra, rb) == (1, 1):
            if sa != "right":
                a, sa, b, sb = b, sb, a, sa
        elif ra > rb:
            a, sa, b, sb = b, sb, a, sa
        fa_, fb_ = face_of[a], face_of[b]
        if fa_ == "u" and fb_ == "f" or fa_ == "f" and fb_ == "d":
            kind = "cap"
        elif fa_ in "ud" or fb_ in "ud":
            kind = "cap_rot"
        else:
            kind = "side"
        rev = _index_dir(FRAMES[fa_], sa) != _index_dir(FRAMES[fb_], sb)
        gap = _gap_between(regions[a], sa, regions[b], sb)
        edge = _edge_number(_outward(FRAMES[fa_], sa), _outward(FRAMES[fb_], sb))
        seams.append(Seam(a, sa, b, sb, rev, kind, gap, edge))
    order_part = {p: i for i, p in enumerate(PARTS)}
    kind_rank = {"side": 0, "cap": 1, "cap_rot": 2}

    def sort_key(s: Seam) -> tuple:
        part = part_of[s.a]
        if s.kind == "side":
            sub = SIDE_CYCLE[part].index(s.a)
        else:
            cap_first = face_of[s.a] == "u"
            other = face_of[s.b] if cap_first else face_of[s.a]
            sub = (0 if cap_first else 1) if s.kind == "cap" else (0 if cap_first else 10) + "rlbf".index(other)
        return (order_part[part], kind_rank[s.kind], sub)

    return sorted(seams, key=sort_key)


def _seams_from_json() -> list[Seam]:
    return [Seam(s["a"], s["side_a"], s["b"], s["side_b"], bool(s["reverse"]), s["kind"], bool(s["gap"]), int(s["edge"]))
            for s in DATA.get("seams", [])]


ADJACENCY: tuple[Seam, ...] = tuple(_seams_from_json() or derive_adjacency())
assert list(ADJACENCY) == derive_adjacency(), "template_regions.json seams drifted from the face frames"
SIDE_SEAMS: list[tuple[str, str]] = [(s.a, s.b) for s in ADJACENCY if s.kind == "side"]
CAP_SEAMS: list[tuple[str, str]] = [(s.a, s.b) for s in ADJACENCY if s.kind == "cap"]
ROTATED_CAP_SEAMS: list[tuple[str, str]] = [(s.a, s.b) for s in ADJACENCY if s.kind == "cap_rot"]


def seams_of(region: str) -> list[Seam]:
    """Every seam that touches ``region`` (as a or b)."""
    return [s for s in ADJACENCY if region in (s.a, s.b)]


def neighbour(region: str, side: str) -> tuple[str, str, bool] | None:
    """``(other_region, other_side, reverse)`` across ``side`` of ``region`` (reverse: indices run opposite)."""
    for s in ADJACENCY:
        if s.a == region and s.side_a == side:
            return s.b, s.side_b, s.reverse
        if s.b == region and s.side_b == side:
            return s.a, s.side_a, s.reverse
    return None


def _edge_index(box: Box, side: str) -> tuple[slice | int, slice | int]:
    x0, y0, x1, y1 = box
    if side == "top":
        return y0, slice(x0, x1 + 1)
    if side == "bottom":
        return y1, slice(x0, x1 + 1)
    if side == "left":
        return slice(y0, y1 + 1), x0
    if side == "right":
        return slice(y0, y1 + 1), x1
    raise ValueError(side)


def edge_pixels(img: np.ndarray, region: str, side: str, depth: int = 0) -> np.ndarray:
    """Pixels of an edge line of ``region``, ordered left to right (top/bottom) or top to bottom (left/right).

    ``depth`` counts lines inward from the edge (0 = the outermost row/column)."""
    x0, y0, x1, y1 = REGIONS[region]
    if side == "top":
        return img[y0 + depth, x0:x1 + 1]
    if side == "bottom":
        return img[y1 - depth, x0:x1 + 1]
    if side == "left":
        return img[y0:y1 + 1, x0 + depth]
    if side == "right":
        return img[y0:y1 + 1, x1 - depth]
    raise ValueError(side)


def seam_edges(img: np.ndarray, seam: Seam, depth: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """The two edge lines of ``seam``, ``b``'s reordered so index i of both touches the same 3D point."""
    ea = edge_pixels(img, seam.a, seam.side_a, depth)
    eb = edge_pixels(img, seam.b, seam.side_b, depth)
    return ea, (eb[::-1] if seam.reverse else eb)


# --------------------------------------------------------------------------------------------------------------------
# gap fill and open-side bleed (CLO-03)
# --------------------------------------------------------------------------------------------------------------------
def _outside_line(box: Box, side: str, depth: int = 1) -> tuple[slice | int, slice | int]:
    x0, y0, x1, y1 = box
    if side == "top":
        return y0 - depth, slice(x0, x1 + 1)
    if side == "bottom":
        return y1 + depth, slice(x0, x1 + 1)
    if side == "left":
        return slice(y0, y1 + 1), x0 - depth
    return slice(y0, y1 + 1), x1 + depth


def fill_shared_gaps(img: np.ndarray) -> np.ndarray:
    """CLO-03: fill every shared 2-px gap 1 px per side with each owner's edge pixels (exact RGBA).

    Region interiors are never touched (asserted). Open sides are handled by ``bleed_open_sides``."""
    out = img.copy()
    for s in ADJACENCY:
        if not s.gap:
            continue
        ya, xa = _outside_line(REGIONS[s.a], s.side_a)
        yb, xb = _outside_line(REGIONS[s.b], s.side_b)
        out[ya, xa] = edge_pixels(img, s.a, s.side_a)
        out[yb, xb] = edge_pixels(img, s.b, s.side_b)
    for k in REGIONS:
        assert np.array_equal(crop(out, k), crop(img, k)), k
    return out


def gapped_sides() -> dict[str, set[str]]:
    """For every region, the sides that face another region across the 2-px gap."""
    d: dict[str, set[str]] = {k: set() for k in REGIONS}
    for s in ADJACENCY:
        if s.gap:
            d[s.a].add(s.side_a)
            d[s.b].add(s.side_b)
    return d


def open_sides(region: str) -> tuple[str, ...]:
    """Sides of ``region`` that are not gapped (outer template edges and the rotated cap edges)."""
    g = gapped_sides()[region]
    return tuple(s for s in SIDES if s not in g)


@lru_cache(maxsize=8)
def _ring_tables(open_px: int, gap_each: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(ys, xs, sy, sx, owner): every ring pixel, the pixel it copies and the region id that owns it."""
    gapped = gapped_sides()
    best = np.full((HEIGHT, WIDTH), np.inf)
    owner = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    for k in REGION_ORDER:
        x0, y0, x1, y1 = REGIONS[k]
        depth = {s: (gap_each if s in gapped[k] else open_px) for s in SIDES}
        rx0, rx1 = max(0, x0 - depth["left"]), min(WIDTH - 1, x1 + depth["right"])
        ry0, ry1 = max(0, y0 - depth["top"]), min(HEIGHT - 1, y1 + depth["bottom"])
        ys, xs = np.mgrid[ry0:ry1 + 1, rx0:rx1 + 1]
        dx = np.maximum(np.maximum(x0 - xs, xs - x1), 0)
        dy = np.maximum(np.maximum(y0 - ys, ys - y1), 0)
        d2 = (dx * dx + dy * dy).astype(float)
        d2[(dx == 0) & (dy == 0)] = np.inf                       # inside the box itself
        sl = (slice(ry0, ry1 + 1), slice(rx0, rx1 + 1))
        better = d2 < best[sl]
        best[sl] = np.where(better, d2, best[sl])
        owner[sl] = np.where(better, REGION_IDS[k], owner[sl])
    outside = region_label_map() == 0
    ys, xs = np.nonzero(np.isfinite(best) & outside)
    ids = owner[ys, xs]
    bx0 = np.array([REGIONS[REGION_BY_ID[i]][0] for i in range(1, len(REGION_ORDER) + 1)])
    by0 = np.array([REGIONS[REGION_BY_ID[i]][1] for i in range(1, len(REGION_ORDER) + 1)])
    bx1 = np.array([REGIONS[REGION_BY_ID[i]][2] for i in range(1, len(REGION_ORDER) + 1)])
    by1 = np.array([REGIONS[REGION_BY_ID[i]][3] for i in range(1, len(REGION_ORDER) + 1)])
    sx = np.clip(xs, bx0[ids - 1], bx1[ids - 1])
    sy = np.clip(ys, by0[ids - 1], by1[ids - 1])
    return ys, xs, sy, sx, ids


def owner_map(open_px: int = DEFAULT_BLEED_PX, gaps: bool = True) -> np.ndarray:
    """uint8 (559, 585): region id for every region pixel plus every ring pixel (1 px per side into shared gaps, then
    ``open_px`` on open sides); 0 elsewhere. Corner pixels are owned by the nearest region."""
    ys, xs, _, _, ids = _ring_tables(open_px, 1 if gaps else 0)
    m = region_label_map().copy()
    m[ys, xs] = ids
    return m


def fill_gaps(img: np.ndarray, owner: np.ndarray | None = None) -> np.ndarray:
    """Gap fill only (APP_SPEC §5.4 name). Equivalent to ``fill_shared_gaps``; ``owner`` is accepted for API symmetry."""
    return fill_shared_gaps(img)


def bleed_open_sides(img: np.ndarray, px: int = DEFAULT_BLEED_PX) -> np.ndarray:
    """Dilate ``px`` (2-4) pixels outward from every OPEN side (outer template edges, rotated cap edges) by copying
    the region's edge pixel; the shared gaps get exactly 1 px per side. Only non-region pixels change (asserted)."""
    if not OPEN_SIDE_BLEED_PX[0] <= px <= OPEN_SIDE_BLEED_PX[1]:
        raise ValueError(f"open-side bleed must be {OPEN_SIDE_BLEED_PX[0]}-{OPEN_SIDE_BLEED_PX[1]} px, got {px}")
    ys, xs, sy, sx, _ = _ring_tables(px, 1)
    out = img.copy()
    out[ys, xs] = img[sy, sx]
    inside = region_label_map() > 0
    assert np.array_equal(out[inside], img[inside]), "bleed touched a region interior"
    return out


def finish_edges(img: np.ndarray, px: int = DEFAULT_BLEED_PX) -> np.ndarray:
    """Last compositor step: gap fill (1 px per side) + open-side bleed (``px``). Identical to ``bleed_open_sides``
    because the ring tables already give each shared gap 1 px per side; kept as the documented single entry point."""
    return bleed_open_sides(fill_shared_gaps(img), px)


@lru_cache(maxsize=4)
def _allowed(bleed_max: int) -> np.ndarray:
    m = region_label_map() > 0
    ys, xs, _, _, _ = _ring_tables(bleed_max, 1)
    m = m.copy()
    m[ys, xs] = True
    m.setflags(write=False)
    return m


def allowed_pixels(bleed_px: int = OPEN_SIDE_BLEED_PX[1]) -> np.ndarray:
    """bool (559, 585): the 18 boxes plus their bleed ring (1 px into shared gaps, ``bleed_px`` on open sides).
    Everything else must be alpha 0 (CLO-18)."""
    return _allowed(bleed_px)


# --------------------------------------------------------------------------------------------------------------------
# character side naming (CLO-06)
# --------------------------------------------------------------------------------------------------------------------
def char_side_to_image_side(char_side: str) -> str:
    """Map a CHARACTER side to the side of the template/front view where it appears.

    The character's right arm/leg occupies template x 19-280, the left x 308-569. In a front view the viewer sees the
    character's right on the viewer's LEFT, so ``"right"`` -> ``"image_left"``."""
    s = char_side.strip().lower()
    if s in ("right", "r", "rlimb"):
        return "image_left"
    if s in ("left", "l", "llimb"):
        return "image_right"
    raise ValueError(f"unknown character side {char_side!r}")


def limb_prefix(char_side: str) -> str:
    """``"right"`` -> ``"rlimb"``, ``"left"`` -> ``"llimb"`` (names are always the character's own side)."""
    s = char_side.strip().lower()
    if s in ("right", "r", "rlimb"):
        return "rlimb"
    if s in ("left", "l", "llimb"):
        return "llimb"
    raise ValueError(f"unknown character side {char_side!r}")


def limb_region(char_side: str, face: str) -> str:
    """Region id of a limb face for a character side, e.g. ``limb_region("right", "f") == "rlimb_f"``."""
    f = face.strip().lower()
    if f not in ("u", "d", "f", "b", "l", "r"):
        raise ValueError(f"unknown face {face!r}")
    return f"{limb_prefix(char_side)}_{f}"


def limb_face_role(region: str) -> str:
    """``front | back | inner | outer | top | bottom`` for a limb face (inner = towards the other limb/body)."""
    part, face = region.split("_")
    if part not in LIMB_PARTS:
        raise ValueError(f"{region} is not a limb region")
    if face in ("f", "b"):
        return {"f": "front", "b": "back"}[face]
    if face in ("u", "d"):
        return {"u": "top", "d": "bottom"}[face]
    # right limb: +X ("l" face) is towards the body centre; left limb: -X ("r" face) is
    if part == "rlimb":
        return "inner" if face == "l" else "outer"
    return "inner" if face == "r" else "outer"


# --------------------------------------------------------------------------------------------------------------------
# world positions, position texture and the numbered-edge texture (CLO-05 / CLO-06 goldens)
# --------------------------------------------------------------------------------------------------------------------
def face_world_points(region: str) -> np.ndarray:
    """float64 (h, w, 3): the 3D position (template pixel units, centred on the part box, +Z front, +X the character's
    left) of every pixel centre of ``region``."""
    fr = region_frame(region)
    dims = np.array(PART_DIMS_PX[PART_OF[region]], dtype=float)
    n, r, d = (np.array(v, dtype=float) for v in fr)
    wf, hf = dims[_axis(fr.right)], dims[_axis(fr.down)]
    hn = dims[_axis(fr.normal)] / 2.0
    w, h = SIZE[region]
    assert (wf, hf) == (w, h), (region, (wf, hf), (w, h))
    uu = np.arange(w) + 0.5 - wf / 2.0
    vv = np.arange(h) + 0.5 - hf / 2.0
    return n * hn + uu[None, :, None] * r + vv[:, None, None] * d


def position_texture() -> np.ndarray:
    """RGBA uint8 template where RGB encodes the 3D position of each pixel on its box (R=x, G=y, B=z, each 0-255
    across the box; A=255 inside regions). Continuous across every seam when the orientation table is right."""
    img = blank_template()
    for k in REGION_ORDER:
        dims = np.array(PART_DIMS_PX[PART_OF[k]], dtype=float)
        p = face_world_points(k)
        rgb = np.rint((p / dims + 0.5) * 255.0).clip(0, 255).astype(np.uint8)
        c = crop(img, k)
        c[..., :3] = rgb
        c[..., 3] = 255
    return img


def _build_font() -> dict[str, list[str]]:
    rows = {
        "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
        "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
        "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
        "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
        "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    }
    return rows


_FONT = _build_font()


def draw_digits(img: np.ndarray, text: str, x: int, y: int, scale: int = 2, fg=(255, 255, 255, 255),
                bg=(0, 0, 0, 255)) -> tuple[int, int]:
    """Draw digits (3x5 font, upright in image space) with a 1-px chip in ``bg``; returns the drawn (w, h)."""
    gw = 3 * scale
    w = len(text) * (gw + scale) - scale
    h = 5 * scale
    ya, yb = max(y - 1, 0), min(y + h + 1, img.shape[0])
    xa, xb = max(x - 1, 0), min(x + w + 1, img.shape[1])
    img[ya:yb, xa:xb] = bg
    for ci, ch in enumerate(text):
        glyph = _FONT[ch]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    px, py = x + ci * (gw + scale) + gx * scale, y + gy * scale
                    img[py:py + scale, px:px + scale] = fg
    return w, h


def numbered_edge_texture(glyphs: bool = True) -> np.ndarray:
    """The golden numbered-edge test texture (FAILURE_MODES CLO-05, §17.1).

    Base = ``position_texture()`` (continuous across every correct seam). With ``glyphs`` every region edge that is
    part of a seam carries its 3D cube-edge number (1-12), upright in that region's own image space. Rendered through
    any three.js ``BoxGeometry`` with this map, each cube edge shows the same number on both faces it joins."""
    img = position_texture()
    if not glyphs:
        return img
    for s in ADJACENCY:
        for region, side in ((s.a, s.side_a), (s.b, s.side_b)):
            x0, y0, x1, y1 = REGIONS[region]
            text = f"{s.edge:02d}"
            tw, th = 14, 10
            cx, cy = (x0 + x1 + 1) // 2, (y0 + y1 + 1) // 2
            if side == "top":
                gx, gy = cx - tw // 2, y0 + 2
            elif side == "bottom":
                gx, gy = cx - tw // 2, y1 - th - 2
            elif side == "left":
                gx, gy = x0 + 2, cy - th // 2
            else:
                gx, gy = x1 - tw - 2, cy - th // 2
            draw_digits(img, text, gx, gy)
    return img


def letter_texture() -> np.ndarray:
    """RGBA uint8 template where each region carries a distinct flat colour and a large digit = its region id (01-18),
    upright in image space. Used for the R/L (CLO-06) golden render."""
    img = blank_template()
    palette = [(230, 60, 50), (60, 180, 80), (60, 110, 220), (240, 190, 40), (160, 80, 200), (60, 200, 200)]
    for k in REGION_ORDER:
        c = crop(img, k)
        c[...] = (*palette[(REGION_IDS[k] - 1) % len(palette)], 255)
        x0, y0, x1, y1 = REGIONS[k]
        draw_digits(img, f"{REGION_IDS[k]:02d}", (x0 + x1) // 2 - 12, (y0 + y1) // 2 - 10, scale=4,
                    fg=(255, 255, 255, 255), bg=(20, 20, 20, 255))
    return img


# --------------------------------------------------------------------------------------------------------------------
# PNG facts and writing (CLO-02)
# --------------------------------------------------------------------------------------------------------------------
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOUR_CHUNKS = frozenset({"iCCP", "gAMA", "sRGB", "cHRM", "sBIT"})


class PngFacts(NamedTuple):
    is_png: bool
    width: int
    height: int
    bit_depth: int
    color_type: int            # 6 = RGBA
    interlace: int
    chunks: tuple[str, ...]

    @property
    def is_rgba8(self) -> bool:
        return self.is_png and self.bit_depth == 8 and self.color_type == 6

    @property
    def colour_chunks(self) -> tuple[str, ...]:
        return tuple(c for c in self.chunks if c in COLOUR_CHUNKS)


def inspect_png(data: bytes) -> PngFacts:
    """Parse the chunk list and IHDR of PNG bytes without decoding pixels. Never raises on garbage input."""
    if not data.startswith(PNG_SIGNATURE):
        return PngFacts(False, 0, 0, 0, -1, 0, ())
    pos = len(PNG_SIGNATURE)
    chunks: list[str] = []
    w = h = depth = ctype = interlace = 0
    try:
        while pos + 8 <= len(data):
            length, ctag = struct.unpack(">I4s", data[pos:pos + 8])
            name = ctag.decode("latin-1")
            body = data[pos + 8:pos + 8 + length]
            chunks.append(name)
            if name == "IHDR":
                w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body[:13])
            pos += 12 + length
            if name == "IEND":
                break
    except (struct.error, UnicodeDecodeError):
        return PngFacts(False, 0, 0, 0, -1, 0, tuple(chunks))
    return PngFacts(True, w, h, depth, ctype, interlace, tuple(chunks))


def encode_png_rgba8(arr: np.ndarray) -> bytes:
    """Encode a (H, W, 4) uint8 array as a plain PNG with no colour chunks (no gAMA/iCCP/sRGB/cHRM)."""
    import io

    from PIL import Image

    if arr.dtype != np.uint8 or arr.ndim != 3 or arr.shape[2] != 4:
        raise ValueError(f"need (H, W, 4) uint8, got {arr.shape} {arr.dtype}")
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr), "RGBA").save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def decode_png_rgba8(data: bytes) -> np.ndarray:
    """Decode PNG bytes to (H, W, 4) uint8 (any mode is converted to RGBA; use ``inspect_png`` for the facts)."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return np.array(im.convert("RGBA"), dtype=np.uint8)


def write_template(arr: np.ndarray) -> bytes:
    """The single template writer (CLO-02): 585x559 RGBA 8-bit PNG, colour chunks absent; re-opened and verified."""
    if arr.shape != (HEIGHT, WIDTH, 4):
        raise ValueError(f"template must be {WIDTH}x{HEIGHT} RGBA, got {arr.shape}")
    data = encode_png_rgba8(arr)
    facts = inspect_png(data)
    if not (facts.is_rgba8 and (facts.width, facts.height) == SIZE_WH and not facts.colour_chunks
            and facts.interlace == 0):
        raise AssertionError(f"template writer produced a non-conforming PNG: {facts}")
    back = decode_png_rgba8(data)
    if not np.array_equal(back, arr):
        raise AssertionError("template PNG did not round-trip")
    return data


# --------------------------------------------------------------------------------------------------------------------
# 3D box preview (numpy raster; Gate 2 tiles and the orientation goldens)
# --------------------------------------------------------------------------------------------------------------------
# The blocky mannequin used by the preview (template px units; +Z front, +X the character's left, so the character's
# RIGHT arm/leg sit at -X). (part, centre, which texture): arms take the Shirt only, legs the Pants only (bible §6.1).
_MANNEQUIN: tuple[tuple[str, tuple[float, float, float], str], ...] = (
    ("torso", (0.0, 0.0, 0.0), "torso"),
    ("rlimb", (-96.0, 0.0, 0.0), "arm"), ("llimb", (96.0, 0.0, 0.0), "arm"),
    ("rlimb", (-32.0, -128.0, 0.0), "leg"), ("llimb", (32.0, -128.0, 0.0), "leg"),
)
_MANNEQUIN_NAMES = ("torso", "rarm", "larm", "rleg", "lleg")
_HEAD_BOX = ((0.0, 96.0, 0.0), (64.0, 64.0, 64.0))


def _over(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    """Straight-alpha 'over' of two RGBA uint8 images (top over base)."""
    a_t = top[..., 3:4].astype(np.float32) / 255.0
    a_b = base[..., 3:4].astype(np.float32) / 255.0
    a_o = a_t + a_b * (1.0 - a_t)
    rgb = np.where(a_o > 0, (top[..., :3] * a_t + base[..., :3] * a_b * (1.0 - a_t)) / np.maximum(a_o, 1e-9), 0.0)
    return np.dstack([np.rint(rgb), np.rint(a_o * 255.0)]).astype(np.uint8)


def composite_clothing(shirt: np.ndarray | None, pants: np.ndarray | None,
                       skin: tuple[int, int, int] = (214, 170, 140)) -> np.ndarray:
    """Body colour -> Pants -> Shirt (the Shirt covers the Pants on the torso; arms Shirt only; legs Pants only).

    Returns an opaque RGBA template-sized texture where transparent clothing shows ``skin`` (bible §6.1 layering)."""
    out = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    out[..., :3] = skin
    out[..., 3] = 255
    for layer in (pants, shirt):
        if layer is not None:
            out = _over(out, layer)
    return out


def render_box_preview(shirt: np.ndarray | None = None, pants: np.ndarray | None = None, *, yaw_deg: float = 32.0,
                       pitch_deg: float = 18.0, size: tuple[int, int] = (320, 400), supersample: int = 2,
                       skin: tuple[int, int, int] = (214, 170, 140), head: tuple[int, int, int] | None = None,
                       background: tuple[int, int, int, int] = (238, 238, 238, 255),
                       texture: np.ndarray | None = None, only: Sequence[str] | None = None) -> np.ndarray:
    """Software-rasterise the blocky mannequin (torso, two arms, two legs, a head box) with the template mapped through
    the same face frames the JSON defines (orthographic camera, z-buffer, back faces culled).

    ``texture`` overrides the shirt/pants composite (the numbered-edge and letter goldens use it). ``only`` limits the figure to
    some of ``torso, rarm, larm, rleg, lleg, head`` (the orientation goldens render the torso alone). Returns RGBA uint8
    of ``size`` = (width, height). Character faces +Z; ``yaw_deg`` > 0 turns the character's left side to the camera."""
    ss = max(1, int(supersample))
    W, H = size[0] * ss, size[1] * ss
    zbuf = np.full((H, W), -np.inf)
    canvas = np.empty((H, W, 4), dtype=np.uint8)
    canvas[...] = background
    ya, pa = np.deg2rad(yaw_deg), np.deg2rad(pitch_deg)
    cy, sy_, cp, sp = np.cos(ya), np.sin(ya), np.cos(pa), np.sin(pa)
    rot_y = np.array([[cy, 0, sy_], [0, 1, 0], [-sy_, 0, cy]])
    rot_x = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    rot = rot_x @ rot_y
    view = np.array([0.0, 0.0, 1.0])                         # towards the camera (+Z after rotation)
    scale = 0.9 * min(W / 330.0, H / 460.0)
    centre = np.array([W / 2.0, H / 2.0 + 0.0 * scale])

    def project(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        q = (rot @ p.T).T
        return np.stack([centre[0] + q[:, 0] * scale, centre[1] - q[:, 1] * scale], axis=1), q[:, 2]

    def draw_face(origin: np.ndarray, right: np.ndarray, down: np.ndarray, wpx: float, hpx: float,
                  sampler: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> None:
        o2, oz = project(origin[None, :])
        r2, rz = project((origin + right * wpx)[None, :])
        d2, dz = project((origin + down * hpx)[None, :])
        a = (r2[0] - o2[0])
        b = (d2[0] - o2[0])
        det = a[0] * b[1] - a[1] * b[0]
        if abs(det) < 1e-9:
            return
        pts = np.array([o2[0], r2[0], d2[0], o2[0] + a + b])
        x_lo, x_hi = int(max(np.floor(pts[:, 0].min()), 0)), int(min(np.ceil(pts[:, 0].max()), W - 1))
        y_lo, y_hi = int(max(np.floor(pts[:, 1].min()), 0)), int(min(np.ceil(pts[:, 1].max()), H - 1))
        if x_hi < x_lo or y_hi < y_lo:
            return
        ys, xs = np.mgrid[y_lo:y_hi + 1, x_lo:x_hi + 1]
        px, py = xs + 0.5 - o2[0][0], ys + 0.5 - o2[0][1]
        u = (px * b[1] - py * b[0]) / det
        v = (a[0] * py - a[1] * px) / det
        inside = (u >= 0) & (u < 1) & (v >= 0) & (v < 1)
        z = oz[0] + u * (rz[0] - oz[0]) + v * (dz[0] - oz[0])
        sub = zbuf[y_lo:y_hi + 1, x_lo:x_hi + 1]
        take = inside & (z > sub)
        if not take.any():
            return
        col = sampler(u[take], v[take])
        sub[take] = z[take]
        canvas[y_lo:y_hi + 1, x_lo:x_hi + 1][take] = col

    def tex_sampler(region: str, tex: np.ndarray) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
        x0, y0, x1, y1 = REGIONS[region]
        w, h = SIZE[region]

        def sample(u: np.ndarray, v: np.ndarray) -> np.ndarray:
            ix = np.clip((u * w).astype(int), 0, w - 1) + x0
            iy = np.clip((v * h).astype(int), 0, h - 1) + y0
            return tex[iy, ix]
        return sample

    def flat_sampler(rgb: tuple[int, int, int]) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
        col = np.array([*rgb, 255], dtype=np.uint8)
        return lambda u, v: np.broadcast_to(col, (u.shape[0], 4))

    if texture is not None:
        tex_torso = tex_arms = tex_legs = texture
    else:
        tex_torso = composite_clothing(shirt, pants, skin)
        tex_arms = composite_clothing(shirt, None, skin)
        tex_legs = composite_clothing(None, pants, skin)
    for (part, part_centre, t), name in zip(_MANNEQUIN, _MANNEQUIN_NAMES):
        if only is not None and name not in only:
            continue
        dims = np.array(PART_DIMS_PX[part], dtype=float)
        ctr = np.array(part_centre, dtype=float)
        for region in PARTS[part]:
            fr = region_frame(region)
            n, r, d = (np.array(v, dtype=float) for v in fr)
            if (rot @ n) @ view <= 1e-9:
                continue
            wf, hf = dims[_axis(fr.right)], dims[_axis(fr.down)]
            hn = dims[_axis(fr.normal)] / 2.0
            origin = ctr + n * hn - r * (wf / 2.0) - d * (hf / 2.0)
            draw_face(origin, r, d, wf, hf, tex_sampler(region, {"torso": tex_torso, "arm": tex_arms, "leg": tex_legs}[t]))
    hc, hd = np.array(_HEAD_BOX[0]), np.array(_HEAD_BOX[1])
    hcol = head if head is not None else skin
    for fname, fr in (FRAMES.items() if (only is None or "head" in only) else ()):
        n, r, d = (np.array(v, dtype=float) for v in fr)
        if (rot @ n) @ view <= 1e-9:
            continue
        wf, hf = hd[_axis(fr.right)], hd[_axis(fr.down)]
        origin = hc + n * (hd[_axis(fr.normal)] / 2.0) - r * (wf / 2.0) - d * (hf / 2.0)
        shade = 0.82 if fname in ("l", "r") else (1.0 if fname == "f" else 0.9)
        draw_face(origin, r, d, wf, hf, flat_sampler(tuple(int(c * shade) for c in hcol)))
    if ss > 1:
        canvas = canvas.reshape(size[1], ss, size[0], ss, 4).astype(np.uint32).sum(axis=(1, 3)) // (ss * ss)
        canvas = canvas.astype(np.uint8)
    return canvas


# --------------------------------------------------------------------------------------------------------------------
# flat front / back preview (Gate 2 'shirt/pants flat front/back' tiles)
# --------------------------------------------------------------------------------------------------------------------
def _as_rgba(x: object) -> np.ndarray | None:
    if x is None:
        return None
    if isinstance(x, np.ndarray):
        arr = x
    elif isinstance(x, (bytes, bytearray)):
        arr = decode_png_rgba8(bytes(x))
    elif isinstance(x, Path):
        arr = decode_png_rgba8(x.read_bytes())
    else:  # PIL image
        arr = np.array(x.convert("RGBA"), dtype=np.uint8)  # type: ignore[attr-defined]
    if arr.shape != (HEIGHT, WIDTH, 4) or arr.dtype != np.uint8:
        raise ValueError(f"expected a {WIDTH}x{HEIGHT} RGBA uint8 template, got {arr.shape} {arr.dtype}")
    return arr


def unfold_flat(shirt: np.ndarray | None, pants: np.ndarray | None, view: Literal["front", "back"],
                skin: tuple[int, int, int] = (214, 170, 140)) -> np.ndarray:
    """Unfold the template into a flat figure (no head), 256x256 RGBA: row 1 = [arm | torso | arm] (64+128+64 wide, 128
    tall), row 2 = [leg | leg] under the torso. Torso = body colour -> Pants -> Shirt; arms = Shirt only; legs = Pants only.

    Front view: the character's RIGHT arm/leg is on the viewer's left (template x 19-280, CLO-06), faces F. Back view: the
    viewer sees the character's LEFT on the left, faces B. Nothing is mirrored: every face is copied as authored."""
    t_torso = composite_clothing(shirt, pants, skin)
    t_arm = composite_clothing(shirt, None, skin)
    t_leg = composite_clothing(None, pants, skin)
    f = "f" if view == "front" else "b"
    vl, vr = ("rlimb", "llimb") if view == "front" else ("llimb", "rlimb")   # viewer-left / viewer-right limb
    out = np.zeros((256, 256, 4), dtype=np.uint8)
    out[0:128, 64:192] = crop(t_torso, f"torso_{f}")
    out[0:128, 0:64] = crop(t_arm, f"{vl}_{f}")
    out[0:128, 192:256] = crop(t_arm, f"{vr}_{f}")
    out[128:256, 64:128] = crop(t_leg, f"{vl}_{f}")
    out[128:256, 128:192] = crop(t_leg, f"{vr}_{f}")
    return out


def render_flat_preview(shirt_png: object | None, pants_png: object | None, *, skin: tuple[int, int, int] = (214, 170, 140),
                        scale: int = 2, background: tuple[int, int, int, int] = (238, 238, 238, 255)
                        ) -> tuple[bytes, bytes]:
    """Unfold the template into flat FRONT and BACK views and return ``(front_png, back_png)`` (Gate 2 tiles).

    ``shirt_png`` / ``pants_png`` may each be PNG bytes, a path, a PIL image, an RGBA ndarray or None. Transparent clothing
    shows ``skin``. Torso FRONT/BACK 128x128, arms and legs 64x128, upscaled ``scale`` times with nearest neighbour."""
    shirt, pants = _as_rgba(shirt_png), _as_rgba(pants_png)
    outs: list[bytes] = []
    for view in ("front", "back"):
        flat = unfold_flat(shirt, pants, view, skin)  # type: ignore[arg-type]
        canvas = np.empty((256 + 16, 256 + 16, 4), dtype=np.uint8)
        canvas[...] = background
        canvas[8:264, 8:264] = np.where(flat[..., 3:4] > 0, flat, canvas[8:264, 8:264])
        if scale > 1:
            canvas = np.repeat(np.repeat(canvas, scale, axis=0), scale, axis=1)
        outs.append(encode_png_rgba8(canvas))
    return outs[0], outs[1]


def iter_region_pixels(img: np.ndarray, regions: Iterable[str] | None = None) -> Iterable[tuple[str, np.ndarray]]:
    """Yield ``(region, crop view)`` for the given regions (all by default)."""
    for k in (REGION_ORDER if regions is None else regions):
        yield k, crop(img, k)


def bands_of(region: str) -> tuple[tuple[int, int], ...]:
    """Row bands of ``region`` in which a print, trim or code-placed piece must lie entirely: torso faces 74-168 and
    172-201 (>= 2 px off row 170), limb side faces 355-416, 421-465 and 469-482 (off 418/419 and 467); cap faces have a
    single band covering the whole region."""
    x0, y0, x1, y1 = REGIONS[region]
    if FACE_OF[region] in ("u", "d"):
        return ((y0, y1),)
    return TORSO_BANDS if PART_OF[region] == "torso" else LIMB_BANDS


def band_of_rows(region: str, y0: int, y1: int) -> tuple[int, int] | None:
    """The band of ``region`` that contains rows y0..y1 entirely, or None when they cross a split row."""
    for b in bands_of(region):
        if b[0] <= y0 and y1 <= b[1]:
            return b
    return None


def strip_layout(part: str) -> tuple[dict[str, float], float]:
    """Perimeter offsets (template px) of the four side faces of ``part`` in wrap order, and the strip length.

    The side faces of a part form one continuous strip (CLO-04): fabric, hem stitches and rib lines are laid out on it so a
    pattern keeps its phase across every vertical seam, including the wrap from the last face back to the first."""
    off: dict[str, float] = {}
    pos = 0.0
    for region in SIDE_CYCLE[part]:
        off[region] = pos
        pos += SIZE[region][0]
    return off, pos
