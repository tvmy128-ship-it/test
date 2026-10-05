"""The DNA router: which DNA fields may enter which image prompt (bible §3.3, APP_SPEC §3.2 and §10.0, PRM-10).

* **WORLD fields** (``theme``, ``palette_family``, ``material_family``, ``detail_level``, the anchors) belong to both characters; only
  ``detail_level`` is ever routed (face parts and prints).
* **CHARACTER fields** (``shape_language``, ``colour_plan``, ``focal_location``, ``motif_object``, ``accessory_style``, ``energy``,
  ``hair.kit_style_id``) are routed only from the asset's **own** character, and only the ones listed for the template.
* At most 2 fields reach any prompt (``prm.dna_fields_max``); the compiler has no other source for these slots.
* Palette hexes, the colour plan, the story and the pair structure have no slot source at all.

``route_dna(template_id, spec, character) -> list[DnaSlot]`` is the single entry point. The table below is the upper bound of the
router unit test: the fields a template actually uses must be a subset of its row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from duoskin.models.common import CharKey
from duoskin.models.spec import DuoSpec
from duoskin.prompts.catalog import Phrases, data_json

CHARACTER_FIELDS = ("shape_language", "colour_plan", "focal_location", "motif_object", "accessory_style", "energy", "hair.kit_style_id")
WORLD_FIELDS = ("theme", "palette_family", "material_family", "detail_level")
ROUTABLE = ("shape_language", "detail_level", "motif_object")       # the only fields any template may use today
MAX_DNA_FIELDS = 2                                                  # prm.dna_fields_max (checked again by the compiler lint)

_SHAPE_DETAIL = ("shape_language", "detail_level")
_MOTIF_SHAPE = ("motif_object", "shape_language")


@dataclass(frozen=True)
class RouteRow:
    """One row of bible §3.3."""

    fields: tuple[str, ...]
    slots: tuple[tuple[str, str], ...] = ()          # (field, slot name) overrides; default slot names below
    forms: tuple[tuple[str, str], ...] = ()          # (field, phrase form) overrides: line | short | clause
    per_part: tuple[tuple[str, tuple[str, ...]], ...] = ()   # part -> the fields that part carries


_DEFAULT_SLOT = {"shape_language": "shape_language_line", "detail_level": "detail_level_line", "motif_object": "motif_object"}

ROUTING: dict[str, RouteRow] = {
    # I1 concept (per character) and its variants: shape language + motif object
    "I1.concept_char": RouteRow(_MOTIF_SHAPE),
    "I1e.concept_edit": RouteRow(_MOTIF_SHAPE),
    "I1f.concept_front": RouteRow(_MOTIF_SHAPE),
    "I1b.concept_back": RouteRow(_MOTIF_SHAPE),
    "I1p.concept_partner_style": RouteRow(_MOTIF_SHAPE),
    "I1j.concept_joint": RouteRow(()),               # a joint call would mix both characters' CHARACTER fields
    # prints: shape language + detail level (deviation from PROPOSAL_DECISION Q1, APP_SPEC §3.2)
    "I2.print": RouteRow(_SHAPE_DETAIL),
    "I2.print_frame": RouteRow(_SHAPE_DETAIL),
    "I2.shoe_decal": RouteRow(_SHAPE_DETAIL),
    "R2.print": RouteRow(_SHAPE_DETAIL, slots=(("shape_language", "shape_short"), ("detail_level", "detail_level_short")),
                         forms=(("shape_language", "short"), ("detail_level", "short"))),
    # face parts: line parts carry shape language only; iris and mouth_open carry the detail level (R1: one field) or both (I3)
    "I3.face_part": RouteRow(_SHAPE_DETAIL, per_part=(
        ("iris", _SHAPE_DETAIL), ("mouth_open", _SHAPE_DETAIL), ("lash_upper", ("shape_language",)),
        ("brow", ("shape_language",)), ("mouth_closed", ("shape_language",)))),
    "I3.face_part_incanvas": RouteRow(_SHAPE_DETAIL, per_part=(
        ("iris", _SHAPE_DETAIL), ("mouth_open", _SHAPE_DETAIL), ("lash_upper", ("shape_language",)),
        ("brow", ("shape_language",)), ("mouth_closed", ("shape_language",)))),
    "R1.face_part": RouteRow(_SHAPE_DETAIL, slots=(("shape_language", "shape_short"), ("detail_level", "detail_clause")),
                             forms=(("shape_language", "short"), ("detail_level", "clause")), per_part=(
        ("iris", ("detail_level",)), ("mouth_open", ("detail_level",)), ("lash_upper", ("shape_language",)),
        ("brow", ("shape_language",)), ("mouth_closed", ("shape_language",)), ("closed_lid_line", ("shape_language",)))),
    # hair
    "I4.hair_front": RouteRow(("shape_language",)),
    "I4k.hair_kit_first": RouteRow(("shape_language",)),
    # accessories and badges: motif object + shape language
    "I5.accessory_front": RouteRow(_MOTIF_SHAPE),
    "I5.accessory_frame": RouteRow(_MOTIF_SHAPE),
    "I5g.accessory_guided": RouteRow(_MOTIF_SHAPE),
    "I6.badge_art": RouteRow(_MOTIF_SHAPE),
    "I6.badge_frame": RouteRow(_MOTIF_SHAPE),
}
# every other template (I0, I7, I8, I10, I11, T2, the global-edit variants I2e..I6e, the L routes) is DNA-free: Image 1 or the
# shared library asset already carries the look (bible §3.3).


@dataclass(frozen=True)
class DnaSlot:
    """One routed DNA field: the slot the template fills, its final text, and where it came from."""

    field: str
    slot: str
    value: str
    scope: Literal["character", "world"]
    character: CharKey | None
    source_path: str                  # JSON pointer into the spec, e.g. /a/dna/shape_language or /world/detail_level

    @property
    def used(self) -> bool:
        return bool(self.value)


def allowed_fields(template_id: str) -> tuple[str, ...]:
    """The routing row of a template (the upper bound the router test enforces); ``()`` for DNA-free templates."""
    row = ROUTING.get(_strip(template_id))
    return row.fields if row else ()


def scope_of(field: str) -> Literal["character", "world"]:
    return "world" if field in WORLD_FIELDS else "character"


def _strip(template_id: str) -> str:
    return template_id.split("@")[0]


def _as_spec(spec: Any) -> DuoSpec:
    if isinstance(spec, DuoSpec):
        return spec
    return DuoSpec.model_validate(spec, context={"skip_rules": True})


def _value(field: str, form: str, spec: DuoSpec, character: CharKey | None, phrases: Phrases) -> tuple[str, str]:
    if field == "detail_level":
        level = spec.world.detail_level
        return phrases.get("dna", "detail_level", form, level), "/world/detail_level"
    if character not in ("a", "b"):
        raise ValueError(f"field {field} is a CHARACTER field: a character (a or b) is required")
    dna = spec.a.dna if character == "a" else spec.b.dna
    if field == "shape_language":
        return phrases.get("dna", "shape_language", form, dna.shape_language), f"/{character}/dna/shape_language"
    if field == "motif_object":
        return dna.motif_object.strip(), f"/{character}/dna/motif_object"
    raise ValueError(f"field {field!r} is not routable")


def route_dna(template_id: str, spec: Any, character: CharKey | None, *, part: str | None = None,
              phrases: Phrases | None = None) -> list[DnaSlot]:
    """The DNA slots of one image call: at most 2, all from ``character`` (or WORLD fields).

    ``part`` selects the face-part row of I3 and R1 (``iris``, ``lash_upper``, ``brow``, ``mouth_closed``, ``mouth_open``). Without a
    part, I3 and R1 route their full row. Fields whose value is empty (a character with no motif object) are returned with an
    empty ``value`` so the compiler drops their line; they never count as used.
    """
    row = ROUTING.get(_strip(template_id))
    if row is None or not row.fields:
        return []
    ph = phrases or Phrases(data_json("phrases.json"))
    spec = _as_spec(spec)
    fields = row.fields
    if part is not None and row.per_part:
        table = dict(row.per_part)
        if part not in table:
            raise ValueError(f"{template_id}: no routing for part {part!r}")
        fields = table[part]
    slot_names = {**_DEFAULT_SLOT, **dict(row.slots)}
    forms = {"shape_language": "line", "detail_level": "line", **dict(row.forms)}
    out: list[DnaSlot] = []
    for f in fields:
        value, path = _value(f, forms.get(f, "line"), spec, character, ph)
        out.append(DnaSlot(field=f, slot=slot_names[f], value=value, scope=scope_of(f),
                           character=character if scope_of(f) == "character" else None, source_path=path))
    if len(out) > MAX_DNA_FIELDS:
        raise AssertionError(f"{template_id}: routes {len(out)} DNA fields, at most {MAX_DNA_FIELDS}")
    return out
