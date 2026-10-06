"""Small colour helpers for the mesh gate: sRGB <-> Lab and CIEDE2000 (numpy, vectorised), dominant colours, pHash.

The shared 2D colour module (``imaging/palette.py``) belongs to another track; this file keeps the mesh worker free of
that dependency (the worker must run with only numpy, scipy, Pillow and trimesh).
"""
from __future__ import annotations

import numpy as np

_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]])
_WHITE = np.array([0.95047, 1.0, 1.08883])


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """``rgb`` uint8 or float 0-255, shape (..., 3) -> Lab (D65)."""
    c = np.asarray(rgb, np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M.T / _WHITE
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, np.float64)
    fy = (lab[..., 0] + 16) / 116
    fx = fy + lab[..., 1] / 500
    fz = fy - lab[..., 2] / 200

    def inv(t):
        return np.where(t ** 3 > 216 / 24389, t ** 3, (116 * t - 16) / (24389 / 27))

    xyz = np.stack([inv(fx), inv(fy), inv(fz)], axis=-1) * _WHITE
    lin = xyz @ np.linalg.inv(_M).T
    lin = np.clip(lin, 0, 1)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(np.round(c * 255.0), 0, 255).astype(np.uint8)


def delta_e2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """CIEDE2000 between Lab arrays (broadcastable, last axis 3)."""
    lab1 = np.asarray(lab1, np.float64)
    lab2 = np.asarray(lab2, np.float64)
    l1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    l2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    cm = (c1 + c2) / 2
    g = 0.5 * (1 - np.sqrt(cm ** 7 / (cm ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dl = l2 - l1
    dc = c2p - c1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, dh)
    dh = np.where(dh < -180, dh + 360, dh)
    dh = np.where((c1p * c2p) == 0, 0, dh)
    dhh = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh) / 2)
    lpm = (l1 + l2) / 2
    cpm = (c1p + c2p) / 2
    hsum = h1p + h2p
    hpm = np.where(np.abs(h1p - h2p) > 180, (hsum + 360) / 2, hsum / 2)
    hpm = np.where((c1p * c2p) == 0, hsum, hpm)
    t = (1 - 0.17 * np.cos(np.radians(hpm - 30)) + 0.24 * np.cos(np.radians(2 * hpm))
         + 0.32 * np.cos(np.radians(3 * hpm + 6)) - 0.20 * np.cos(np.radians(4 * hpm - 63)))
    dtheta = 30 * np.exp(-(((hpm - 275) / 25) ** 2))
    rc = 2 * np.sqrt(cpm ** 7 / (cpm ** 7 + 25.0 ** 7))
    sl = 1 + 0.015 * (lpm - 50) ** 2 / np.sqrt(20 + (lpm - 50) ** 2)
    sc = 1 + 0.045 * cpm
    sh = 1 + 0.015 * cpm * t
    rt = -np.sin(np.radians(2 * dtheta)) * rc
    return np.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dhh / sh) ** 2 + rt * (dc / sc) * (dhh / sh))


def de2000_rgb(rgb1, rgb2) -> np.ndarray:
    return delta_e2000(srgb_to_lab(np.asarray(rgb1)), srgb_to_lab(np.asarray(rgb2)))


def dominant_colours(rgb: np.ndarray, mask: np.ndarray | None = None, k: int = 4, bits: int = 4) -> list[tuple[np.ndarray, float]]:
    """Top-``k`` colours of an image by a coarse histogram. Returns ``[(rgb mean uint8 (3,), share)]`` sorted by share."""
    px = np.asarray(rgb)[..., :3].reshape(-1, 3)
    if mask is not None:
        px = px[np.asarray(mask, bool).reshape(-1)]
    if len(px) == 0:
        return []
    shift = 8 - bits
    key = ((px[:, 0] >> shift).astype(np.int64) << (2 * bits)) | ((px[:, 1] >> shift).astype(np.int64) << bits) | (px[:, 2] >> shift).astype(np.int64)
    _uniq, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    order = np.argsort(-counts)[:k]
    out = []
    for o in order:
        sel = inv == o
        out.append((px[sel].mean(axis=0), float(counts[o]) / len(px)))
    return out


def phash(img: np.ndarray, size: int = 32, keep: int = 8) -> int:
    """64-bit perceptual hash of a greyscale-able image array (DCT of a 32x32 area-resampled grey image)."""
    from PIL import Image
    from scipy.fft import dctn

    arr = np.asarray(img)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).resize((size, size), Image.Resampling.BOX)
    d = dctn(np.asarray(im, np.float64), norm="ortho")[:keep, :keep]
    med = np.median(d.flatten()[1:])
    bits = (d.flatten() > med).astype(np.uint64)
    out = 0
    for i, b in enumerate(bits):
        out |= int(b) << i
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
