"""The data the prompt compiler reads: phrase maps, banned terms, style blocks, colour names, the kit inventory.

``CompileCtx`` is the startup-loaded catalogue of APP_SPEC §10.0. Nothing here touches the network; everything is a versioned
file in ``duoskin/data`` (plus the optional user file ``DATA\\user_data\\banned_terms.extra.json``) or the kit inventory.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from duoskin.models import kitenums
from duoskin.models.kitenums import KitInventory

_DATA = Path(__file__).resolve().parent.parent / "data"

TEXT_INVITING = "text_inviting"
STYLE_BLOCKS = ("HOUSE_STYLE_2D", "HOUSE_STYLE_3D_INPUT")


class PhraseError(KeyError):
    """A slot has no human-written source in ``data/phrases.json`` or the kit manifest (a template bug, PRM-05)."""


def norm(text: str) -> str:
    """NFKC and lower-case: how every lint compares text (bible §2.3 rule 4, FAILURE_MODES §5.3)."""
    return unicodedata.normalize("NFKC", str(text)).lower()


@lru_cache(maxsize=16)
def _json(name: str) -> dict[str, Any]:
    return json.loads((_DATA / name).read_text(encoding="utf-8"))


def data_json(name: str) -> dict[str, Any]:
    """A deep copy of a shipped data file (so callers may not mutate the cache)."""
    return json.loads(json.dumps(_json(name)))


# ------------------------------------------------------------------------------------------------ phrases
class Phrases:
    """``data/phrases.json`` with loud lookups: a missing key raises :class:`PhraseError` instead of rendering ``None``."""

    def __init__(self, data: Mapping[str, Any]):
        self.data = data

    @property
    def version(self) -> int:
        return int(self.data["version"])

    def section(self, *path: str) -> Any:
        node: Any = self.data
        for p in path:
            if not isinstance(node, Mapping) or p not in node:
                raise PhraseError("/".join(path))
            node = node[p]
        return node

    def get(self, *path: str) -> str:
        v = self.section(*path)
        if not isinstance(v, str):
            raise PhraseError("/".join(path) + " is not a phrase")
        return v

    def number(self, *path: str) -> int:
        return int(self.section(*path))


# ------------------------------------------------------------------------------------------------ banned terms
def _term_regex(term: str) -> str:
    parts = [re.escape(p) for p in norm(term).split()]
    return r"\s+".join(parts)


class Banned:
    """Banned vocabulary by group (bible §2.4a). ``everywhere`` groups are banned in the whole prompt including EXCLUDE; the
    ``text_inviting`` group is banned in PURPOSE, IMAGES, SUBJECT and MUST and allowed in the fixed OUTPUT and EXCLUDE lines."""

    def __init__(self, groups: Mapping[str, Iterable[str]]):
        self.groups: dict[str, tuple[str, ...]] = {g: tuple(sorted({norm(t).strip() for t in terms if str(t).strip()}))
                                                   for g, terms in groups.items()}
        self._rx = {g: re.compile(r"(?<!\w)(?:" + "|".join(_term_regex(t) for t in terms) + r")(?:s|es|ed|ing)?(?!\w)")
                    for g, terms in self.groups.items() if terms}

    @property
    def everywhere(self) -> tuple[str, ...]:
        return tuple(g for g in self.groups if g != TEXT_INVITING)

    def hits(self, text: str, groups: Iterable[str]) -> list[str]:
        """Sorted distinct banned terms (as written in ``text``) of ``groups`` that occur as whole words."""
        t = norm(text)
        found: set[str] = set()
        for g in groups:
            rx = self._rx.get(g)
            if rx:
                found.update(m.group(0) for m in rx.finditer(t))
        return sorted(found)

    def all_terms(self) -> list[str]:
        return sorted({t for terms in self.groups.values() for t in terms})


def _flatten(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [s for v in node.values() for s in _flatten(v)]
    if isinstance(node, (list, tuple)):
        return [s for v in node for s in _flatten(v)]
    return []


def load_banned(extra_path: str | Path | None = None) -> Banned:
    """The shipped list merged with the user's ``banned_terms.extra.json`` (a list, ``{"terms": [...]}`` or ``{"groups": {...}}``)."""
    groups = {g: list(t) for g, t in _json("banned_terms.json")["groups"].items()}
    if extra_path is None:
        try:
            from duoskin import config

            extra_path = config.paths().user_data_dir / "banned_terms.extra.json"
        except Exception:    # noqa: BLE001 - no data folder (tests, tools)
            extra_path = None
    if extra_path and Path(extra_path).exists():
        raw = json.loads(Path(extra_path).read_text(encoding="utf-8"))
        if isinstance(raw, Mapping) and isinstance(raw.get("groups"), Mapping):
            for g, terms in raw["groups"].items():
                groups.setdefault(g, []).extend(_flatten(terms))
        else:
            groups.setdefault("user", []).extend(_flatten(raw))
    return Banned(groups)


# ------------------------------------------------------------------------------------------------ style
@dataclass(frozen=True)
class StyleGuide:
    data: Mapping[str, Any]

    @property
    def version(self) -> int:
        return int(self.data["version"])

    def block(self, name: str) -> str:
        """The house style block, verbatim (never paraphrased, bible §5.2)."""
        if name == "none" or not name:
            return ""
        try:
            return str(self.data["blocks"][name])
        except KeyError:
            raise PhraseError(f"style block {name}") from None

    def parameters(self) -> dict[str, Any]:
        """The §5.1 parameters (everything except the verbatim blocks)."""
        return {k: v for k, v in self.data.items() if k != "blocks"}


# ------------------------------------------------------------------------------------------------ colour names
def default_colour_namer(hexes: Sequence[str], n: int) -> list[str]:
    """Distinct dictionary names (nearest by CIEDE2000) for up to ``n`` hex colours; hex codes never reach a prompt."""
    from duoskin.imaging.colournames import name_palette

    return name_palette(list(hexes), max_names=n)


@dataclass(frozen=True)
class CompileCtx:
    """The catalogue the compiler reads (APP_SPEC §10.0)."""

    inventory: KitInventory
    phrases: Phrases
    banned: Banned
    style: StyleGuide
    colour_namer: Callable[[Sequence[str], int], list[str]] = default_colour_namer
    settings: Mapping[str, Any] = field(default_factory=dict)       # e.g. {"sparkle_star_allowed": True}

    def style_block(self, name: str) -> str:
        return self.style.block(name)

    def names_for(self, hexes: Sequence[str], n: int = 3) -> list[str]:
        return self.colour_namer(list(hexes), n)


def default_ctx(inventory: KitInventory | None = None, *, banned: Banned | None = None,
                colour_namer: Callable[[Sequence[str], int], list[str]] | None = None) -> CompileCtx:
    """The catalogue for the **current** kit inventory (demo defaults until the app installs a manifest)."""
    return CompileCtx(inventory=inventory or kitenums.current_inventory(), phrases=Phrases(_json("phrases.json")),
                      banned=banned or _default_banned(), style=StyleGuide(_json("style_guide.json")),
                      colour_namer=colour_namer or default_colour_namer)


@lru_cache(maxsize=1)
def _default_banned() -> Banned:
    return load_banned(extra_path="")


def join_names(names: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``: the canonical rendering of colour names (bible §2.3 rule 6)."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
