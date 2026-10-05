"""Silhouettes of meshes and of the approved 2D views, and the scale-free IoU used by orientation and judging
(FM MESH-12, ACC-01, ACC-07, ACC-08).

A mesh silhouette is rasterised with the numpy z-buffer in the view named like the approved image: ``front``, ``left`` (the
subject's left side: the object's front points to the image's LEFT edge), ``back``, ``right``. Both masks are cropped to
their bounding box and fitted into a common square, so the IoU does not depend on scale or position.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from duoskin.render import raster

VIEW_NAMES = ("front", "left", "back", "right")
NORM = 96


def canonical_view_name(key: str) -> str | None:
    """``"02_LEFT_subject-left.png"`` -> ``"left"``; None when the key names no view."""
    k = key.lower()
    for name in VIEW_NAMES:
        if name in k:
            return name
    return None


def load_view_image(src: Any) -> Image.Image:
    if isinstance(src, Image.Image):
        return src
    if isinstance(src, np.ndarray):
        return Image.fromarray(src)
    with Image.open(Path(src)) as im:
        im.load()
        return im.copy()


def mask_from_image(img: Image.Image, *, bg_de: float = 14.0) -> np.ndarray:
    """Foreground mask of an approved view: the alpha channel when it carries transparency, else everything that differs
    from the border colour (views are flattened on #FFFFFF or #D9D9D9)."""
    rgba = np.asarray(img.convert("RGBA"))
    alpha = rgba[..., 3]
    if alpha.min() < 250 and (alpha < 250).mean() > 0.01:
        return alpha > 127
    rgb = rgba[..., :3].astype(np.float64)
    ring = np.concatenate([rgb[:3].reshape(-1, 3), rgb[-3:].reshape(-1, 3), rgb[:, :3].reshape(-1, 3), rgb[:, -3:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    diff = np.abs(rgb - bg).max(axis=2)
    mask = diff > bg_de
    return ndimage.binary_fill_holes(mask) if mask.any() else mask


def normalise_mask(mask: np.ndarray, size: int = NORM, pad: float = 0.04) -> np.ndarray:
    """Crop to the bbox and fit into a ``size`` square (aspect kept, centred). Empty masks stay empty."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return np.zeros((size, size), bool)
    crop = mask[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
    h, w = crop.shape
    inner = max(2, int(round(size * (1 - 2 * pad))))
    s = inner / max(h, w)
    nh, nw = max(1, int(round(h * s))), max(1, int(round(w * s)))
    im = Image.fromarray((crop * 255).astype(np.uint8)).resize((nw, nh), Image.Resampling.BILINEAR)
    out = np.zeros((size, size), bool)
    y0, x0 = (size - nh) // 2, (size - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = np.asarray(im) > 127
    return out


def iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter / union) if union else 0.0


def mesh_silhouette(vertices: np.ndarray, faces: np.ndarray, view: str, size: int = 128) -> np.ndarray:
    """Boolean silhouette of a mesh in a named orthographic view, framed on the mesh."""
    if len(faces) == 0:
        return np.zeros((size, size), bool)
    used = vertices[np.unique(faces)]
    cam = raster.fit_camera(view, used, size, size, margin=0.03)
    rm = raster.RenderMesh("m", vertices, faces, two_sided=True)
    return raster.silhouette(rm, cam, size, size)


def silhouette_iou(vertices: np.ndarray, faces: np.ndarray, view: str, approved_mask: np.ndarray, *, size: int = 128, norm: int = NORM) -> float:
    """Scale-free IoU of the mesh silhouette (rasterised at ``size`` px) and an approved mask (both fitted into ``norm`` px)."""
    sil = normalise_mask(mesh_silhouette(vertices, faces, view, size), norm)
    return iou(sil, normalise_mask(approved_mask, norm))


def silhouette_iou_normed(vertices: np.ndarray, faces: np.ndarray, view: str, approved_norm: np.ndarray, *, size: int = 128) -> float:
    """Like ``silhouette_iou`` with an approved mask that is already ``normalise_mask``-ed (saves resizing it again)."""
    sil = normalise_mask(mesh_silhouette(vertices, faces, view, size), approved_norm.shape[0])
    return iou(sil, approved_norm)


def thin_mask(mask: np.ndarray, radius_frac: float = 0.025) -> np.ndarray:
    """Thin structures of a silhouette: pixels removed by a morphological opening with a disc of ``radius_frac`` of the
    longer side (rings, straps, ears, spikes)."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return np.zeros_like(mask)
    side = max(ys.max() - ys.min() + 1, xs.max() - xs.min() + 1)
    r = max(1, int(round(side * radius_frac)))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    disc = (xx ** 2 + yy ** 2) <= r * r
    opened = ndimage.binary_opening(mask, structure=disc)
    return mask & ~opened


def approved_masks(approved_views: dict[str, Any]) -> dict[str, np.ndarray]:
    """``{view name: foreground mask}`` for every approved view image given (keys may be file-like names)."""
    out: dict[str, np.ndarray] = {}
    for key, src in approved_views.items():
        name = canonical_view_name(str(key))
        if name is None or name in out:
            continue
        out[name] = mask_from_image(load_view_image(src))
    return out
