"""Badge artwork for sticker slabs and hair-clip slabs (PROMPT_BIBLE §11.6 "Code afterwards", APP_SPEC §10.4 "Badge border").

The model draws flat artwork (I6). **Code** does everything that must be exact: fill the alpha holes, binarise at 128, remove islands,
dilate by N px to make the border (a flat colour, never drawn by the model), check the silhouette is one compact solid shape, and lay
out the opaque 24-bit texture of the slab. The extrusion itself (alpha contour, triangle budget, thickness, bevel) is ``mesh/slab.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

from duoskin.imaging import checks as C
from duoskin.imaging import palette as P


@dataclass
class Badge:
    image: Image.Image            # RGBA: art + border, binary alpha
    silhouette: np.ndarray        # bool mask of the whole badge (art + border)
    art_mask: np.ndarray          # bool mask of the art only
    border_px: int
    border_hex: str
    solidity: float
    holes_filled: int
    islands_removed: int


def build_badge(art: Image.Image, *, border_px: int, border_hex: str, min_island_frac: float | None = None) -> Badge:
    """Fill alpha holes -> binarise at 128 -> remove islands -> dilate ``border_px`` (round) -> fill the border with ``border_hex``.

    The border is painted where the dilated silhouette extends past the art; the art pixels are untouched.
    """
    from scipy import ndimage as ndi

    clean, rep = C.cleanup_alpha_asset(art, None, binarize=True, fill_holes=True, min_island_frac=min_island_frac)
    arr = np.array(clean, dtype=np.uint8)
    art_mask = arr[..., 3] >= 128
    if border_px > 0 and art_mask.any():
        dist = ndi.distance_transform_edt(~art_mask)
        sil = dist <= border_px
    else:
        sil = art_mask.copy()
    ring = sil & ~art_mask
    arr[ring, :3] = P.hex_to_rgb(border_hex)
    arr[ring, 3] = 255
    arr[~sil, 3] = 0
    out = Image.fromarray(arr, "RGBA")
    return Badge(out, sil, art_mask, border_px, P.normalise_hex(border_hex), C.solidity(sil), rep.holes_filled, rep.islands_removed)


def outline_polygon(mask: np.ndarray, epsilon_px: float = 1.5) -> np.ndarray:
    """Outer contour of the largest piece as an ``(N, 2)`` polygon in pixel coordinates, simplified with Douglas-Peucker."""
    import cv2

    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return np.zeros((0, 2), np.float32)
    c = max(cnts, key=cv2.contourArea)
    return cv2.approxPolyDP(c, epsilon_px, True).reshape(-1, 2).astype(np.float32)


def back_art_unmirrored(front: Image.Image) -> Image.Image:
    """The art for the back island when both sides carry the same design.

    A plain planar UV on the back face shows the front texture mirrored when viewed from behind (ACC-17). Flipping the texture
    horizontally in the back island makes the design read correctly from behind.
    """
    return ImageOps.mirror(front.convert("RGBA"))


def compose_slab_texture(badge: Badge, size: int = 1024, *, back: str = "plain", back_hex: str | None = None) -> Image.Image:
    """Opaque RGB atlas ``size x size`` (24-bit, alpha 255): the front island on the left half, the back island on the right half.

    * front: the badge (art + border) fitted into the left square on the border colour;
    * back: ``"plain"`` = the border colour only, ``"same_art"`` = the art flipped so it reads correctly from behind.
    """
    half = size // 2
    bg = P.hex_to_rgb(back_hex or badge.border_hex)
    atlas = Image.new("RGB", (size, size), bg)
    bb = C.bbox_of(badge.silhouette)
    if bb is None:
        return atlas
    crop = badge.image.crop(bb)
    scale = min(half * 0.94 / crop.width, size * 0.94 / crop.height)
    nw, nh = max(1, round(crop.width * scale)), max(1, round(crop.height * scale))
    fit = crop.convert("RGBa").resize((nw, nh), Image.Resampling.BOX if scale < 1 else Image.Resampling.LANCZOS).convert("RGBA")
    ox, oy = (half - nw) // 2, (size - nh) // 2
    layer = Image.new("RGBA", (half, size), bg + (255,))
    layer.alpha_composite(fit, (ox, oy))
    atlas.paste(layer.convert("RGB"), (0, 0))
    if back == "same_art":
        flipped = back_art_unmirrored(fit)
        layer_b = Image.new("RGBA", (half, size), bg + (255,))
        layer_b.alpha_composite(flipped, (ox, oy))
        atlas.paste(layer_b.convert("RGB"), (half, 0))
    elif back != "plain":
        raise ValueError(f"unknown back mode {back!r}")
    return atlas
