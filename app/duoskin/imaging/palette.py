"""Colour science and palette tools: CIELAB, CIEDE2000, k-means palette extraction, snapping, sentinel choice.

Everything here is pure numpy (scipy only for morphology). The CIEDE2000 implementation follows Sharma, Wu and Dalal (2005)
and is cross-checked against scikit-image in the tests.

Conventions: colours are ``#rrggbb`` hex strings (lower-case on output), sRGB 8-bit, D65 / 2 degree Lab.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
_WHITE = np.array([0.95047, 1.0, 1.08883])
_RGB2XYZ = np.array([[0.412453, 0.357580, 0.180423],
                     [0.212671, 0.715160, 0.072169],
                     [0.019334, 0.119193, 0.950227]])
_XYZ2RGB = np.linalg.inv(_RGB2XYZ)
SENTINELS = ("#00ff00", "#ff00ff", "#00ffff", "#0000ff")   # IMG-12 candidates


# ------------------------------------------------------------------ hex and Lab
def normalise_hex(h: str) -> str:
    m = _HEX.match(h.strip())
    if not m:
        raise ValueError(f"not a hex colour: {h!r}")
    return "#" + m.group(1).lower()


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    s = normalise_hex(h)[1:]
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb_to_hex(rgb: Sequence[float]) -> str:
    r, g, b = (int(round(min(255.0, max(0.0, float(c))))) for c in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0..255, any shape ``(..., 3)``) to CIELAB D65."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = lin @ _RGB2XYZ.T / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0, 500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2])], axis=-1)


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    """CIELAB D65 to sRGB 0..255 floats (clipped)."""
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16.0) / 116.0
    fx = fy + lab[..., 1] / 500.0
    fz = fy - lab[..., 2] / 200.0
    f = np.stack([fx, fy, fz], axis=-1)
    xyz = np.where(f ** 3 > 0.008856, f ** 3, (f - 16.0 / 116.0) / 7.787) * _WHITE
    lin = xyz @ _XYZ2RGB.T
    lin = np.clip(lin, 0.0, 1.0)
    c = np.where(lin > 0.0031308, 1.055 * np.power(lin, 1 / 2.4) - 0.055, 12.92 * lin)
    return np.clip(c * 255.0, 0.0, 255.0)


def hex_to_lab(h: str) -> np.ndarray:
    return srgb_to_lab(np.array(hex_to_rgb(h), dtype=np.float64))


def luminance_y(rgb: np.ndarray) -> np.ndarray:
    """Relative luminance (Y of XYZ, 0..1) of sRGB 0..255."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    return lin @ _RGB2XYZ[1]


# ------------------------------------------------------------------ CIEDE2000
def deltaE2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """CIEDE2000 between Lab arrays (broadcast over the leading dimensions)."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar7 = ((C1 + C2) / 2.0) ** 7
    G = 0.5 * (1.0 - np.sqrt(Cbar7 / (Cbar7 + 25.0 ** 7)))
    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0
    dLp = L2 - L1
    dCp = C2p - C1p
    prod = C1p * C2p
    dh = h2p - h1p
    dh = np.where(dh > 180.0, dh - 360.0, np.where(dh < -180.0, dh + 360.0, dh))
    dh = np.where(prod == 0, 0.0, dh)
    dHp = 2.0 * np.sqrt(prod) * np.sin(np.radians(dh / 2.0))
    Lbp = (L1 + L2) / 2.0
    Cbp = (C1p + C2p) / 2.0
    hsum = h1p + h2p
    hbp = np.where(np.abs(h1p - h2p) <= 180.0, hsum / 2.0, np.where(hsum < 360.0, (hsum + 360.0) / 2.0, (hsum - 360.0) / 2.0))
    hbp = np.where(prod == 0, hsum, hbp)
    T = (1.0 - 0.17 * np.cos(np.radians(hbp - 30.0)) + 0.24 * np.cos(np.radians(2.0 * hbp))
         + 0.32 * np.cos(np.radians(3.0 * hbp + 6.0)) - 0.20 * np.cos(np.radians(4.0 * hbp - 63.0)))
    dtheta = 30.0 * np.exp(-(((hbp - 275.0) / 25.0) ** 2))
    Rc = 2.0 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25.0 ** 7))
    Sl = 1.0 + 0.015 * (Lbp - 50.0) ** 2 / np.sqrt(20.0 + (Lbp - 50.0) ** 2)
    Sc = 1.0 + 0.045 * Cbp
    Sh = 1.0 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2.0 * dtheta)) * Rc
    t1 = dLp / Sl
    t2 = dCp / Sc
    t3 = dHp / Sh
    return np.sqrt(t1 * t1 + t2 * t2 + t3 * t3 + Rt * t2 * t3)


def de2000_hex(a: str, b: str) -> float:
    """CIEDE2000 between two hex colours."""
    return float(deltaE2000(hex_to_lab(a), hex_to_lab(b)))


def de2000_rgb(a: Sequence[float], b: Sequence[float]) -> float:
    return float(deltaE2000(srgb_to_lab(np.asarray(a, float)), srgb_to_lab(np.asarray(b, float))))


def nearest_in_palette(lab_points: np.ndarray, palette_lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each Lab point return ``(index of the nearest palette colour, its CIEDE2000 distance)``."""
    pts = np.asarray(lab_points, dtype=np.float64).reshape(-1, 3)
    pal = np.asarray(palette_lab, dtype=np.float64).reshape(-1, 3)
    d = deltaE2000(pts[:, None, :], pal[None, :, :])
    idx = np.argmin(d, axis=1)
    return idx, d[np.arange(len(pts)), idx]


def palette_lab(palette_hex: Iterable[str]) -> np.ndarray:
    return np.array([hex_to_lab(h) for h in palette_hex], dtype=np.float64).reshape(-1, 3)


def snap_hex(h: str, palette_hex: Sequence[str]) -> tuple[str, float]:
    """Nearest palette colour to ``h`` and its distance."""
    idx, d = nearest_in_palette(hex_to_lab(h)[None, :], palette_lab(palette_hex))
    return normalise_hex(palette_hex[int(idx[0])]), float(d[0])


# ------------------------------------------------------------------ pixels and masks
def to_rgba_array(im: Image.Image) -> np.ndarray:
    """``(H, W, 4) uint8`` for any Pillow image (RGB gets alpha 255)."""
    return np.asarray(im.convert("RGBA"), dtype=np.uint8)


def foreground_mask(im: Image.Image, *, alpha_min: int = 128, bg_hex: str | Sequence[str] | None = None,
                    bg_de: float = 6.0) -> np.ndarray:
    """Foreground = alpha >= ``alpha_min``; for opaque images pass ``bg_hex`` (flat background colour(s)) instead."""
    arr = to_rgba_array(im)
    fg = arr[..., 3] >= alpha_min
    if bg_hex is not None:
        bgs = [bg_hex] if isinstance(bg_hex, str) else list(bg_hex)
        lab = srgb_to_lab(arr[..., :3])
        for b in bgs:
            fg &= deltaE2000(lab, hex_to_lab(b)) > bg_de
    return fg


def erode(mask: np.ndarray, px: int = 1) -> np.ndarray:
    """Binary erosion by ``px`` pixels (8-connected structuring element); the image border counts as outside."""
    if px <= 0:
        return mask.copy()
    from scipy import ndimage as ndi

    return ndi.binary_erosion(mask, structure=np.ones((3, 3), bool), iterations=px, border_value=0)


def dilate(mask: np.ndarray, px: int = 1) -> np.ndarray:
    if px <= 0:
        return mask.copy()
    from scipy import ndimage as ndi

    return ndi.binary_dilation(mask, structure=np.ones((3, 3), bool), iterations=px)


def interior_mask(im: Image.Image, erode_px: int = 1, edge_de: float = 6.0) -> np.ndarray:
    """Pixels that may be palette-snapped: opaque and not on an anti-aliased edge.

    With a real alpha channel the interior is ``alpha == 255`` eroded by ``erode_px``. For an opaque image there is no
    alpha to read edges from, so edge pixels are those that differ from a 4-neighbour by more than ``edge_de`` (CIE76).
    """
    arr = to_rgba_array(im)
    a = arr[..., 3]
    if (a < 255).any():
        return erode(a == 255, erode_px)
    lab = srgb_to_lab(arr[..., :3])
    edge = np.zeros(a.shape, bool)
    for axis in (0, 1):
        d = np.sqrt(((np.diff(lab, axis=axis)) ** 2).sum(-1)) > edge_de
        if axis == 0:
            edge[:-1] |= d
            edge[1:] |= d
        else:
            edge[:, :-1] |= d
            edge[:, 1:] |= d
    return ~dilate(edge, max(0, erode_px - 1)) if erode_px > 1 else ~edge


# ------------------------------------------------------------------ k-means in CIELAB
@dataclass(frozen=True)
class ColourCluster:
    lab: tuple[float, float, float]
    hex: str
    share: float          # share of the pixels that were clustered
    count: int


def kmeans_lab(points: np.ndarray, k: int, *, seed: int = 0, iters: int = 40) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic k-means (k-means++ start). Returns ``(centres (k, 3), labels (n,))``."""
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    if n == 0:
        return np.zeros((0, 3)), np.zeros((0,), np.int64)
    k = max(1, min(k, n))
    rng = np.random.RandomState(seed)
    centres = np.empty((k, 3))
    centres[0] = pts[rng.randint(n)]
    d2 = ((pts - centres[0]) ** 2).sum(1)
    for i in range(1, k):
        tot = d2.sum()
        if tot <= 0:
            centres[i:] = centres[0]
            break
        centres[i] = pts[rng.choice(n, p=d2 / tot)]
        d2 = np.minimum(d2, ((pts - centres[i]) ** 2).sum(1))
    labels = np.zeros(n, np.int64)
    for _ in range(iters):
        dist = ((pts[:, None, :] - centres[None, :, :]) ** 2).sum(2)
        new = dist.argmin(1)
        if _ and (new == labels).all():
            break
        labels = new
        for j in range(k):
            sel = pts[labels == j]
            if len(sel):
                centres[j] = sel.mean(0)
    return centres, labels


def extract_palette(im: Image.Image, *, k: int = 6, mask: np.ndarray | None = None, erode_px: int = 1,
                    exclude_hex: Sequence[str] = (), exclude_de: float = 6.0, merge_de: float = 4.0,
                    min_share: float = 0.0, max_samples: int = 30000, seed: int = 0) -> list[ColourCluster]:
    """Dominant colours of the opaque interior (k-means in CIELAB), shading bands merged within ``merge_de``.

    * With ``mask`` the zone is the mask; otherwise it is the foreground (alpha >= 128) eroded by ``erode_px``.
    * ``exclude_hex`` removes background / guide-grey / skin colours (within ``exclude_de``) before clustering.
    * Clusters closer than ``merge_de`` are merged (so shading bands count as one colour).
    """
    arr = to_rgba_array(im)
    zone = mask.astype(bool) if mask is not None else erode(arr[..., 3] >= 128, erode_px)
    if mask is not None and erode_px:
        zone = erode(zone, erode_px) if zone.sum() > 400 else zone
    px = arr[zone][:, :3]
    if len(px) == 0:
        return []
    lab = srgb_to_lab(px)
    if exclude_hex:
        keep = np.ones(len(lab), bool)
        for h in exclude_hex:
            keep &= deltaE2000(lab, hex_to_lab(h)) > exclude_de
        lab = lab[keep]
        if len(lab) == 0:
            return []
    total = len(lab)
    if total > max_samples:
        lab = lab[np.random.RandomState(seed).choice(total, max_samples, replace=False)]
    centres, labels = kmeans_lab(lab, k, seed=seed)
    counts = np.bincount(labels, minlength=len(centres)).astype(np.float64)
    order = np.argsort(-counts)
    merged: list[list] = []   # [lab_sum, count]
    for j in order:
        if counts[j] == 0:
            continue
        for m in merged:
            if float(deltaE2000(centres[j], m[0] / m[1])) < merge_de:
                m[0] = m[0] + centres[j] * counts[j]
                m[1] += counts[j]
                break
        else:
            merged.append([centres[j] * counts[j], counts[j]])
    out = []
    for lab_sum, cnt in sorted(merged, key=lambda m: -m[1]):
        c = lab_sum / cnt
        share = cnt / counts.sum()
        if share < min_share:
            continue
        out.append(ColourCluster(lab=(float(c[0]), float(c[1]), float(c[2])), hex=rgb_to_hex(lab_to_srgb(c)), share=float(share),
                                 count=int(round(share * total))))
    return out


def palette_overlap(a: Sequence[ColourCluster], b: Sequence[ColourCluster], de_match: float = 12.0) -> float:
    """Shared share of two cluster sets: greedy one-to-one matching within ``de_match``, summing ``min(shares)``.

    1.0 means identical colour distribution, 0.0 means no colour in common.
    """
    if not a or not b:
        return 0.0
    la = np.array([c.lab for c in a])
    lb = np.array([c.lab for c in b])
    d = deltaE2000(la[:, None, :], lb[None, :, :])
    sa = [c.share for c in a]
    sb = [c.share for c in b]
    pairs = sorted(((d[i, j], i, j) for i in range(len(a)) for j in range(len(b)) if d[i, j] <= de_match))
    used_a: set[int] = set()
    used_b: set[int] = set()
    total = 0.0
    for _, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        total += min(sa[i], sb[j])
    return float(min(1.0, total))


# ------------------------------------------------------------------ snapping
@dataclass
class SnapStats:
    changed_px: int = 0
    interior_px: int = 0
    max_de_moved: float = 0.0
    mean_de_moved: float = 0.0
    colours_before: int = 0
    colours_after: int = 0
    per_colour_de: dict[str, float] = field(default_factory=dict)   # palette hex -> max shift of the colours snapped to it


def snap_to_palette(im: Image.Image, palette_hex: Sequence[str], *, interior_only: bool = True, erode_px: int = 1,
                    max_de: float | None = None) -> tuple[Image.Image, SnapStats]:
    """Snap interior pixels to their nearest palette colour (CIEDE2000). Anti-aliased edges and alpha are never touched.

    With ``max_de`` a colour whose nearest palette entry is farther than that is left as it is.
    The result keeps the input's mode (RGBA in, RGBA out).
    """
    if not palette_hex:
        raise ValueError("empty palette")
    arr = to_rgba_array(im).copy()
    sel = interior_mask(im, erode_px) if interior_only else (arr[..., 3] > 0)
    sel &= arr[..., 3] > 0
    stats = SnapStats(interior_px=int(sel.sum()))
    if not sel.any():
        return im.copy(), stats
    rgb = arr[..., :3][sel]
    packed = (rgb[:, 0].astype(np.uint32) << 16) | (rgb[:, 1].astype(np.uint32) << 8) | rgb[:, 2].astype(np.uint32)
    uniq, inv = np.unique(packed, return_inverse=True)
    ur = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=1).astype(np.float64)
    pal = [normalise_hex(h) for h in palette_hex]
    plab = palette_lab(pal)
    idx, d = nearest_in_palette(srgb_to_lab(ur), plab)
    prgb = np.array([hex_to_rgb(h) for h in pal], dtype=np.uint8)
    new_u = prgb[idx].astype(np.uint8).copy()
    if max_de is not None:
        far = d > max_de
        new_u[far] = ur[far].astype(np.uint8)
        d = np.where(far, 0.0, d)
    new_rgb = new_u[inv.reshape(-1)]
    moved = np.any(new_rgb != rgb, axis=1)
    arr_rgb = arr[..., :3]
    arr_rgb[sel] = new_rgb
    arr[..., :3] = arr_rgb
    per_px_d = d[inv.reshape(-1)]
    stats.changed_px = int(moved.sum())
    stats.max_de_moved = float(per_px_d.max()) if len(per_px_d) else 0.0
    stats.mean_de_moved = float(per_px_d[moved].mean()) if moved.any() else 0.0
    stats.colours_before = int(len(uniq))
    packed_new = (new_u[:, 0].astype(np.uint32) << 16) | (new_u[:, 1].astype(np.uint32) << 8) | new_u[:, 2].astype(np.uint32)
    stats.colours_after = int(len(np.unique(packed_new)))
    for i in np.unique(idx):
        stats.per_colour_de[pal[int(i)]] = float(d[idx == i].max())
    out = Image.fromarray(arr, "RGBA")
    return (out if im.mode == "RGBA" else out.convert(im.mode if im.mode in ("RGB", "RGBA") else "RGBA")), stats


# ------------------------------------------------------------------ palette-vs-spec evaluation (A_PALETTE facts)
@dataclass
class PaletteFacts:
    clusters: list[ColourCluster]
    nearest: list[tuple[str, float]]      # per cluster: (palette hex, ΔE2000)
    worst_de: float                        # worst ΔE among clusters that count (>= min_share)
    worst_large_de: float                  # worst ΔE among large clusters (>= large_share of the foreground)
    counted: int                           # clusters that count (>= min_share)
    allowed: int


def palette_facts(im: Image.Image, allowed_hex: Sequence[str], *, k: int | None = None, mask: np.ndarray | None = None,
                  exclude_hex: Sequence[str] = ()) -> PaletteFacts:
    """k-means the interior in CIELAB and measure every cluster against the allowed palette (IMG-05)."""
    allowed = [normalise_hex(h) for h in allowed_hex]
    kk = k or min(10, max(3, len(allowed) + 2))
    clusters = extract_palette(im, k=kk, mask=mask, exclude_hex=exclude_hex, merge_de=2.0)
    plab = palette_lab(allowed)
    min_share = float(TH.get("img.palette_min_cluster"))
    large_share = float(TH.get("img.palette_large_share"))
    nearest: list[tuple[str, float]] = []
    worst = 0.0
    worst_large = 0.0
    counted = 0
    for c in clusters:
        idx, d = nearest_in_palette(np.array(c.lab)[None, :], plab)
        nearest.append((allowed[int(idx[0])], float(d[0])))
        if c.share >= min_share:
            counted += 1
            worst = max(worst, float(d[0]))
        if c.share >= large_share:
            worst_large = max(worst_large, float(d[0]))
    return PaletteFacts(clusters, nearest, worst, worst_large, counted, len(allowed))


# ------------------------------------------------------------------ sentinel colour (IMG-12)
def choose_sentinel(palette_hex: Sequence[str], *, min_de: float | None = None) -> str | None:
    """The farthest of #00FF00, #FF00FF, #00FFFF, #0000FF from every palette colour, or ``None``.

    ``None`` means even the best candidate is closer than ``img.sentinel_de_min`` (40) to some palette colour: use native
    alpha or matting instead of a chroma background.
    """
    lim = float(TH.get("img.sentinel_de_min")) if min_de is None else min_de
    pal = palette_lab(palette_hex) if len(palette_hex) else np.zeros((0, 3))
    best: str | None = None
    best_d = -1.0
    for s in SENTINELS:
        d = float(deltaE2000(hex_to_lab(s)[None, :], pal).min()) if len(pal) else 1e9
        if d > best_d:
            best, best_d = s, d
    return best if best_d >= lim else None


def sentinel_min_distance(sentinel: str, palette_hex: Sequence[str]) -> float:
    pal = palette_lab(palette_hex)
    return float(deltaE2000(hex_to_lab(sentinel)[None, :], pal).min()) if len(pal) else 1e9


# ------------------------------------------------------------------ concept palette lock (C3)
@dataclass(frozen=True)
class SnapDecision:
    zone: str
    planned: str
    extracted: str
    de: float
    action: str          # "snap" (close enough to the plan) or "confirm" (the user decides)


def zone_palettes(im: Image.Image, zones: dict[str, np.ndarray], *, k: int = 3, exclude_hex: Sequence[str] = ()) -> dict[str, list[ColourCluster]]:
    """Dominant colours per guide zone (garment and hair zones), skin and background excluded by the caller's masks."""
    return {name: extract_palette(im, k=k, mask=m, exclude_hex=exclude_hex, merge_de=6.0) for name, m in zones.items()}


def snap_or_confirm(extracted: dict[str, str], planned: dict[str, str], *, snap_de: float | None = None) -> list[SnapDecision]:
    """C3 palette lock: an extracted zone colour within ``con.palette_snap_de`` of the planned colour is snapped to it
    automatically; a larger difference needs the user's confirmation (CHK-G1-08)."""
    lim = float(TH.get("con.palette_snap_de")) if snap_de is None else snap_de
    out = []
    for zone, ex in extracted.items():
        pl = planned.get(zone)
        if pl is None:
            out.append(SnapDecision(zone, "", normalise_hex(ex), float("inf"), "confirm"))
            continue
        d = de2000_hex(ex, pl)
        out.append(SnapDecision(zone, normalise_hex(pl), normalise_hex(ex), d, "snap" if d <= lim else "confirm"))
    return out


# ------------------------------------------------------------------ palette sheet (bible §8: palette sheets)
def palette_sheet(colours: Sequence[str], *, cell: int = 64, gap: int = 8, columns: int | None = None, bg: str = "#f2f2f2") -> Image.Image:
    """A code-drawn swatch sheet (flat squares, no text) for judges and the UI."""
    n = len(colours)
    cols = columns or min(n, 8) or 1
    rows = max(1, -(-n // cols))
    w = cols * cell + (cols + 1) * gap
    h = rows * cell + (rows + 1) * gap
    from PIL import ImageDraw

    sheet = Image.new("RGB", (w, h), hex_to_rgb(bg))
    draw = ImageDraw.Draw(sheet)
    for i, hx in enumerate(colours):
        r, c = divmod(i, cols)
        x0 = gap + c * (cell + gap)
        y0 = gap + r * (cell + gap)
        draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=hex_to_rgb(hx))
    return sheet
