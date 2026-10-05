"""Slot builders: spec fields -> the text slots of a template (bible §2.3, §3.4).

Slots come only from spec fields, the human-written phrase maps (``data/phrases.json``), the kit catalogue (``prompt_phrase``)
and linted model text. DNA slots are **not** built here; ``prompts/dna_router.py`` is their only source. Colour names come from the
colour-name dictionary (nearest CIEDE2000, at most 3 per prompt); hex codes never appear in a slot.

A builder returns :class:`Built`: the slot values (an empty string means "empty", which drops a line or clause), provider fields
(for example Recraft ``controls.colors``) and, where the size depends on the call, the size. Builders with more than one
``level`` degrade deterministically: level 0 is the full text, higher levels drop the least important clauses until the compiler's
length budget is met.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from duoskin.models.common import CharKey
from duoskin.models.kitenums import KitError
from duoskin.models.spec import Character, DuoSpec
from duoskin.prompts import freetext
from duoskin.prompts.catalog import CompileCtx, join_names
from duoskin.prompts.registry import TemplateMeta


class PromptBuildError(ValueError):
    """A slot cannot be built from the spec (a missing phrase, an unknown id, a dangling palette ref)."""


class FreeTextLintError(PromptBuildError):
    """A model-written string failed the free-text lint (PLN-12, PRM-12): it goes back to the reviser, never edited silently."""

    def __init__(self, path: str, text: str, problems: list[freetext.FreeTextProblem]):
        self.path, self.text, self.problems = path, text, problems
        super().__init__(f"free text at {path} failed the lint: " + "; ".join(str(p) for p in problems))


@dataclass
class Built:
    slots: dict[str, str]
    provider_fields: dict[str, Any] = field(default_factory=dict)
    size: str | None = None
    flags: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildArgs:
    spec: DuoSpec
    char: CharKey | None
    ctx: CompileCtx
    inputs: dict[str, Any]
    level: int
    meta: TemplateMeta

    @property
    def me(self) -> Character:
        if self.char not in ("a", "b"):
            raise PromptBuildError(f"{self.meta.id}: a character (a or b) is required")
        return self.spec.a if self.char == "a" else self.spec.b

    @property
    def phr(self):
        return self.ctx.phrases

    def input(self, name: str, default: Any = None) -> Any:
        return self.inputs.get(name, default)


# ------------------------------------------------------------------------------------------------ helpers
def palette_hex(spec: DuoSpec, ref: str) -> str:
    for c in spec.palette:
        if c.id == ref:
            return c.hex
    raise PromptBuildError(f"palette id {ref!r} does not exist")


def role_hex(spec: DuoSpec, role: str) -> str | None:
    for c in spec.palette:
        if c.role == role:
            return c.hex
    return None


def names(a: BuildArgs, refs: list[str], n: int = 3) -> str:
    """Dictionary colour names (at most ``n``, distinct) for palette refs, joined ``a, b and c``."""
    hexes = [palette_hex(a.spec, r) for r in refs if r and r != "none"]
    return join_names(a.ctx.names_for(hexes, n)) if hexes else ""


def one_name(a: BuildArgs, ref: str) -> str:
    if not ref or ref == "none":
        return ""
    return a.ctx.names_for([palette_hex(a.spec, ref)], 1)[0]


def ft(a: BuildArgs, text: str, cap: int, path: str, *, allow_empty: bool = True) -> str:
    """The free-text lint for a model-written string that becomes a slot; returns the stripped text or raises."""
    t = " ".join(str(text).split())
    problems = freetext.free_text_problems(t, cap, a.ctx.banned, allow_empty=allow_empty, strict=True)
    if problems:
        raise FreeTextLintError(path, t, problems)
    return t


def lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _words(s: str) -> int:
    return len(s.split())


def fit_clauses(clauses: list[tuple[int, str]], cap: int) -> str:
    """Join clauses with ``, `` in the given order, dropping the least important whole clauses (highest priority number, the later
    one on a tie) until the text has at most ``cap`` words. The most important clause is never dropped; it is cut at a comma or at
    a word boundary if it alone is too long. Empty clauses are ignored."""
    items = [(p, t, i) for i, (p, t) in enumerate(clauses) if t and t.strip()]
    while len(items) > 1 and _words(", ".join(t for _, t, _ in sorted(items, key=lambda x: x[2]))) > cap:
        drop = max(items, key=lambda x: (x[0], x[2]))
        items.remove(drop)
    text = ", ".join(t for _, t, _ in sorted(items, key=lambda x: x[2]))
    if _words(text) > cap:
        parts = text.split(", ")
        while len(parts) > 1 and _words(", ".join(parts)) > cap:
            parts.pop()
        text = ", ".join(parts)
        if _words(text) > cap:
            text = " ".join(text.split()[:cap])
    return text


def cut_words(a: BuildArgs, group: str, value: str, **fmt: str) -> str:
    return a.phr.get("cut_words", group, value).format(**fmt) if fmt else a.phr.get("cut_words", group, value)


def _recipe_phrase(a: BuildArgs, recipe_id: str) -> str:
    try:
        phrase = a.ctx.inventory.recipe(recipe_id).prompt_phrase.strip()
    except KitError as exc:
        raise PromptBuildError(str(exc)) from exc
    if not phrase:
        raise PromptBuildError(f"recipe {recipe_id!r} has no prompt_phrase in the kit manifest")
    return phrase


def print_clause(a: BuildArgs, prints: list, part: str) -> str:
    """``with a small {motif} print {placement}`` for the first print that a concept view shows; empty when none does."""
    table = a.phr.section("print", f"placement_{part}")
    for i, p in enumerate(prints):
        motif = ft(a, p.motif, 12, f"/{a.char}/{part}/prints/{i}/motif")
        if not motif:
            continue
        place = table.get(p.region, "")
        if not place:
            continue
        motif = re.sub(r"^(?:one|an?|the)\s+", "", motif, flags=re.IGNORECASE)         # the template already says "a ... print"
        size = a.phr.get("print", "size_word", p.scale)
        if size and motif.lower().split(" ")[0] == size:                                # "small gear wheel" is not "small small gear wheel"
            size = ""
        return a.phr.get("print", "template").format(sized_motif=f"{size} {motif}" if size else motif, placement=place)
    return ""


def fringe_present(a: BuildArgs, c: Character) -> bool:
    """Bible §3.4: a fringe module, or ``kit_default`` when the kit style's manifest ``default_fringe`` is not ``none``."""
    fid = c.hair.fringe_id
    if fid == "none":
        return False
    if fid == "kit_default":
        style = a.ctx.inventory.hair_style(c.hair.kit_style_id)
        return bool(style and style.default_fringe != "none")
    return True


def hair_phrase(a: BuildArgs, c: Character, path: str, cap: int) -> str:
    """Kit hair: the style's ``prompt_phrase`` + ``hair.description``. ``hair_custom``: description + parting + fringe phrase."""
    desc = ft(a, c.hair.description, 12, f"{path}/hair/description")
    style = a.ctx.inventory.hair_style(c.hair.kit_style_id)
    if style is not None:
        if not style.prompt_phrase.strip():
            raise PromptBuildError(f"hair style {style.id!r} has no prompt_phrase")
        return fit_clauses(_dedupe([(0, style.prompt_phrase.strip()), (1, desc)]), cap)
    parting = a.phr.get("hair", "parting_phrase", c.hair.parting)
    fringe = a.phr.get("hair", "fringe_phrase", "present" if fringe_present(a, c) else "absent")
    return fit_clauses([(0, desc), (2, parting), (1, fringe)], cap)


def _dedupe(clauses: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop a clause whose words already sit inside another (a kit phrase repeated by the description)."""
    norm_ = [" ".join(re.findall(r"[a-z0-9]+", t.lower())) for _, t in clauses]
    keep = []
    for i, (p, t) in enumerate(clauses):
        if not t.strip():
            continue
        if any(j != i and norm_[i] and norm_[i] in norm_[j] and (norm_[i] != norm_[j] or j < i) for j in range(len(clauses))):
            continue
        keep.append((p, t))
    return keep


_STEM_END = re.compile(r"(?:ed|es|s)$")


def _stems(text: str) -> set[str]:
    return {_STEM_END.sub("", w) if len(w) > 4 else w for w in re.findall(r"[a-z]+", text.lower())}


def _new_words(clause: str, recipe_phrase: str) -> str:
    """``clause`` unless the recipe phrase already says all of it ("long sleeves" after "long-sleeved crew-neck tee"): the same words twice
    spend the prompt budget and make the picture model weigh them double."""
    return "" if clause and _stems(clause) <= _stems(recipe_phrase) else clause


def top_phrase(a: BuildArgs, c: Character, path: str, cap: int) -> str:
    t = c.top
    lvl = a.level
    inner = ""
    if t.front in ("open", "layered"):
        inner = _recipe_phrase(a, t.inner_recipe_id) if t.inner_recipe_id != "none" else "an inner layer"
    recipe = _recipe_phrase(a, t.recipe_id)
    clauses = [
        (0, recipe),
        (3, _new_words(cut_words(a, "top.sleeve", t.sleeve), recipe)),
        (4, _new_words(cut_words(a, "top.hem", t.hem), recipe) if lvl < 2 else ""),
        (2, _new_words(cut_words(a, "top.neckline", t.neckline), recipe)),
        (5 if t.front != "closed" else 6, cut_words(a, "top.front", t.front, inner=inner) if (lvl < 1 or t.front != "closed") else ""),
        (1, print_clause(a, t.prints, "top")),
    ]
    return fit_clauses(clauses, cap)


def bottom_phrase(a: BuildArgs, c: Character, path: str, cap: int) -> str:
    b = c.bottom
    lvl = a.level
    recipe = _recipe_phrase(a, b.recipe_id)
    clauses = [
        (0, recipe),
        (2, _new_words(cut_words(a, "bottom.leg", b.leg), recipe)),
        (4, _new_words(cut_words(a, "bottom.waist", b.waist), recipe) if lvl < 1 else ""),
        (3, cut_words(a, "bottom.legwear", b.legwear) if lvl < 3 else ""),
        (1, print_clause(a, b.prints, "bottom")),
    ]
    return fit_clauses(clauses, cap)


def shoe_phrase(a: BuildArgs, c: Character) -> str:
    try:
        phrase = a.ctx.inventory.shoe(c.bottom.shoes.style_id).prompt_phrase.strip()
    except KitError as exc:
        raise PromptBuildError(str(exc)) from exc
    if not phrase:
        raise PromptBuildError(f"shoe style {c.bottom.shoes.style_id!r} has no prompt_phrase")
    return phrase


def accessory_phrase(a: BuildArgs, c: Character, path: str) -> str:
    limit = 1 if a.level >= 3 else 3
    cap = 8 if a.level >= 3 else 15
    out = []
    for i, acc in enumerate(c.accessories[:limit]):
        desc = ft(a, acc.description, 15, f"{path}/accessories/{i}/description")
        if cap < 15:
            desc = fit_clauses([(0, desc)], cap)
        out.append(f"{desc} on the {a.phr.get('attachment_phrase', acc.attachment)}")
    return "; ".join(out)


def face_phrase(a: BuildArgs, c: Character) -> str:
    f = c.face
    eye = a.ctx.inventory.eye_shapes.get(f.eye_shape)
    eye_p = (eye.prompt_phrase if eye and eye.prompt_phrase else a.phr.get("face", "eye_phrase", f.eye_shape))
    return f"{eye_p} with {a.phr.get('face', 'iris_phrase', f.iris_style)}, {a.phr.get('face', 'mouth_phrase', f.mouth_style)}"


def character_colour_names(a: BuildArgs, key: CharKey) -> str:
    hexes = [h for h in (role_hex(a.spec, f"{key}_main"), role_hex(a.spec, f"{key}_second"), role_hex(a.spec, f"hair_{key}")) if h]
    return join_names(a.ctx.names_for(hexes, 3)) if hexes else ""


def presentation_style(a: BuildArgs, c: Character) -> str:
    return a.phr.get("presentation_style", c.presentation)


# ------------------------------------------------------------------------------------------------ I1 family
def _concept_slots(a: BuildArgs, key: CharKey) -> dict[str, str]:
    c = a.spec.a if key == "a" else a.spec.b
    local = BuildArgs(a.spec, key, a.ctx, a.inputs, a.level, a.meta)
    p = f"/{key}"
    return {
        "presentation_style": presentation_style(local, c),
        "hair_phrase": hair_phrase(local, c, p, 16),
        "top_phrase": top_phrase(local, c, p, 20),
        "bottom_phrase": bottom_phrase(local, c, p, 16),
        "shoe_phrase": shoe_phrase(local, c),
        "accessory_phrase": accessory_phrase(local, c, p),
        "colour_names": character_colour_names(local, key),
        "face_phrase": face_phrase(local, c),
    }


def build_i1(a: BuildArgs) -> Built:
    """I1, I1e, I1f, I1b, I1p (one character; ``fix_sentence`` is the L7 sentence for I1e)."""
    slots = _concept_slots(a, a.char)    # type: ignore[arg-type]
    fix = a.input("fix_sentence", "")
    slots["fix_sentence"] = ft(a, fix, a.phr.number("free_text", "fix_sentence_max_words"), "fix_sentence") if fix else ""
    return Built(slots=slots)


def build_i1j(a: BuildArgs) -> Built:
    out: dict[str, str] = {}
    for key in ("a", "b"):
        s = _concept_slots(a, key)
        out[f"presentation_style_{key}"] = s["presentation_style"]
        parts = [s["hair_phrase"], s["top_phrase"], s["bottom_phrase"], s["shoe_phrase"]]
        if s["accessory_phrase"]:
            parts.append(s["accessory_phrase"])
        out[f"phrases_{key}"] = "; ".join(parts)
        out[f"colour_names_{key}"] = s["colour_names"]
        out[f"face_phrase_{key}"] = s["face_phrase"]
    return Built(slots=out)


# ------------------------------------------------------------------------------------------------ prints
_TALL_REGIONS = ("torso_l", "torso_r", "rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "llimb_f", "llimb_b", "llimb_l", "llimb_r")


def _print_source(a: BuildArgs):
    me = a.me
    if a.input("accessory") is not None:
        # the Recraft rung of a sticker-slab badge (R2_badge): the artwork is the accessory itself, in the accessory's own colours
        idx, acc = _accessory(a)
        return None, acc.description, f"/accessories/{idx}/description", list(acc.colour_refs)
    ref = str(a.input("print", "top.0"))
    if ref == "shoes":
        return None, me.bottom.shoes.motif, "/bottom/shoes/motif", [me.bottom.shoes.accent_ref, me.bottom.shoes.base_ref]
    part, _, idx = ref.partition(".")
    prints = me.top.prints if part == "top" else me.bottom.prints if part == "bottom" else None
    if prints is None or not idx.isdigit() or int(idx) >= len(prints):
        raise PromptBuildError(f"{a.meta.id}: print {ref!r} does not exist on character {a.char}")
    p = prints[int(idx)]
    return p, p.motif, f"/{part}/prints/{idx}/motif", list(p.colour_refs)


def build_i2(a: BuildArgs) -> Built:
    p, motif, mpath, refs = _print_source(a)
    cap = 15 if a.input("accessory") is not None else 6 if p is None else 12      # a shoe motif is 6 words, a print 12, an accessory description 15
    tall = bool(p is not None and p.region in _TALL_REGIONS)
    aspect = a.input("aspect") or ("tall" if tall else "square")
    if aspect not in ("square", "tall"):
        raise PromptBuildError("aspect must be square or tall")
    return Built(slots={"motif": ft(a, motif, cap, f"/{a.char}{mpath}", allow_empty=False), "colour_names": names(a, refs, 3),
                        "area": "tall 1:2" if aspect == "tall" else "square"},
                 size=a.phr.get("sizes", aspect))


def build_r2(a: BuildArgs) -> Built:
    b = build_i2(a)
    line = a.spec.palette
    outline = next((c.hex for c in line if c.role == "line"), None)
    b.slots["outline_colour"] = join_names(a.ctx.names_for([outline], 1)) if outline else "dark"
    sentinel = _sentinel(a, [palette_hex(a.spec, r) for r in _print_source(a)[3]])
    b.slots["bg"] = sentinel[1]
    b.provider_fields = _recraft_fields(a, [palette_hex(a.spec, r) for r in _print_source(a)[3]], sentinel[0])
    b.provider_fields["size"] = "1024x1024" if b.slots["area"] == "square" else "768x1536"
    b.size = b.provider_fields["size"]
    return b


# ------------------------------------------------------------------------------------------------ face parts
FACE_PARTS = ("iris", "lash_upper", "brow", "mouth_closed", "mouth_open", "closed_lid_line")


def _sentinel(a: BuildArgs, hexes: list[str]) -> tuple[str, str]:
    from duoskin.imaging.palette import choose_sentinel

    chosen = choose_sentinel(hexes) if hexes else None
    chosen = (chosen or "#00FF00").upper()
    return chosen, a.phr.get("r1", "sentinel_names", chosen)


def _recraft_fields(a: BuildArgs, hexes: list[str], sentinel_hex: str) -> dict[str, Any]:
    from duoskin.imaging.palette import hex_to_rgb

    return {"controls": {"colors": [{"rgb": list(hex_to_rgb(h))} for h in hexes],
                         "background_color": {"rgb": list(hex_to_rgb(sentinel_hex))}},
            "sentinel_hex": sentinel_hex, "style_mode": not bool(a.input("bootstrap_style", False))}


def _face_part_values(a: BuildArgs, part: str) -> dict[str, str]:
    """Clause values shared by R1 and I3 (bible §11.1 'Slot values')."""
    f = a.me.face
    r1 = a.phr.section("r1")
    v: dict[str, str] = {}
    iris_c, iris_d, pupil_c = one_name(a, f.iris_ref), one_name(a, f.iris_dark_ref), one_name(a, f.pupil_ref)
    v.update(iris_colour=iris_c, iris_dark=iris_d, pupil_colour=pupil_c,
             iris_shape=r1["iris_shape"][f.iris_style], pupil_shape=r1["pupil_shape"][f.iris_style],
             iris_style_clause=r1["iris_style_clause"][f.iris_style].format(iris_dark=iris_d, iris_colour=iris_c),
             flick_clause=r1["flick_clause"][f.lash_style], lid_flick_clause=r1["lid_flick_clause"],
             brow_clause=r1["brow_clause"][f.brow_style], brow_end=r1["brow_end"][f.brow_style],
             mouth_clause=r1["mouth_clause"][f.mouth_style],
             lash_colour=one_name(a, f.lash_ref), brow_colour=one_name(a, f.brow_ref),
             mouth_line_colour=one_name(a, f.mouth_line_ref), mouth_inner_colour=one_name(a, f.mouth_inner_ref),
             tongue_colour=one_name(a, f.tongue_ref), teeth_colour=one_name(a, f.teeth_ref))
    fill = f", filled with {v['mouth_inner_colour']}" if f.mouth_style in r1["fill_styles"] else ""
    v["fill_clause"] = fill
    shape = r1["open_shape"].get(f.mouth_style, r1["open_shape"]["default"])
    v["open_shape"] = shape
    v["teeth_clause"] = r1["teeth_clause"].format(teeth_colour=v["teeth_colour"]) if f.teeth_ref != "none" else ""
    v["part_noun"] = r1["part_noun"][part]
    v["orientation_rule"] = r1["orientation_rule"] if part in ("lash_upper", "brow") else ""
    return v


def _part_refs(a: BuildArgs, part: str) -> list[str]:
    f = a.me.face
    return {"iris": [f.iris_ref, f.iris_dark_ref, f.pupil_ref], "lash_upper": [f.lash_ref], "closed_lid_line": [f.lash_ref],
            "brow": [f.brow_ref], "mouth_closed": [f.mouth_line_ref, f.mouth_inner_ref],
            "mouth_open": [f.mouth_inner_ref, f.tongue_ref, f.teeth_ref]}[part]


def build_r1(a: BuildArgs) -> Built:
    part = str(a.input("part"))
    if part not in FACE_PARTS:
        raise PromptBuildError(f"R1: unknown part {part!r}")
    v = _face_part_values(a, part)
    refs = [r for r in _part_refs(a, part) if r != "none"]
    if part == "mouth_closed" and a.me.face.mouth_style not in a.phr.section("r1", "fill_styles"):
        refs = refs[:1]
    hexes = [palette_hex(a.spec, r) for r in refs]
    sentinel_hex, sentinel_name = _sentinel(a, hexes)
    v["bg"] = sentinel_name
    v["bootstrap_prefix"] = a.phr.get("r1", "bootstrap_prefix") if a.input("bootstrap_style", False) else ""
    fields = _recraft_fields(a, hexes, sentinel_hex)
    fields["size"] = a.phr.get("r1", "size", part)
    fields["part"] = part
    return Built(slots=v, provider_fields=fields, size=fields["size"])


def build_i3(a: BuildArgs) -> Built:
    part = str(a.input("part"))
    if part not in FACE_PARTS or part == "closed_lid_line":
        raise PromptBuildError(f"I3: unknown part {part!r}")
    v = _face_part_values(a, part)
    i3 = a.phr.section("i3")
    f = a.me.face
    if part == "iris":
        v["part_phrase"] = f"a {v['iris_shape']} eye iris{v['iris_style_clause']}"
        v["colour_rule"] = i3["colour_rule_iris"].format(colours=names(a, [f.iris_ref, f.iris_dark_ref, f.pupil_ref], 3))
    elif part == "lash_upper":
        v["part_phrase"] = f"an upper eyelash line: {lower_first(v['flick_clause'])}"
        v["colour_rule"] = i3["colour_rule_lines"].format(colour=v["lash_colour"])
    elif part == "brow":
        v["part_phrase"] = f"an eyebrow: {lower_first(v['brow_clause'])}, a thick rounded end at the left and {v['brow_end']} at the right"
        v["colour_rule"] = i3["colour_rule_lines"].format(colour=v["brow_colour"])
    elif part == "mouth_closed":
        v["part_phrase"] = f"a small cartoon mouth: {lower_first(v['mouth_clause'])}{v['fill_clause']}"
        v["colour_rule"] = i3["colour_rule_lines"].format(colour=v["mouth_line_colour"])
    else:
        teeth = f" and {lower_first(v['teeth_clause'])}" if v["teeth_clause"] else ""
        v["part_phrase"] = (f"an open cartoon mouth: a {v['open_shape']} filled with {v['mouth_inner_colour']}, "
                            f"with a small {v['tongue_colour']} tongue{teeth}")
        v["colour_rule"] = i3["colour_rule_mouth_open"].format(inner=v["mouth_inner_colour"], tongue=v["tongue_colour"])
    drop = set(i3["exclude_drop"].get(part, ()))
    v["exclude_list"] = ", ".join(w for w in i3["exclude_base"] if w not in drop)
    return Built(slots=v, size=a.phr.get("sizes", "square"))


# ------------------------------------------------------------------------------------------------ hair
def build_i4(a: BuildArgs) -> Built:
    c = a.me
    style = a.ctx.inventory.hair_style(c.hair.kit_style_id)
    k = style.clump_k if style else a.phr.number("hair", "custom_clump_k")
    parting = style.parting if style else c.hair.parting
    h = a.phr.section("hair")
    if parting in ("left", "right"):
        key = "parting_side_s0" if a.input("bootstrap") else "parting_side"
        part_clause = h[key].format(side=parting)
    else:
        part_clause = h["parting_symmetric"]
    return Built(slots={"hair_phrase": hair_phrase(a, c, f"/{a.char}", 16), "parting_clause": part_clause, "k": str(k),
                        "fringe_line": h["bangs_line"] if fringe_present(a, c) else h["bare_line"]},
                 size=a.phr.get("sizes", "hair"))


# ------------------------------------------------------------------------------------------------ accessories and badges
def _accessory(a: BuildArgs):
    idx = a.input("accessory", 0)
    accs = a.me.accessories
    if not isinstance(idx, int) or not 0 <= idx < len(accs):
        raise PromptBuildError(f"{a.meta.id}: accessory {idx!r} does not exist on character {a.char}")
    return idx, accs[idx]


def build_i5(a: BuildArgs) -> Built:
    idx, acc = _accessory(a)
    opt = a.phr.section("attachment_option")
    option = opt["keychain_charm"] if acc.kind == "keychain_charm" else opt.get(acc.attachment, "")
    return Built(slots={
        "item_noun": a.phr.get("item_noun", acc.kind),
        "accessory_description": ft(a, acc.description, 15, f"/{a.char}/accessories/{idx}/description", allow_empty=False),
        "material_phrase": a.phr.get("material_phrase", acc.material),
        "colour_names": names(a, list(acc.colour_refs), 3),
        "attachment_option": option}, size=a.phr.get("sizes", "square"))


def build_i6(a: BuildArgs) -> Built:
    idx, acc = _accessory(a)
    return Built(slots={
        "item_noun": a.phr.get("item_noun", acc.kind),
        "accessory_description": ft(a, acc.description, 15, f"/{a.char}/accessories/{idx}/description", allow_empty=False)},
        size=a.phr.get("sizes", "square"))


def build_i10(a: BuildArgs) -> Built:
    view = str(a.input("view"))
    if view not in ("back", "left", "right"):
        raise PromptBuildError("I10: view must be back, left or right")
    target = str(a.input("target", "hair"))
    if target == "hair":
        noun = a.phr.get("object_noun", "hair")
    else:
        _, _, idx = target.partition(":")
        accs = a.me.accessories
        if not idx.isdigit() or int(idx) >= len(accs):
            raise PromptBuildError(f"I10: accessory {target!r} does not exist")
        noun = a.phr.get("object_noun", accs[int(idx)].kind)
    return Built(slots={"object_noun": noun, "view_phrase": a.phr.get("own_view_phrase", view),
                        "direction_rule": a.phr.get("direction_rule", view)}, size=a.phr.get("sizes", "square"))


def build_t2(a: BuildArgs) -> Built:
    view = str(a.input("view"))
    if view not in ("back", "left", "right"):
        raise PromptBuildError("T2: view must be back, left or right")
    target = str(a.input("target", "hair"))
    if target == "hair":
        noun = a.phr.get("object_noun", "hair")
    else:
        _, _, idx = target.partition(":")
        accs = a.me.accessories
        if not idx.isdigit() or int(idx) >= len(accs):
            raise PromptBuildError(f"T2: accessory {target!r} does not exist")
        noun = a.phr.get("object_noun", accs[int(idx)].kind)
    fix = ft(a, a.input("fix_sentence", ""), a.phr.number("free_text", "fix_sentence_max_words"), "fix_sentence", allow_empty=False)
    return Built(slots={"object_noun": noun, "view_phrase": a.phr.get("view_phrase", view), "fix_sentence": fix},
                 provider_fields={"view": view})


# ------------------------------------------------------------------------------------------------ library assets
def build_i7(a: BuildArgs) -> Built:
    try:
        fab = a.ctx.inventory.fabric(str(a.input("fabric_id")))
    except KitError as exc:
        raise PromptBuildError(str(exc)) from exc
    if not fab.fabric_phrase.strip():
        raise PromptBuildError(f"fabric {fab.id!r} has no fabric_phrase")
    return Built(slots={"fabric_phrase": fab.fabric_phrase, "pattern_phrase": fab.pattern_phrase or a.phr.get("defaults", "pattern_phrase"),
                        "k": str(fab.weave_k)}, size=a.phr.get("sizes", "square"))


def build_i8(a: BuildArgs) -> Built:
    recipe = a.ctx.inventory.recipe(str(a.input("recipe_id")))
    panel = str(a.input("panel"))
    if panel not in ("torso_f", "torso_b", "torso_side", "limb_face"):
        raise PromptBuildError("I8: panel must be torso_f, torso_b, torso_side or limb_face")
    phrase = a.phr.get("panel_phrase", f"limb_face_{'shirt' if recipe.template == 'shirt' else 'pants'}" if panel == "limb_face" else panel)
    size = a.phr.get("sizes", "square" if panel in ("torso_f", "torso_b") else "tall")
    return Built(slots={"recipe_phrase": _recipe_phrase(a, recipe.id), "panel_phrase": phrase, "k": str(recipe.fold_k)}, size=size)


# ------------------------------------------------------------------------------------------------ edit, finalize, repair
def build_edit(a: BuildArgs) -> Built:
    """The global-edit variants I2e..I6e: one fix sentence, the L7 keep list and the base template's fixed rules."""
    fix = ft(a, a.input("fix_sentence", ""), a.phr.number("free_text", "fix_sentence_max_words"), "fix_sentence", allow_empty=False)
    keep = [ft(a, k, 8, "keep") for k in (a.input("keep", []) or [])]
    slots = {"fix_sentence": fix, "keep_list": "; ".join(k for k in keep if k), "orientation_rule": "", "fringe_line": ""}
    part = a.input("part")
    if part in ("lash_upper", "brow"):
        slots["orientation_rule"] = a.phr.get("r1", "orientation_rule")
    if a.meta.id.startswith("I4e") and a.char in ("a", "b"):
        h = a.phr.section("hair")
        slots["fringe_line"] = h["bangs_line"] if fringe_present(a, a.me) else h["bare_line"]
    return Built(slots=slots)


def build_i0(a: BuildArgs) -> Built:
    return Built(slots={"asset_noun": a.phr.get("asset_noun", str(a.input("asset")))})


def build_i11(a: BuildArgs) -> Built:
    edit = [ft(a, s, a.phr.number("free_text", "edit_sentence_max_words"), "edit_prompt") for s in (a.input("edit", []) or [])]
    edit = [s for s in edit if s]
    if not edit or len(edit) > a.phr.number("free_text", "edit_sentences_max"):
        raise PromptBuildError("I11: 1 to 4 edit sentences are required")
    subject = ft(a, a.input("subject_sentence", ""), 30, "subject_sentence", allow_empty=False)
    keep = [ft(a, k, 8, "keep") for k in (a.input("keep", []) or [])] + [ft(a, k, 8, "template_keep") for k in (a.input("template_keep", []) or [])]
    keep = list(dict.fromkeys(k for k in keep if k))          # the caller's keep list and the template's often name the same thing
    form = str(a.input("form", "masked"))
    if form not in ("masked", "global"):
        raise PromptBuildError("I11: form must be masked or global")
    lead = "Change only the masked area:" if form == "masked" else "Apply this change to the whole image:"
    slots = {"asset_noun": a.phr.get("asset_noun", str(a.input("asset"))), "subject_sentence": lower_first(subject.rstrip(".")),
             "edit_1": f"{lead} {edit[0]}", "keep_list": "; ".join(k for k in keep if k)}
    for i in range(2, 5):
        slots[f"edit_{i}"] = edit[i - 1] if len(edit) >= i else ""
    return Built(slots=slots)


Builder = Callable[[BuildArgs], Built]
#: name -> (builder, highest degrade level). Level 0 is the full text.
BUILDERS: dict[str, tuple[Builder, int]] = {
    "i0": (build_i0, 0), "i1": (build_i1, 3), "i1j": (build_i1j, 3), "i2": (build_i2, 0), "r2": (build_r2, 0),
    "i3": (build_i3, 0), "r1": (build_r1, 0), "i4": (build_i4, 0), "i5": (build_i5, 0), "i6": (build_i6, 0),
    "i7": (build_i7, 0), "i8": (build_i8, 0), "i10": (build_i10, 0), "i11": (build_i11, 0), "t2": (build_t2, 0),
    "edit": (build_edit, 0),
}

