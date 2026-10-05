"""Painted kit pieces: shoes, legwear, bracelets, wristbands and gloves (APP_SPEC §10.5 step 6, bible §6.2, CLO-07).

The pieces are **data** in ``builtin_kits/shoes.json``, ``legwear.json`` and ``bracelets.json`` and use the same layer schema
as garment recipes (``imaging/recipes.py``), so they run through the same compositor stage ("kit", after prints):

* shoes (Pants): rows 446-482 at most (low shoes 460-482) on the four faces of each leg plus the D sole; the top painted row
  must lie in rows 446-465 (assert rule ``shoe_top``);
* legwear (Pants): socks and tights painted UNDER the pants (only where nothing covers yet);
* bracelets, wristbands, gloves (Shirt arms): every piece lies fully inside ONE limb band (355-416, 421-465 or 469-482; assert
  rule ``band``), so the R15 elbow (418.5) and wrist (467) splits never cut it. Names are the character's own side (CLO-06).

Colour roles used: ``shoe_base``, ``shoe_sole``, ``shoe_accent``, ``legwear``, ``bracelet``, ``glove``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from duoskin.imaging.recipes import Layer, RecipeError
from duoskin.roblox import template as T

KIT_DIR = Path(__file__).resolve().parent.parent / "builtin_kits"
SHOE_STYLES = ("sneaker_low", "sneaker_high", "boot", "loafer", "sandal_strap", "mary_jane")
LEGWEAR_KINDS = ("socks_ankle", "socks_crew", "socks_knee", "tights")
ARM_PIECES = ("bracelet_char_right", "bracelet_char_left", "wristband_char_right", "wristband_char_left", "gloves")


@dataclass(frozen=True)
class KitPiece:
    kit: str                    # shoes | legwear | arm
    piece_id: str
    title: str
    layers: tuple[Layer, ...]
    top_row: int | None = None


@lru_cache(maxsize=8)
def _read(name: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    for d in (*map(Path, extra), KIT_DIR):
        p = d / name
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise RecipeError(f"kit file {name} not found")


def _piece(kit: str, name: str, key: str, piece_id: str, extra: tuple[str, ...]) -> KitPiece:
    data = _read(name, extra)[key]
    if piece_id not in data:
        raise RecipeError(f"{name}: no {kit} piece {piece_id!r}; known: {sorted(data)}")
    spec = data[piece_id]
    layers = []
    for raw in spec["layers"]:
        raw = {**raw, "id": f"{kit}.{piece_id}.{raw['id']}"}
        raw.setdefault("stage", "kit")
        layers.append(Layer.model_validate(raw))
    return KitPiece(kit, piece_id, spec.get("title", piece_id), tuple(layers), spec.get("top_row"))


def shoe_piece(style_id: str, extra_dirs: list[Path] | None = None) -> KitPiece:
    return _piece("shoes", "shoes.json", "styles", style_id, tuple(map(str, extra_dirs or [])))


def legwear_piece(kind: str, extra_dirs: list[Path] | None = None) -> KitPiece | None:
    """The legwear piece for ``kind`` (``bare`` -> None)."""
    if kind in ("", "bare", "none"):
        return None
    return _piece("legwear", "legwear.json", "styles", kind, tuple(map(str, extra_dirs or [])))


def arm_piece(piece_id: str, extra_dirs: list[Path] | None = None) -> KitPiece:
    return _piece("arm", "bracelets.json", "pieces", piece_id, tuple(map(str, extra_dirs or [])))


def list_shoe_styles() -> list[str]:
    return sorted(_read("shoes.json")["styles"])


def list_legwear() -> list[str]:
    return sorted(_read("legwear.json")["styles"])


def list_arm_pieces() -> list[str]:
    return sorted(_read("bracelets.json")["pieces"])


def kit_layers(kind: str, *, shoe_style: str | None = None, legwear: str | None = None,
               arm_extras: list[str] | tuple[str, ...] = (), extra_dirs: list[Path] | None = None) -> list[Layer]:
    """The kit layers to paint for a template, in paint order: legwear, shoes (Pants) or arm pieces (Shirt)."""
    out: list[Layer] = []
    if kind == "pants":
        lw = legwear_piece(legwear or "bare", extra_dirs)
        if lw:
            out.extend(lw.layers)
        if shoe_style and shoe_style not in ("none", ""):
            out.extend(shoe_piece(shoe_style, extra_dirs).layers)
    else:
        seen: set[str] = set()
        for e in arm_extras:
            if e in seen:
                continue
            seen.add(e)
            out.extend(arm_piece(e, extra_dirs).layers)
    return out


def check_rule(rule: str, lo_row: int, hi_row: int) -> str | None:
    """CLO-07 ASSERT for one painted kit piece: an error message, or None when the piece obeys its rule."""
    if rule == "shoe_top":
        a, b = T.SHOE_TOP_ROW_RANGE
        return None if a <= lo_row <= b else f"shoe top edge at row {lo_row} is outside rows {a}-{b}"
    if rule == "band":
        for a, b in T.LIMB_BANDS:
            if a <= lo_row and hi_row <= b:
                return None
        return f"piece rows {lo_row}-{hi_row} are not inside one limb band {list(T.LIMB_BANDS)}"
    return None


def validate_catalogue() -> list[str]:
    """Static check of every built-in piece (used by tests): the declared ``top_row`` of each shoe style obeys the shoe rule and
    every layer name is unique. Returns problems (empty = fine)."""
    bad: list[str] = []
    for sid in list_shoe_styles():
        p = shoe_piece(sid)
        msg = check_rule("shoe_top", int(p.top_row or 0), 482)
        if msg:
            bad.append(f"{sid}: {msg}")
        ids = [layer.id for layer in p.layers]
        if len(ids) != len(set(ids)):
            bad.append(f"{sid}: duplicate layer ids")
    return bad
