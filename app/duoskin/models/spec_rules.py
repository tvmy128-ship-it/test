"""What the structured-output grammar cannot enforce on a ``DuoSpec`` (APP_SPEC §6.2, bible §3.1, C1 #1).

Word caps, the ``#RRGGBB`` pattern, unique palette ids, every ``*_ref`` resolving (or ``none`` where it is allowed), 2 to 3
anchors, at least 5 contrasts, 5 to 12 palette entries, 1 to 4 colour refs, and the number of prints per garment.

The functions work on a ``DuoSpec`` **or** on the plain dict it dumps to, so the linter, the reviser path and the tests can
check a spec that the strict model would refuse. ``DuoSpec`` calls :func:`spec_problems` from a model validator (a violation
raises :class:`SpecRuleError`, which Pydantic reports as a ``ValidationError`` that goes to the reviser as findings).
Pass ``context={"skip_rules": True}`` to ``model_validate`` to parse a spec without these rules (what the linter does so
that it can report **every** problem at once).

This module imports nothing from ``models/spec.py`` (``spec.py`` imports this module).
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
NONE_REF = "none"
CHARS = ("a", "b")

ANCHORS_MIN, ANCHORS_MAX = 2, 3
CONTRASTS_MIN = 5
PALETTE_MIN, PALETTE_MAX = 5, 12
COLOUR_REFS_MIN, COLOUR_REFS_MAX = 1, 4
TOP_PRINTS_MAX, BOTTOM_PRINTS_MAX = 2, 1

WORD_CAPS: dict[str, int] = {
    "world.theme": 8, "world.structure_note": 12, "world.story": 25,
    "anchor.description": 12, "anchor.on_a": 10, "anchor.on_b": 10,
    "contrast.a_value": 6, "contrast.b_value": 6, "palette.name": 3,
    "role_in_duo": 8, "dna.motif_object": 5, "dna.accessory_style": 6, "dna.energy": 3,
    "hair.description": 12, "print.motif": 12, "shoes.motif": 6, "accessory.description": 15, "makeup.description": 10,
    "plan.how_they_differ": 40,
}


@dataclass(frozen=True)
class Problem:
    """One rule violation. ``path`` is an RFC 6901 JSON Pointer into the spec (``/a/hair/colour_ref``)."""

    code: str
    path: str
    message: str
    rule: str                 # caps | palette | refs | counts | completeness

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class SpecRuleError(ValueError):
    """Raised by the ``DuoSpec`` validator; ``problems`` carries the structured list."""

    def __init__(self, problems: list[Problem]):
        self.problems = problems
        shown = "; ".join(str(p) for p in problems[:12])
        more = f" (+{len(problems) - 12} more)" if len(problems) > 12 else ""
        super().__init__(f"{len(problems)} spec rule problem(s): {shown}{more}")


def words(text: str) -> int:
    """Word count used by every cap: whitespace-separated tokens (a hyphenated word counts once)."""
    return len(str(text).split())


def as_dict(spec: Any) -> dict[str, Any]:
    """``spec`` as a plain dict (a pydantic model is dumped in JSON mode)."""
    if isinstance(spec, Mapping):
        return dict(spec)
    return spec.model_dump(mode="json")


# --------------------------------------------------------------------------------------------------- walkers
def iter_free_text(spec: Any, *, include_plan: bool = False) -> Iterator[tuple[str, str, str]]:
    """Every free-text field of the spec as ``(pointer, cap_key, text)`` (FAILURE_MODES §5.3 ``FREE_TEXT_PATHS``).

    ``cap_key`` indexes :data:`WORD_CAPS`. ``story`` is included: it never reaches an image prompt but is still linted.
    """
    d = as_dict(spec)
    w = d.get("world") or {}
    for key in ("theme", "structure_note", "story"):
        yield f"/world/{key}", f"world.{key}", str(w.get(key, ""))
    for i, a in enumerate(d.get("shared_anchors") or ()):
        for key in ("description", "on_a", "on_b"):
            yield f"/shared_anchors/{i}/{key}", f"anchor.{key}", str(a.get(key, ""))
    for i, c in enumerate(d.get("contrasts") or ()):
        for key in ("a_value", "b_value"):
            yield f"/contrasts/{i}/{key}", f"contrast.{key}", str(c.get(key, ""))
    for i, p in enumerate(d.get("palette") or ()):
        yield f"/palette/{i}/name", "palette.name", str(p.get("name", ""))
    for ck in CHARS:
        c = d.get(ck) or {}
        yield f"/{ck}/role_in_duo", "role_in_duo", str(c.get("role_in_duo", ""))
        dna = c.get("dna") or {}
        for key in ("motif_object", "accessory_style", "energy"):
            yield f"/{ck}/dna/{key}", f"dna.{key}", str(dna.get(key, ""))
        yield f"/{ck}/hair/description", "hair.description", str((c.get("hair") or {}).get("description", ""))
        for part in ("top", "bottom"):
            for i, p in enumerate((c.get(part) or {}).get("prints") or ()):
                yield f"/{ck}/{part}/prints/{i}/motif", "print.motif", str(p.get("motif", ""))
        yield f"/{ck}/bottom/shoes/motif", "shoes.motif", str(((c.get("bottom") or {}).get("shoes") or {}).get("motif", ""))
        for i, a in enumerate(c.get("accessories") or ()):
            yield f"/{ck}/accessories/{i}/description", "accessory.description", str(a.get("description", ""))
        yield f"/{ck}/makeup/description", "makeup.description", str((c.get("makeup") or {}).get("description", ""))
    if include_plan and "how_they_differ" in d:
        yield "/how_they_differ", "plan.how_they_differ", str(d["how_they_differ"])


_FACE_REFS_REQUIRED = ("iris_ref", "iris_dark_ref", "pupil_ref", "sclera_ref", "lash_ref", "brow_ref", "mouth_line_ref",
                       "mouth_inner_ref", "tongue_ref")


def iter_refs(spec: Any) -> Iterator[tuple[str, str, bool]]:
    """Every palette reference as ``(pointer, value, none_allowed)``."""
    d = as_dict(spec)
    for ck in CHARS:
        c = d.get(ck) or {}
        yield f"/{ck}/body/modesty_ref", str((c.get("body") or {}).get("modesty_ref", "")), False
        face = c.get("face") or {}
        for k in _FACE_REFS_REQUIRED:
            yield f"/{ck}/face/{k}", str(face.get(k, "")), False
        for k in ("teeth_ref", "blush_ref"):
            yield f"/{ck}/face/{k}", str(face.get(k, "")), True
        hair = c.get("hair") or {}
        yield f"/{ck}/hair/colour_ref", str(hair.get("colour_ref", "")), False
        yield f"/{ck}/hair/shadow_ref", str(hair.get("shadow_ref", "")), False
        yield f"/{ck}/hair/highlight_ref", str(hair.get("highlight_ref", "")), True
        for part in ("top", "bottom"):
            g = c.get(part) or {}
            yield f"/{ck}/{part}/base_ref", str(g.get("base_ref", "")), False
            yield f"/{ck}/{part}/second_ref", str(g.get("second_ref", "")), True
            yield f"/{ck}/{part}/trim_ref", str(g.get("trim_ref", "")), True
            for i, p in enumerate(g.get("prints") or ()):
                for j, r in enumerate(p.get("colour_refs") or ()):
                    yield f"/{ck}/{part}/prints/{i}/colour_refs/{j}", str(r), False
        bottom = c.get("bottom") or {}
        yield f"/{ck}/bottom/legwear_ref", str(bottom.get("legwear_ref", "")), True
        shoes = bottom.get("shoes") or {}
        yield f"/{ck}/bottom/shoes/base_ref", str(shoes.get("base_ref", "")), False
        yield f"/{ck}/bottom/shoes/sole_ref", str(shoes.get("sole_ref", "")), False
        yield f"/{ck}/bottom/shoes/accent_ref", str(shoes.get("accent_ref", "")), True
        for i, a in enumerate(c.get("accessories") or ()):
            for j, r in enumerate(a.get("colour_refs") or ()):
                yield f"/{ck}/accessories/{i}/colour_refs/{j}", str(r), False
        for j, r in enumerate((c.get("makeup") or {}).get("colour_refs") or ()):
            yield f"/{ck}/makeup/colour_refs/{j}", str(r), False


# --------------------------------------------------------------------------------------------------- the rules
def palette_ids(spec: Any) -> list[str]:
    return [str(p.get("id", "")) for p in (as_dict(spec).get("palette") or ())]


def spec_problems(spec: Any) -> list[Problem]:
    """All rule violations of one spec (an empty list means the spec is valid). The order is stable."""
    d = as_dict(spec)
    out: list[Problem] = []

    # ---- palette ----------------------------------------------------------------------------
    palette = d.get("palette") or []
    n = len(palette)
    if not PALETTE_MIN <= n <= PALETTE_MAX:
        out.append(Problem("palette_count", "/palette", f"needs {PALETTE_MIN} to {PALETTE_MAX} colours, has {n}", "counts"))
    seen: dict[str, int] = {}
    for i, p in enumerate(palette):
        pid = str(p.get("id", ""))
        if not ID_RE.match(pid) or pid.lower() == NONE_REF:
            out.append(Problem("palette_id", f"/palette/{i}/id", f"{pid!r} is not a valid palette id (letters, digits, - or _; not 'none')", "palette"))
        elif pid in seen:
            out.append(Problem("palette_id_unique", f"/palette/{i}/id", f"palette id {pid!r} is already used by /palette/{seen[pid]}", "palette"))
        else:
            seen[pid] = i
        hx = str(p.get("hex", ""))
        if not HEX_RE.match(hx):
            out.append(Problem("palette_hex", f"/palette/{i}/hex", f"{hx!r} is not #RRGGBB", "palette"))

    # ---- references -------------------------------------------------------------------------
    ids = set(seen)
    for path, ref, none_ok in iter_refs(d):
        if ref == NONE_REF:
            if not none_ok:
                out.append(Problem("ref_none", path, "a palette id is required here, not 'none'", "refs"))
        elif ref not in ids:
            out.append(Problem("ref_dangling", path, f"{ref!r} is not a palette id", "refs"))

    # ---- counts -----------------------------------------------------------------------------
    anchors = d.get("shared_anchors") or []
    if not ANCHORS_MIN <= len(anchors) <= ANCHORS_MAX:
        out.append(Problem("anchor_count", "/shared_anchors", f"needs {ANCHORS_MIN} or {ANCHORS_MAX} anchors, has {len(anchors)}", "counts"))
    contrasts = d.get("contrasts") or []
    if len(contrasts) < CONTRASTS_MIN:
        out.append(Problem("contrast_count", "/contrasts", f"needs at least {CONTRASTS_MIN} contrasts, has {len(contrasts)}", "counts"))
    for ck in CHARS:
        c = d.get(ck) or {}
        for part, cap in (("top", TOP_PRINTS_MAX), ("bottom", BOTTOM_PRINTS_MAX)):
            prints = (c.get(part) or {}).get("prints") or []
            if len(prints) > cap:
                out.append(Problem("print_count", f"/{ck}/{part}/prints", f"at most {cap} print(s) on the {part}, has {len(prints)}", "counts"))
            for i, p in enumerate(prints):
                nref = len(p.get("colour_refs") or [])
                if not COLOUR_REFS_MIN <= nref <= COLOUR_REFS_MAX:
                    out.append(Problem("colour_refs_count", f"/{ck}/{part}/prints/{i}/colour_refs",
                                       f"needs {COLOUR_REFS_MIN} to {COLOUR_REFS_MAX} colour ids, has {nref}", "counts"))
        for i, a in enumerate(c.get("accessories") or []):
            nref = len(a.get("colour_refs") or [])
            if not COLOUR_REFS_MIN <= nref <= COLOUR_REFS_MAX:
                out.append(Problem("colour_refs_count", f"/{ck}/accessories/{i}/colour_refs",
                                   f"needs {COLOUR_REFS_MIN} to {COLOUR_REFS_MAX} colour ids, has {nref}", "counts"))
        mk = c.get("makeup") or {}
        if len(mk.get("colour_refs") or []) > COLOUR_REFS_MAX:
            out.append(Problem("colour_refs_count", f"/{ck}/makeup/colour_refs", f"at most {COLOUR_REFS_MAX} colour ids", "counts"))
        extras = list((c.get("top") or {}).get("arm_extras") or [])
        if len(extras) != len(set(extras)):
            out.append(Problem("arm_extras_unique", f"/{ck}/top/arm_extras", "each arm extra may appear once", "counts"))
        if mk.get("kind") == "none" and (str(mk.get("description", "")).strip() or mk.get("colour_refs")):
            out.append(Problem("makeup_none", f"/{ck}/makeup", "kind none needs an empty description and no colour ids", "completeness"))

    # ---- word caps and completeness ---------------------------------------------------------
    world = d.get("world") or {}
    for path, key, text in iter_free_text(d):
        cap = WORD_CAPS[key]
        if words(text) > cap:
            out.append(Problem("word_cap", path, f"{words(text)} words, at most {cap}", "caps"))
    struct = world.get("pair_structure")
    note = str(world.get("structure_note", "")).strip()
    if struct == "other" and not note:
        out.append(Problem("structure_note", "/world/structure_note", "pair_structure 'other' needs a structure_note", "completeness"))
    if struct not in (None, "other") and note:
        out.append(Problem("structure_note", "/world/structure_note", "structure_note must be an empty string unless pair_structure is 'other'", "completeness"))
    for ck in CHARS:
        c = d.get(ck) or {}
        for part in ("top", "bottom"):
            for i, p in enumerate((c.get(part) or {}).get("prints") or []):
                if not str(p.get("motif", "")).strip():
                    out.append(Problem("print_motif", f"/{ck}/{part}/prints/{i}/motif", "a print needs a motif", "completeness"))
        for i, a in enumerate(c.get("accessories") or []):
            if not str(a.get("description", "")).strip():
                out.append(Problem("accessory_description", f"/{ck}/accessories/{i}/description", "an accessory needs a description", "completeness"))
    return out


def plan_set_problems(plan: Any) -> list[Problem]:
    """Rules for a ``PlanSet`` itself (the how_they_differ cap and every spec's own problems, path-prefixed)."""
    d = as_dict(plan)
    out: list[Problem] = []
    text = str(d.get("how_they_differ", ""))
    cap = WORD_CAPS["plan.how_they_differ"]
    if words(text) > cap:
        out.append(Problem("word_cap", "/how_they_differ", f"{words(text)} words, at most {cap}", "caps"))
    for i, spec in enumerate(d.get("specs") or []):
        for p in spec_problems(spec):
            out.append(Problem(p.code, f"/specs/{i}{p.path}", p.message, p.rule))
    return out


def problems_from_error(exc: BaseException) -> list[Problem]:
    """The structured problems inside a pydantic ``ValidationError`` raised by ``DuoSpec`` (empty when it holds none)."""
    errors = getattr(exc, "errors", None)
    if errors is None:
        return []
    found: list[Problem] = []
    for e in errors():
        err = (e.get("ctx") or {}).get("error")
        if isinstance(err, SpecRuleError):
            found.extend(err.problems)
    return found
