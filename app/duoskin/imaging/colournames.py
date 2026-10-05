"""Colour names for prompts (APP_SPEC §10.0) and the 5 preview skin tones.

Image prompts never contain hex codes: the compiler asks ``nearest_names(hex)`` for the nearest human-written names
(CIEDE2000, at most 3 per prompt). The dictionary ``data/colour_names.json`` must hold no banned or age-coded word
(``find_banned_names`` is used by CHK-S11 and by the tests).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from duoskin.imaging import palette as P

_DATA = Path(__file__).resolve().parent.parent / "data"
MAX_NAMES_PER_PROMPT = 3

# A small built-in guard list. The real list is data/banned_terms.json (owned by the prompt track); callers pass it to
# ``find_banned_names`` and this fallback only protects the dictionary when that file is not available.
FALLBACK_BANNED = (
    "baby", "kid", "kids", "child", "children", "toddler", "teen", "teenage", "young", "little", "girlish", "boyish", "nude", "flesh",
    "blood", "bloody", "gun", "gunmetal", "weapon", "drug", "sexy", "naked", "hitler", "nazi",
    "pokemon", "mario", "sonic", "disney", "barbie", "tiffany", "coca", "pepsi", "starbucks", "nike", "roblox",
)


@dataclass(frozen=True)
class ColourName:
    name: str
    hex: str
    de: float


@dataclass(frozen=True)
class SkinTone:
    id: str
    name: str
    hex: str


@lru_cache(maxsize=1)
def load_names(path: str | None = None) -> dict[str, str]:
    raw = json.loads(Path(path or _DATA / "colour_names.json").read_text(encoding="utf-8"))
    return {k: P.normalise_hex(v) for k, v in raw["colours"].items()}


@lru_cache(maxsize=1)
def _lab_table() -> tuple[tuple[str, ...], np.ndarray]:
    names = load_names()
    keys = tuple(names)
    return keys, P.palette_lab([names[k] for k in keys])


def nearest_names(hex_colour: str, n: int = 1, *, exclude: Iterable[str] = ()) -> list[ColourName]:
    """The ``n`` nearest dictionary names to ``hex_colour`` by CIEDE2000 (closest first)."""
    keys, lab = _lab_table()
    d = P.deltaE2000(P.hex_to_lab(hex_colour)[None, :], lab)
    skip = set(exclude)
    order = np.argsort(d)
    out: list[ColourName] = []
    for i in order:
        if keys[i] in skip:
            continue
        out.append(ColourName(keys[i], load_names()[keys[i]], float(d[i])))
        if len(out) >= n:
            break
    return out


def colour_name(hex_colour: str) -> str:
    """The single nearest name."""
    return nearest_names(hex_colour, 1)[0].name


def name_palette(colours: Sequence[str], *, max_names: int = MAX_NAMES_PER_PROMPT) -> list[str]:
    """Distinct names for up to ``max_names`` colours (the most distinct names win when a colour's nearest name repeats)."""
    out: list[str] = []
    for h in colours:
        for cand in nearest_names(h, 4, exclude=out):
            out.append(cand.name)
            break
        if len(out) >= max_names:
            break
    return out


def find_banned_names(banned_terms: Iterable[str] | None = None) -> list[str]:
    """Names in the dictionary that contain a banned term as a whole word (NFKC lower-case, word boundaries)."""
    import unicodedata

    terms = [unicodedata.normalize("NFKC", t).lower() for t in (banned_terms if banned_terms is not None else FALLBACK_BANNED)]
    bad: list[str] = []
    for name in load_names():
        low = unicodedata.normalize("NFKC", name).lower()
        if any(re.search(rf"\b{re.escape(t)}\b", low) for t in terms if t):
            bad.append(name)
    return bad


@lru_cache(maxsize=1)
def load_skin_tones() -> tuple[SkinTone, ...]:
    """The 5 preview skin tones from ``data/skin_tones.json`` (``face.skin_tones`` = 5, SPEC)."""
    raw = json.loads((_DATA / "skin_tones.json").read_text(encoding="utf-8"))
    return tuple(SkinTone(t["id"], t["name"], P.normalise_hex(t["hex"])) for t in raw["tones"])


def skin_tone_hex(tone_id: str) -> str:
    for t in load_skin_tones():
        if t.id == tone_id:
            return t.hex
    raise KeyError(tone_id)
