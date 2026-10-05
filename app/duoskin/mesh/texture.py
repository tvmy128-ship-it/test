"""Texture handling for rigid accessories (MESH-04, MESH-05, APP_SPEC 10.9 step 7).

The Roblox texture rules we enforce (creator-docs avatar/rigid-accessories/specifications.md: Marketplace textures cannot exceed
2048x2048; marketplace/validation-system.md: a colour map pixel with alpha below 255 fails): PNG, 24-bit RGB (alpha 255 everywhere),
FAIL above 2048 px. Our own choices, not Roblox rules: ship at most 1024 px (WARN above), not a single flat colour, glTF material
OPAQUE. Colours are dilated into the UV gutters before any resize so that no texel at an island edge is transparent or black.
"""
from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from duoskin.render.raster import rasterise


def as_pil(tex: Any) -> Image.Image:
    """Accept a PIL image, an ndarray or PNG/JPEG bytes."""
    if isinstance(tex, Image.Image):
        return tex
    if isinstance(tex, (bytes, bytearray)):
        im = Image.open(io.BytesIO(bytes(tex)))
        im.load()
        return im
    return Image.fromarray(np.asarray(tex))


def to_rgba_array(img: Image.Image) -> np.ndarray:
    """8-bit sRGB RGBA array from any PIL mode (palette, 16 bit, LA, ...)."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I"):
        arr = np.asarray(img, np.float64)
        arr = arr / (257.0 if arr.max() > 255 else 1.0)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return np.asarray(img.convert("RGBA"), np.uint8)


def uv_coverage(uv: np.ndarray, faces: np.ndarray, width: int, height: int, grow: int = 1) -> np.ndarray:
    """Texels covered by the UV triangles (glTF convention, v down), grown by ``grow`` pixels."""
    if len(faces) == 0 or uv is None:
        return np.zeros((height, width), bool)
    xy = np.stack([uv[:, 0] * width, uv[:, 1] * height], axis=1)[faces]
    z = np.zeros(faces.shape, np.float64)
    _, tbuf = rasterise(xy, z, width, height)
    cov = tbuf >= 0
    if grow > 0:
        cov = ndimage.binary_dilation(cov, structure=np.ones((3, 3), bool), iterations=grow)
    return cov


def dilate_into_gutters(rgb: np.ndarray, covered: np.ndarray) -> np.ndarray:
    """Fill every texel outside ``covered`` with the colour of the nearest covered texel."""
    if covered.all() or not covered.any():
        return rgb
    _, (iy, ix) = ndimage.distance_transform_edt(~covered, return_indices=True)
    return rgb[iy, ix]


def channel_std(img: Image.Image) -> float:
    """Largest per-channel standard deviation (0-255 scale) of the RGB channels."""
    arr = to_rgba_array(img)[..., :3].reshape(-1, 3).astype(np.float64)
    return float(arr.std(axis=0).max())


def min_alpha(img: Image.Image) -> int:
    """Smallest alpha value (255 for any mode without alpha)."""
    if img.mode in ("RGB", "L", "P") and "transparency" not in img.info:
        return 255
    return int(to_rgba_array(img)[..., 3].min())


def is_flat(img: Image.Image, std_min: float = 2.0) -> bool:
    return channel_std(img) <= std_min


def prepare_texture(img: Image.Image, uv: np.ndarray | None, faces: np.ndarray | None, target_px: int,
                    *, dilate: bool = True) -> tuple[Image.Image, dict[str, Any]]:
    """Opaque RGB texture of at most ``target_px`` with the gutters filled.

    Order (APP_SPEC 10.9 step 7): dilate edge colours into the UV gutters at the source resolution, drop alpha, then
    resize. Returns the image and a report ``{src_size, size, src_mode, src_min_alpha, dilated, resized}``.
    """
    src_mode, src_size = img.mode, img.size
    src_min_alpha = min_alpha(img)
    rgba = to_rgba_array(img)
    rgb = rgba[..., :3].copy()
    h, w = rgb.shape[:2]
    dilated = False
    if dilate and uv is not None and faces is not None and len(faces):
        cov = uv_coverage(uv, faces, w, h, grow=1)
        # semi-transparent texels inside the UV islands keep their colour; only gutters are rebuilt
        if cov.any() and not cov.all():
            rgb = dilate_into_gutters(rgb, cov)
            dilated = True
    out = Image.fromarray(rgb, "RGB")
    resized = False
    if max(out.size) > target_px or out.size[0] != out.size[1]:
        side = min(target_px, max(out.size))
        out = out.resize((side, side), Image.Resampling.LANCZOS)
        resized = True
    return out, {"src_size": list(src_size), "size": list(out.size), "src_mode": src_mode,
                 "src_min_alpha": src_min_alpha, "dilated": dilated, "resized": resized}


def png_bytes(img: Image.Image) -> bytes:
    """24-bit RGB PNG bytes without ancillary colour chunks (no ICC, no gamma)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def luminance_ramp(img: Image.Image) -> float:
    """Low-frequency luminance ramp across the texture (ACC-16): 95th minus 5th percentile of a heavily blurred luminance."""
    a = np.asarray(img.convert("L"), np.float64) / 255.0
    small = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).resize((16, 16), Image.Resampling.BOX), np.float64) / 255.0
    return float(np.percentile(small, 95) - np.percentile(small, 5))


def shading_band_count(img: Image.Image, bins: int = 16, min_share: float = 0.02) -> int:
    """Number of luminance bins holding at least ``min_share`` of the opaque texels (a cheap cel-shading band count)."""
    lum = np.asarray(img.convert("L")).reshape(-1)
    hist = np.bincount((lum.astype(np.int64) * bins) // 256, minlength=bins) / max(len(lum), 1)
    return int((hist >= min_share).sum())
