"""Shared helpers for the clothing tests (unique module name: the test dirs have no __init__.py)."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SHIRT_PNG = FIXTURES / "Template-Shirts-R15.png"
PANTS_PNG = FIXTURES / "Template-Pants-R15.png"

PALETTE_ROLES = {"base": (86, 150, 200), "second": (250, 226, 120), "trim": (40, 52, 96), "inner": (240, 240, 240),
                 "shoe_base": (230, 230, 230), "shoe_sole": (40, 40, 40), "shoe_accent": (230, 80, 80),
                 "legwear": (240, 240, 240), "bracelet": (230, 80, 80), "glove": (250, 226, 120)}
FABRIC_FOR = {"tee": "jersey_plain", "tee_long": "jersey_plain", "raglan": "jersey_plain", "hoodie": "fleece_soft",
              "jacket_zip": "nylon_smooth", "jacket_open": "nylon_smooth", "vest": "knit_rib", "crop_top": "jersey_plain",
              "skirt_pleated": "twill_fine", "skirt_a_line": "wool_felt", "jeans_straight": "denim_classic",
              "jeans_wide": "denim_classic", "shorts": "canvas_plain", "cargos": "canvas_plain", "cargo_joggers": "corduroy_fine"}


def official(kind: str) -> np.ndarray:
    return np.array(Image.open(SHIRT_PNG if kind == "shirt" else PANTS_PNG).convert("RGBA"), dtype=np.uint8)


def request_for(rid: str, *, attrs=None, params=None, prints=None, shoe="sneaker_low", legwear="bare", extras=(), colours=None,
                palette=None, fabric=None, folds=None):
    from duoskin.imaging import compositor as C
    from duoskin.imaging import fabric as F
    from duoskin.imaging import recipes as R

    rec = R.load_recipe(rid)
    return C.GarmentRequest(kind=rec.template, recipe=rec, colours=dict(colours or PALETTE_ROLES), attrs=dict(attrs or {}),
                            params=dict(params or {}), fabric=fabric or F.procedural_fabric(FABRIC_FOR.get(rid, "jersey_plain")),
                            folds=folds, prints=list(prints or []), shoe_style=(shoe if rec.template == "pants" else None),
                            legwear=legwear, arm_extras=list(extras), palette=list(palette or []))


@lru_cache(maxsize=64)
def composed(rid: str, key: str = "", **_kw):
    """Compose ``rid`` once per (rid, key); ``key`` names a variant built by the caller through ``variant``."""
    from duoskin.imaging import compositor as C

    return C.compose(request_for(rid), run_checks=True, previews=True)


def compose(rid: str, **kw):
    from duoskin.imaging import compositor as C

    run_checks = kw.pop("run_checks", True)
    with_layers = kw.pop("with_layers", False)
    previews = kw.pop("previews", False)
    return C.compose(request_for(rid, **kw), run_checks=run_checks, with_layers=with_layers, previews=previews)


def rgba(png: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png)).convert("RGBA"), dtype=np.uint8)


def glyph_rgba(size: int = 24) -> np.ndarray:
    """An asymmetric RGBA 'F'-like glyph (opaque strokes on transparent), never equal to its mirror image."""
    g = np.zeros((size, size, 4), dtype=np.uint8)
    s = size // 6
    g[2 * s // 2:size - s, s:2 * s] = (220, 40, 40, 255)          # vertical stem on the left
    g[s:2 * s, s:size - s] = (220, 40, 40, 255)                   # top bar
    g[3 * s:4 * s, s:size - 2 * s] = (40, 40, 220, 255)           # middle bar (shorter)
    return g
