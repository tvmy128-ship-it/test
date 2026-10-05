"""The DNA card: a **view** of the spec (APP_SPEC §3.2, §6.4; PROPOSAL_DECISION #1).

WORLD fields (shared by both characters): ``world`` (theme, pair_structure, structure_note, story, palette_family,
material_family, detail_level) and the shared anchors. CHARACTER fields (one set per character): ``dna`` (shape_language,
colour_plan, focal_location, motif_object, accessory_style, energy) and the hair kit style.

The card is versioned with the spec: ``version`` is the ``SpecRecord.version`` it was built from, and ``locked`` is True from
the concept lock (C3) on. "Locked" means a versioned default, not a wall: a change creates a new card with a field diff.
Palette hexes live on the card for the log and the diff; they are never sent to an image model.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import Field

from duoskin.models.common import CharKey, Strict
from duoskin.models.spec import Anchor, CharacterDNA, DuoSpec, WorldDNA

HAIR_CUSTOM = "hair_custom"
# the CHARACTER fields counted by PLN-DNA-01 (APP_SPEC §3.2): the hair kit style is the sixth
DNA_COMPARED_FIELDS = ("shape_language", "colour_plan", "focal_location", "hair.kit_style_id", "motif_object", "accessory_style")


class DnaCard(Strict):
    spec_id: str
    version: int                                    # = SpecRecord.version at the time
    locked: bool                                    # True from C3 on (a versioned default, not a wall)
    source: Literal["planner", "concept_extracted", "change", "revise_plan"]
    world: WorldDNA
    anchors: list[Anchor]
    palette_family: str
    palette_hexes: dict[str, str]                   # id -> hex (never sent to an image model)
    a: CharacterDNA
    b: CharacterDNA
    hair_kit: dict[CharKey, str]
    diff_from_previous: list[str] = Field(default_factory=list)   # changed field paths

    def character(self, key: CharKey) -> CharacterDNA:
        return self.a if key == "a" else self.b


def normalise_text(text: str) -> str:
    """NFKC, case-folded, punctuation dropped, whitespace collapsed: how free-text DNA fields are compared."""
    t = unicodedata.normalize("NFKC", str(text)).casefold()
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _unpack(rec: Any) -> tuple[str, int, DuoSpec]:
    spec = getattr(rec, "spec", rec)
    if isinstance(spec, dict):
        spec = DuoSpec.model_validate(spec, context={"skip_rules": True})
    if not isinstance(spec, DuoSpec):
        raise TypeError(f"card_from_spec needs a SpecRecord or a DuoSpec, got {type(rec).__name__}")
    return str(getattr(rec, "id", "")), int(getattr(rec, "version", 1)), spec


def card_from_spec(rec: Any, locked: bool = False, source: str = "planner", *, previous: DnaCard | None = None) -> DnaCard:
    """Build the DNA card of a ``SpecRecord`` (or of a bare ``DuoSpec``).

    ``previous`` is the card of the parent spec; when given, ``diff_from_previous`` lists every changed field path.
    """
    spec_id, version, spec = _unpack(rec)
    card = DnaCard(
        spec_id=spec_id, version=version, locked=locked, source=source,   # type: ignore[arg-type]
        world=spec.world, anchors=list(spec.shared_anchors), palette_family=spec.world.palette_family,
        palette_hexes={c.id: c.hex.upper() for c in spec.palette},
        a=spec.a.dna, b=spec.b.dna,
        hair_kit={"a": spec.a.hair.kit_style_id, "b": spec.b.hair.kit_style_id})
    if previous is not None:
        card = card.model_copy(update={"diff_from_previous": card_diff(previous, card)})
    return card


def card_diff(old: DnaCard, new: DnaCard) -> list[str]:
    """Changed field paths between two cards (``/world/theme``, ``/a/dna/shape_language``, ``/palette/p3``, ...), sorted."""
    out: list[str] = []
    ow, nw = old.world.model_dump(), new.world.model_dump()
    out += [f"/world/{k}" for k in nw if ow.get(k) != nw[k]]
    if [a.model_dump() for a in old.anchors] != [a.model_dump() for a in new.anchors]:
        out.append("/shared_anchors")
    for ck in ("a", "b"):
        od, nd = old.character(ck).model_dump(), new.character(ck).model_dump()   # type: ignore[arg-type]
        out += [f"/{ck}/dna/{k}" for k in nd if od.get(k) != nd[k]]
        if old.hair_kit.get(ck) != new.hair_kit.get(ck):
            out.append(f"/{ck}/hair/kit_style_id")
    for pid in sorted(set(old.palette_hexes) | set(new.palette_hexes)):
        if old.palette_hexes.get(pid) != new.palette_hexes.get(pid):
            out.append(f"/palette/{pid}")
    return sorted(out)


def character_field_differences(card: DnaCard, *, hair_custom_contrast: bool = False) -> list[str]:
    """The CHARACTER DNA fields in which A and B differ (lint PLN-DNA-01 needs at least ``pln.dna_char_diff_min`` = 2).

    Compared: ``shape_language``, ``colour_plan``, ``focal_location``, ``hair.kit_style_id`` (a difference unless both are
    ``hair_custom``, APP_SPEC §2 S19), ``motif_object`` and ``accessory_style`` (normalised text). Two ``hair_custom`` hairs count
    as differing only when the caller says the hair contrast of PLN-03 is declared (``hair_custom_contrast=True``).
    """
    out: list[str] = []
    a, b = card.a, card.b
    for f in ("shape_language", "colour_plan", "focal_location"):
        if getattr(a, f) != getattr(b, f):
            out.append(f)
    ha, hb = card.hair_kit.get("a", ""), card.hair_kit.get("b", "")
    if ha == HAIR_CUSTOM and hb == HAIR_CUSTOM:
        if hair_custom_contrast:
            out.append("hair.kit_style_id")
    elif ha != hb:
        out.append("hair.kit_style_id")
    for f in ("motif_object", "accessory_style"):
        if normalise_text(getattr(a, f)) != normalise_text(getattr(b, f)):
            out.append(f)
    return [f for f in DNA_COMPARED_FIELDS if f in out]


def recent_cards_json(cards: list[DnaCard], n: int = 5) -> list[dict[str, Any]]:
    """The compact JSON of the last ``n`` cards for the planner's ``<recent_cards>`` block (no hexes, no story)."""
    out = []
    for c in cards[-n:]:
        w = c.world
        out.append({"theme": w.theme, "pair_structure": w.pair_structure, "palette_family": w.palette_family,
                    "material_family": w.material_family, "detail_level": w.detail_level,
                    "anchor_kinds": sorted({x.kind for x in c.anchors}),
                    "a": {"shape_language": c.a.shape_language, "colour_plan": c.a.colour_plan, "focal_location": c.a.focal_location,
                          "motif_object": c.a.motif_object, "accessory_style": c.a.accessory_style, "hair_kit": c.hair_kit.get("a", "")},
                    "b": {"shape_language": c.b.shape_language, "colour_plan": c.b.colour_plan, "focal_location": c.b.focal_location,
                          "motif_object": c.b.motif_object, "accessory_style": c.b.accessory_style, "hair_kit": c.hair_kit.get("b", "")}})
    return out


__all__ = [
    "DNA_COMPARED_FIELDS",
    "DnaCard",
    "card_diff",
    "card_from_spec",
    "character_field_differences",
    "normalise_text",
    "recent_cards_json",
]
