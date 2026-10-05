"""Mock role builders that need the real schemas (registered lazily by ``providers.mock.llm``; nothing here runs unless
``duoskin.models.spec`` and ``duoskin.models.kitenums`` import).

``L3_planner`` / ``PlanSet``: three ``DuoSpec`` that satisfy ``models/spec_rules`` and the plan rules the spec lists for the mock:

* three different pair structures, or, when the brief names one (``same_club``, "same club", ...), that structure three times;
* exactly one wildcard, which departs from the other two in palette family and anchor kind;
* 2 anchors, at least 5 contrasts on different axes that are measurable from fields that really differ between A and B,
  faces that differ in at least 3 fields, at least 2 differing character DNA fields, different top recipes, and
  (when the kit hair list has one style only, as in demo mode) two ``hair_custom`` hairs with a ``hair_shape`` contrast;
* kit ids drawn from the live inventory (``kitenums.current_inventory()``), garment attributes chosen from each recipe's ``cut``.

The result is deterministic from the brief and the call's seed. The C1 plan linter itself belongs to another track; this
builder aims at its documented rules and is checked here against ``spec_rules.plan_set_problems`` only.
"""
from __future__ import annotations

import colorsys
import random
import re
from typing import Any

from duoskin.providers.mock import _draw as D

FAMILY_HUES: dict[str, tuple[float, float, float]] = {       # base hue (0..1), saturation, value
    "warm_pastel": (0.04, 0.35, 0.96), "cool_pastel": (0.55, 0.35, 0.95), "warm_bright": (0.07, 0.85, 0.95),
    "cool_bright": (0.58, 0.80, 0.90), "earthy_natural": (0.09, 0.45, 0.65), "muted_vintage": (0.12, 0.30, 0.70),
    "jewel_tones": (0.75, 0.70, 0.60), "candy_bright": (0.92, 0.65, 0.98), "neon_night": (0.45, 0.90, 0.95),
    "monochrome_accent": (0.60, 0.15, 0.60),
}
ROLE_ORDER = ("a_main", "a_second", "b_main", "b_second", "accent", "neutral_light", "neutral_dark", "hair_a", "hair_b", "modesty")
STRUCTURES = ("complement", "leader_chaotic", "mirror", "seasonal_twins", "same_club", "object_mascot")
THEMES = ("lantern festival night market", "seaside bakery morning", "mountain train journey", "paper garden workshop", "arcade after rain")
OBJECTS = ("paper lantern", "tiny umbrella", "brass key", "folded kite", "glass jar")
ACCESSORY_SETS = (
    ("plush_pet", "shoulder", "right_shoulder", "tripo", "plush"), ("keychain_charm", "waist", "waist_front", "tripo", "enamel_flat"),
    ("hair_clip_slab", "hair", "hair", "sticker_slab", "enamel_flat"), ("small_hat", "hat", "hat", "tripo", "knit"),
    ("bag", "back", "body_back", "tripo", "canvas"), ("sticker_slab", "face", "face_front", "sticker_slab", "vinyl"),
)
ANCHOR_KINDS = ("colour", "motif", "trim", "accessory_pair", "material", "silhouette_detail")


def register_mock_roles(register) -> None:
    """Called by ``providers.mock.llm`` the first time a mock call needs the role registry."""
    try:
        from duoskin.models import kitenums, spec  # noqa: F401
    except ImportError:
        return
    register("L3_planner", planner_builder)
    register("PlanSet", planner_builder)


# --------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------

def _hex(c: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


def _name_of(c: tuple[int, int, int]) -> str:
    best = min(D.COLOR_WORDS.items(), key=lambda kv: sum((a - b) ** 2 for a, b in zip(kv[1], c, strict=True)))
    return best[0]


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def _palette(family: str, brief_colors: list[tuple[int, int, int]], rng: random.Random) -> list[dict[str, str]]:
    h, s, v = FAMILY_HUES[family]
    shift = rng.random() * 0.03
    cols: dict[str, tuple[int, int, int]] = {
        "a_main": _hsv(h + shift, s, v), "a_second": _hsv(h + 0.08, s * 0.8, v), "b_main": _hsv(h + 0.5, s, v),
        "b_second": _hsv(h + 0.58, s * 0.8, v), "accent": _hsv(h + 0.17, min(1.0, s + 0.2), min(1.0, v + 0.05)),
        "neutral_light": _hsv(h, 0.05, 0.97), "neutral_dark": _hsv(h, 0.35, 0.20), "hair_a": _hsv(h + 0.02, 0.5, 0.30),
        "hair_b": _hsv(h + 0.3, 0.45, 0.35), "modesty": _hsv(h + 0.45, 0.25, 0.32),
    }
    for role, c in zip(("a_main", "b_main", "accent"), brief_colors, strict=False):
        cols[role] = c
    return [{"id": f"p{i + 1}", "name": _name_of(cols[r]), "hex": _hex(cols[r]), "role": r} for i, r in enumerate(ROLE_ORDER)]


def _pick(options: list[str] | tuple[str, ...], idx: int) -> str:
    return options[idx % len(options)]


def _cut(inv: Any, recipe_id: str, attr: str, preferred: tuple[str, ...], idx: int) -> str:
    """A value for ``attr`` that the recipe can draw (its ``cut`` list), preferring ``preferred`` in the given rotation."""
    allowed = tuple(inv.recipe(recipe_id).cut.get(attr, ())) or preferred
    pool = [v for v in preferred if v in allowed] or list(allowed)
    return pool[idx % len(pool)]


def _words(text: str, cap: int) -> str:
    return " ".join(text.split()[:cap])


def _combo(brief: str) -> str:
    low = brief.lower()
    for c in ("bb", "gg", "bg", "gb"):
        if re.search(rf"\b{c}\b", low):
            return c
    if "two girls" in low or "girl and girl" in low:
        return "gg"
    if "two boys" in low or "boy and boy" in low:
        return "bb"
    if "girl and boy" in low:
        return "gb"
    return "bg"


def _named_structure(brief: str) -> str | None:
    low = brief.lower().replace("_", " ")
    for s in STRUCTURES:
        if s.replace("_", " ") in low:
            return s
    return None


# --------------------------------------------------------------------------------------------------------------
# One character and one spec
# --------------------------------------------------------------------------------------------------------------

def _character(inv: Any, k: int, plan: int, who: str, pal: dict[str, str], combo_letter: str, rng: random.Random, theme_obj: str) -> dict[str, Any]:
    """``k`` is 0 for character a and 1 for b; everything that must differ between A and B is rotated by ``k``."""
    off = plan + 2 * k
    tops = list(inv.ids("TopRecipeKit"))
    tops = [t for t in tops if inv.recipe(t).family not in ("crop_top",)] or tops
    bottoms = list(inv.ids("BottomRecipeKit"))
    top_id = tops[(plan + 3 * k) % len(tops)] if k == 0 else tops[(plan + 3 * k + 1) % len(tops)]
    bottom_id = bottoms[(plan + 2 * k) % len(bottoms)]
    fabrics = list(inv.ids("FabricKit"))
    shoes = list(inv.ids("ShoeKit"))
    main, second = (pal["a_main"], pal["a_second"]) if k == 0 else (pal["b_main"], pal["b_second"])
    hair_ref = pal["hair_a"] if k == 0 else pal["hair_b"]
    sleeve = _cut(inv, top_id, "sleeve", ("short", "long", "three_quarter", "none"), k)
    layout = _cut(inv, top_id, "block_layout", ("solid", "contrast_sleeves", "horizontal_band", "vertical_split"), plan + k)
    neckline = _cut(inv, top_id, "neckline", ("crew", "v_neck", "collar", "square", "hood"), k + plan)
    leg = _cut(inv, bottom_id, "leg", ("full", "knee", "above_knee", "mini", "midi"), k)
    acc = ACCESSORY_SETS[(plan + 2 * k) % len(ACCESSORY_SETS)]
    dna_shapes = ("round_soft", "sharp_angular", "boxy_sturdy", "flowing_curved", "spiky_energetic", "geometric_clean")
    plans = ("ratio_60_30_10", "ratio_70_20_10", "block_50_50", "mono_accent", "allover_pattern")
    focal = ("chest", "back_print", "hair", "accessory", "shoes", "face")
    presentation = "boy" if combo_letter == "b" else "girl"
    hair_kits = list(inv.ids("HairKit"))
    hair_kit = hair_kits[k % len(hair_kits)]
    skin = list(inv.ids("SkinToneKit"))
    eyes, mouths = list(inv.ids("EyeShapeKit")), list(inv.ids("MouthKit"))
    return {
        "presentation": presentation,
        "role_in_duo": "the steady one" if k == 0 else "the lively one",
        "dna": {"shape_language": _pick(dna_shapes, off + k), "colour_plan": _pick(plans, off + 2 * k), "focal_location": _pick(focal, off + 3 * k),
                "motif_object": theme_obj if k == 0 else _pick(OBJECTS, plan + 1), "accessory_style": "small and tidy" if k == 0 else "big and playful",
                "energy": "calm" if k == 0 else "bouncy"},
        "body": {"skin_tone": skin[(plan + k) % len(skin)], "modesty_ref": pal["modesty"]},
        "face": {
            "eye_shape": eyes[(plan + k) % len(eyes)],
            "iris_style": _pick(("oval_solid", "oval_top_band", "oval_two_step", "oval_ring", "round_small_pupil", "vertical_slit"), off),
            "highlight_style": _pick(("dual_dot", "single_large", "sparkle_star", "triple_dot", "crescent_rim"), off + k),
            "lash_style": _pick(("clean_line", "outer_flick_1", "outer_flicks_3", "wing"), off + 1),
            "brow_style": _pick(("thin_arched", "straight_thick", "short_round", "angled_up", "soft_worried"), off + 2 * k),
            "mouth_style": mouths[(plan + 2 * k) % len(mouths)], "nose_style": _pick(("none", "dot", "tiny_hook"), k + plan),
            "cheek_mark": "blush_soft" if k == 0 else "none",
            "default_expression": _pick(("soft_smile", "smug", "cheerful", "determined", "sleepy"), off),
            "iris_ref": pal["accent"] if k == 0 else pal["b_second"], "iris_dark_ref": pal["neutral_dark"], "pupil_ref": pal["neutral_dark"],
            "sclera_ref": pal["neutral_light"], "lash_ref": pal["neutral_dark"], "brow_ref": hair_ref, "mouth_line_ref": pal["neutral_dark"],
            "mouth_inner_ref": pal["neutral_dark"], "tongue_ref": pal["accent"], "teeth_ref": "none",
            "blush_ref": pal["accent"] if k == 0 else "none",
        },
        "hair": {"kit_style_id": hair_kit, "fringe_id": "kit_default", "back_id": "kit_default", "parting": "left" if k == 0 else "right",
                 "description": "short tidy bob with side fringe" if k == 0 else "tall spiky crop with loose back",
                 "colour_ref": hair_ref, "shadow_ref": pal["neutral_dark"], "highlight_ref": pal["accent"] if k == 1 else "none"},
        "top": {"recipe_id": top_id, "sleeve": sleeve,
                "hem": _cut(inv, top_id, "hem", ("hip_untucked", "waist_tucked", "crop"), k), "neckline": neckline,
                "front": "closed", "block_layout": layout, "inner_recipe_id": "none", "fabric_id": fabrics[(plan + 2 * k) % len(fabrics)],
                "base_ref": main, "second_ref": second, "trim_ref": pal["accent"],
                "prints": [{"motif": f"{_words(theme_obj, 4)} cluster", "region": "torso_f", "scale": "medium", "colour_refs": [pal["accent"], pal["neutral_light"]]}],
                "arm_extras": ["bracelet_char_right"] if k == 1 else []},
        "bottom": {"recipe_id": bottom_id, "leg": leg, "waist": "high" if "crop" in top_id else "mid", "fabric_id": fabrics[(plan + 2 * k + 1) % len(fabrics)],
                   "base_ref": second if k == 0 else pal["neutral_dark"], "second_ref": "none", "trim_ref": "none", "prints": [],
                   "legwear": "socks_crew" if k == 0 else "bare", "legwear_ref": pal["neutral_light"] if k == 0 else "none",
                   "shoes": {"style_id": shoes[(plan + k) % len(shoes)], "base_ref": pal["neutral_dark"], "sole_ref": pal["neutral_light"],
                             "accent_ref": pal["accent"], "motif": ""}},
        "accessories": [{"kind": acc[0], "description": f"{acc[0].replace('_', ' ')} in the {'main' if k == 0 else 'second'} colours, no text",
                         "category": acc[1], "attachment": acc[2], "size_class": "small", "build": acc[3], "material": acc[4],
                         "linked_to_partner": plan == 0, "colour_refs": [main, pal["accent"]]}],
        "makeup": {"kind": "none", "description": "", "colour_refs": []},
    }


def _spec(inv: Any, plan: int, structure: str, family: str, wildcard: bool, combo: str, brief: str, brief_colors: list, rng: random.Random) -> dict[str, Any]:
    palette = _palette(family, brief_colors, rng)
    pal = {p["role"]: p["id"] for p in palette}
    theme = _words(THEMES[(plan + (2 if wildcard else 0)) % len(THEMES)], 8)
    obj = OBJECTS[plan % len(OBJECTS)]
    a = _character(inv, 0, plan, "a", pal, combo[0], rng, obj)
    b = _character(inv, 1, plan, "b", pal, combo[1], rng, obj)
    if a["top"]["recipe_id"] == b["top"]["recipe_id"]:
        tops = [t for t in inv.ids("TopRecipeKit") if t != a["top"]["recipe_id"] and inv.recipe(t).family != "crop_top"]
        if tops:
            b["top"]["recipe_id"] = tops[0]
            for attr, pref in (("sleeve", ("long", "short")), ("neckline", ("crew", "v_neck")), ("block_layout", ("solid", "horizontal_band")), ("hem", ("hip_untucked",))):
                b["top"][attr] = _cut(inv, tops[0], attr, pref, 1)
    contrasts = [("top_type", a["top"]["recipe_id"], b["top"]["recipe_id"]), ("sleeve_length", a["top"]["sleeve"], b["top"]["sleeve"]),
                 ("neckline", a["top"]["neckline"], b["top"]["neckline"]), ("leg_length", a["bottom"]["leg"], b["bottom"]["leg"]),
                 ("face_mouth", a["face"]["mouth_style"], b["face"]["mouth_style"]), ("expression", a["face"]["default_expression"], b["face"]["default_expression"]),
                 ("shape_language", a["dna"]["shape_language"], b["dna"]["shape_language"]), ("accessory_kind", a["accessories"][0]["kind"], b["accessories"][0]["kind"])]
    if a["hair"]["kit_style_id"] == b["hair"]["kit_style_id"]:
        contrasts.append(("hair_shape", "short bob", "tall spiky crop"))
    seen: set[str] = set()
    contrast_rows = []
    for axis, av, bv in contrasts:
        if av != bv and axis not in seen:
            seen.add(axis)
            contrast_rows.append({"axis": axis, "a_value": _words(str(av).replace("_", " "), 6), "b_value": _words(str(bv).replace("_", " "), 6)})
    k0 = (plan + (3 if wildcard else 0)) % len(ANCHOR_KINDS)
    anchors = [
        {"kind": ANCHOR_KINDS[k0], "description": f"matching {_words(obj, 3)} detail", "on_a": "on the chest", "on_b": "on the shoulder strap", "visible_from": "front"},
        {"kind": ANCHOR_KINDS[(k0 + 2) % len(ANCHOR_KINDS)], "description": "a shared accent colour", "on_a": "shoe soles", "on_b": "hair tie", "visible_from": "both"},
    ]
    return {
        "combo": combo, "lead": "a" if plan % 2 == 0 else "b", "is_wildcard": wildcard,
        "world": {"theme": theme, "pair_structure": structure, "structure_note": "", "story": f"{_words(theme, 6)}; two friends in the same world",
                  "palette_family": family, "material_family": ("jersey", "twill", "knit")[plan % 3], "detail_level": "standard"},
        "shared_anchors": anchors, "contrasts": contrast_rows, "palette": palette, "a": a, "b": b,
    }


# --------------------------------------------------------------------------------------------------------------
# The planner role
# --------------------------------------------------------------------------------------------------------------

def planner_builder(call: Any) -> dict[str, Any]:
    """``PlanSet`` for the L3 route: see the module docstring."""
    from duoskin.models import kitenums
    from duoskin.models.spec import PlanSet
    from duoskin.models.spec_rules import plan_set_problems

    inv = kitenums.current_inventory()
    rng: random.Random = call.rng
    brief = call.brief
    combo = _combo(brief)
    named = _named_structure(brief)
    structures = [named] * 3 if named else rng.sample(STRUCTURES, 3)
    families = rng.sample(list(FAMILY_HUES), 3)
    wildcard = rng.randrange(3)
    brief_colors = D.colors_from_text(brief, 3)
    specs = [_spec(inv, i, structures[i], families[i], i == wildcard, combo, brief, brief_colors if i != wildcard else [], rng) for i in range(3)]
    must = re.search(r"<must_include>(.*?)</must_include>", call.content_text, re.S)
    lines = [ln.strip(" -*\t") for ln in (must.group(1) if must else "").splitlines() if ln.strip(" -*\t")]
    constraints = [{"text": _words(ln, 12), "spec_paths": ["/a/accessories/0"]} for ln in lines]
    out = {"specs": specs, "brief_constraints": constraints,
           "how_they_differ": _words(f"Plan {wildcard + 1} is the wildcard with another palette family and anchor kind; the others differ in pair structure, "
                                     "garments and accessories.", 40)}
    PlanSet.model_validate(out)                   # fail loudly here (not in the pipeline) if a rule changed under this builder
    assert not plan_set_problems(out)
    return out
