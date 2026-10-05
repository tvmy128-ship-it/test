"""Palette-aware unmixing of a sentinel background (APP_SPEC §10.4.1, bible §2.5 fallback ladder rung 1, IMG-12).

An opaque image whose background is a flat sentinel colour ``S`` (the farthest of #00FF00, #FF00FF, #00FFFF, #0000FF from every palette
colour, ``palette.choose_sentinel``) becomes a clean cut-out without a pixel chroma key: for each edge pixel ``P`` the palette colour
``F`` and alpha ``a`` are chosen that minimise ``|P - (a*F + (1-a)*S)|``. Pixels that are the background become alpha 0, pixels that are
a palette colour stay opaque, and only the anti-aliased pixels in between are unmixed.

For SVG the exact two-pass matte in ``imaging/svg.py`` is used instead; this module is for raster output (GPT / Gemini on a sentinel).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result
from duoskin.imaging import palette as P


@dataclass
class UnmixResult:
    image: Image.Image            # RGBA
    edge_px: int                  # pixels that were unmixed
    unresolved_px: int            # edge pixels that no (F, S) mixture explains; they were snapped to the nearest palette colour
    max_residual: float           # worst RGB residual among the unmixed edge pixels (0..441)


def unmix_sentinel(im: Image.Image, palette_hex: Sequence[str], sentinel_hex: str, *, bg_de: float = 5.0, solid_de: float = 5.0,
                   max_residual: float = 20.0) -> UnmixResult:
    """Cut a sentinel background out of an opaque image. Raises ``ValueError`` if the sentinel collides with the palette (IMG-12)."""
    pal = [P.normalise_hex(h) for h in palette_hex]
    s_hex = P.normalise_hex(sentinel_hex)
    min_de = P.sentinel_min_distance(s_hex, pal)
    if min_de < float(TH.get("img.sentinel_de_min")):
        raise ValueError(f"sentinel {s_hex} is only dE {min_de:.1f} from the palette (needs >= {TH.get('img.sentinel_de_min')})")
    arr = np.asarray(im.convert("RGB"), dtype=np.float64)
    h, w, _ = arr.shape
    flat = arr.reshape(-1, 3)
    lab = P.srgb_to_lab(flat)
    d_bg = P.deltaE2000(lab, P.hex_to_lab(s_hex))
    plab = P.palette_lab(pal)
    idx, d_pal = P.nearest_in_palette(lab, plab)
    prgb = np.array([P.hex_to_rgb(x) for x in pal], dtype=np.float64)
    bg = d_bg <= bg_de
    solid = ~bg & (d_pal <= solid_de)
    edge = ~bg & ~solid
    out_rgb = flat.copy()
    out_a = np.full(len(flat), 255.0)
    out_a[bg] = 0.0
    out_rgb[bg] = 0.0
    resid_max = 0.0
    unresolved = 0
    if edge.any():
        s = np.array(P.hex_to_rgb(s_hex), dtype=np.float64)
        p = flat[edge]                                              # (n, 3)
        dirs = prgb - s                                             # (k, 3)
        denom = np.maximum((dirs ** 2).sum(1), 1e-9)                # (k,)
        a = np.clip(((p - s) @ dirs.T) / denom, 0.0, 1.0)           # (n, k)
        recon = a[:, :, None] * prgb[None, :, :] + (1.0 - a[:, :, None]) * s
        resid = np.linalg.norm(p[:, None, :] - recon, axis=2)       # (n, k)
        best = resid.argmin(1)
        r = resid[np.arange(len(p)), best]
        ea = a[np.arange(len(p)), best]
        good = r <= max_residual
        unresolved = int((~good).sum())
        resid_max = float(r[good].max()) if good.any() else 0.0
        col = prgb[best]
        a_final = np.where(good, ea * 255.0, 255.0)
        col_final = np.where(good[:, None], col, prgb[idx[edge]])
        out_rgb[edge] = col_final
        out_a[edge] = a_final
    res = np.dstack([out_rgb.reshape(h, w, 3), out_a.reshape(h, w, 1)]).round().clip(0, 255).astype(np.uint8)
    return UnmixResult(Image.fromarray(res, "RGBA"), int(edge.sum()), unresolved, resid_max)


def check_sentinel_choice(palette_hex: Sequence[str], sentinel_hex: str, *, subject_sha: str = "") -> CheckResult:
    """A_SENTINEL_DE (IMG-12, ASSERT): the chosen sentinel is at least dE 40 from every palette colour."""
    d = P.sentinel_min_distance(P.normalise_hex(sentinel_hex), palette_hex)
    return build_result("A_SENTINEL_DE", passed=d >= float(TH.get("img.sentinel_de_min")), subject_sha=subject_sha, metric="sentinel_min_de2000",
                        value=d, threshold=TH.describe("img.sentinel_de_min", ">="), evidence=f"nearest palette colour dE {d:.1f}",
                        fix_hint="change_technique")
