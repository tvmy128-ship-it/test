"""``DuoSpec``: the LLM-facing design spec (APP_SPEC §6.2, PROMPT_BIBLE §3.2 v1.3).

The planner returns a ``PlanSet`` through structured outputs, so these models follow bible §2.8 and §3.1:

* no ``Optional``, no unions, no defaults, no ``dict``, no recursion (``tests/spec`` checks the generated schemas);
* lower-case snake_case enums, lower-cased by a ``BeforeValidator`` (``E``);
* every field has a ``Field(description=...)`` because descriptions are part of the prompt. Where the bible gives a description it
  is copied verbatim; the other fields get a short plain one;
* kit enums (``K.HairKit``, ...) are validated against the live kit inventory (``models/kitenums.py``), so these classes are
  never rebuilt when a kit is added;
* word caps, hex patterns, palette references, counts and prints per garment are enforced by ``models/spec_rules.py`` from a model
  validator. ``model_validate(data, context={"skip_rules": True})`` skips them (the linter parses that way to report every
  problem at once).

Code-owned constants that are not model fields (``schema_version``, ``text_policy="no_text"``, ``spec_id``, ``parent_spec_id``,
``dna_card_version``, ``palette_source``) live on ``SpecRecord`` (``models/spec_record.py``).
"""
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, ValidationInfo, model_validator

from duoskin.models import spec_rules
from duoskin.models.common import Strict
from duoskin.models.kitenums import K


def E(*values: str) -> Any:
    """Closed choice. Values are lowercase snake_case; input is lowercased first because structured outputs do not guarantee
    enum capitalisation."""
    return Annotated[Literal.__getitem__(values), BeforeValidator(lambda v: v.strip().lower() if isinstance(v, str) else v)]


# ---- static enums ----------------------------------------------------------------------------------------------------
Combo = E("bb", "gg", "bg", "gb")
PairStructure = E("complement", "leader_chaotic", "same_club", "mirror", "seasonal_twins", "object_mascot", "other")
PaletteFamily = E("warm_pastel", "cool_pastel", "warm_bright", "cool_bright", "earthy_natural", "muted_vintage",
                  "jewel_tones", "candy_bright", "neon_night", "monochrome_accent")
DetailLevel = E("minimal", "standard", "maximal")
MaterialFamily = E("jersey", "twill", "denim", "fleece", "nylon", "knit", "canvas", "corduroy")
ShapeLanguage = E("round_soft", "sharp_angular", "boxy_sturdy", "flowing_curved", "spiky_energetic", "geometric_clean")
ColourPlan = E("ratio_60_30_10", "ratio_70_20_10", "block_50_50", "mono_accent", "allover_pattern")
FocalLocation = E("chest", "back_print", "hair", "accessory", "shoes", "face")
Region = E("torso_u", "torso_f", "torso_b", "torso_l", "torso_r", "torso_d",
           "rlimb_u", "rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "rlimb_d",
           "llimb_u", "llimb_f", "llimb_b", "llimb_l", "llimb_r", "llimb_d")   # code maps to pixel boxes (guide_regions.json)

PAIR_STRUCTURES = ("complement", "leader_chaotic", "same_club", "mirror", "seasonal_twins", "object_mascot", "other")
CONTRAST_AXES = ("colour_temperature", "value", "hair_shape", "hair_length", "top_type", "bottom_type", "sleeve_length",
                 "leg_length", "neckline", "layering", "block_layout", "pattern_scale", "fabric", "accessory_kind",
                 "accessory_slot", "face_eyes", "face_mouth", "expression", "shape_language")
ANCHOR_KINDS = ("colour", "motif", "material", "trim", "silhouette_detail", "accessory_pair", "hair_detail", "face_detail")
COLOUR_ROLES = ("a_main", "a_second", "b_main", "b_second", "shared", "accent", "neutral_light", "neutral_dark",
                "hair_a", "hair_b", "modesty", "line")
ACCESSORY_KINDS = ("plush_pet", "keychain_charm", "bag", "small_hat", "hair_clip_slab", "sticker_slab", "prop")
ACCESSORY_CATEGORIES = ("hat", "hair", "face", "neck", "shoulder", "front", "back", "waist")
ACCESSORY_ATTACHMENTS = ("hat", "hair", "face_front", "face_center", "neck", "right_shoulder", "left_shoulder", "right_collar",
                         "left_collar", "body_front", "body_back", "waist_front", "waist_center", "waist_back")
CHARACTER_DNA_FIELDS = ("shape_language", "colour_plan", "focal_location", "motif_object", "accessory_style", "energy")


class Colour(Strict):
    id: str = Field(description="short palette id such as p1; every *_ref field points to one of these ids")
    name: str = Field(description="plain colour name, at most 3 words; code replaces it with the dictionary name")
    hex: str = Field(description="#RRGGBB")
    role: E(*COLOUR_ROLES) = Field(description="what the colour is for: a_ roles belong to character a, b_ roles to character b; "
                                               "shared, accent and the neutrals serve both; hair_a and hair_b are hair colours")


class WorldDNA(Strict):
    theme: str = Field(description="at most 8 words; the shared world, described visually")
    pair_structure: PairStructure = Field(description="how the two characters relate; <structure_profiles> lists the rules of each")
    structure_note: str = Field(description="at most 12 words when pair_structure is 'other', else empty string")
    story: str = Field(description="one line, at most 25 words; metadata only, never drawn or printed")
    palette_family: PaletteFamily = Field(description="the palette family shared by both characters")
    material_family: MaterialFamily = Field(description="the main fabric family of the world; a planner hint, fabrics come from the kit")
    detail_level: DetailLevel = Field(description="how much detail prints and accessories carry: minimal, standard or maximal")


class Anchor(Strict):
    # "colour" is the Claude report's / FAILURE_MODES' "palette" anchor kind
    kind: E(*ANCHOR_KINDS) = Field(description="what kind of thing both characters share; vary the kind between plans")
    description: str = Field(description="at most 12 words; what is shared, as a visible thing")
    on_a: str = Field(description="at most 10 words; where and how it shows on A")
    on_b: str = Field(description="at most 10 words; where and how it shows on B")
    visible_from: E("front", "both") = Field(description="front when it shows from the front only, both when it also shows from behind")


class Contrast(Strict):
    # Every axis must be measurable from spec fields (top_type = the recipe families differ, sleeve_length = top.sleeve, ...).
    # The linter also credits each differing CHARACTER DNA field (shape_language, colour_plan, focal_location, motif_object,
    # accessory_style, hair kit style) as one contrast on a code-side axis "dna_<field>" (never a duplicate of a declared axis),
    # so the >= 2 differing CHARACTER fields of PLN-DNA-01 count toward the 5 (PROPOSAL_DECISION #1).
    axis: E(*CONTRAST_AXES) = Field(description="the axis on which A and B differ; each axis once; code must be able to measure it from the spec fields")
    a_value: str = Field(description="at most 6 words")
    b_value: str = Field(description="at most 6 words")


class CharacterDNA(Strict):
    shape_language: ShapeLanguage = Field(description="the dominant shape language of this character")
    colour_plan: ColourPlan = Field(description="how colour is spread over this character's outfit")
    focal_location: FocalLocation = Field(description="where the eye should land on this character; steers print placement")
    motif_object: str = Field(description="one concrete object, at most 5 words, e.g. 'paper lantern'; no brands or characters")
    accessory_style: str = Field(description="at most 6 words, visual only")
    energy: str = Field(description="at most 3 words; metadata and default expression only, never drawn as text")


class Body(Strict):
    skin_tone: K.SkinToneKit = Field(description="a skin tone kit id; the body carries no clothing")
    modesty_ref: str = Field(description="palette id of the modesty layer colour; must differ clearly from skin")


class Face(Strict):
    eye_shape: K.EyeShapeKit = Field(description="head-base rig variant that sets the eye opening")
    iris_style: E("oval_solid", "oval_top_band", "oval_two_step", "oval_ring", "round_small_pupil", "vertical_slit") = Field(
        description="iris drawing style; always a full oval or round, highlights are added by code")
    highlight_style: E("dual_dot", "single_large", "sparkle_star", "triple_dot", "crescent_rim", "none_matte") = Field(
        description="white catchlight style drawn by code on both eyes")
    lash_style: E("clean_line", "outer_flick_1", "outer_flicks_3", "wing", "heavy_line_lower_ticks") = Field(
        description="upper lash line style, one colour")
    brow_style: E("thin_arched", "straight_thick", "short_round", "angled_up", "soft_worried") = Field(description="eyebrow style, one colour")
    mouth_style: K.MouthKit = Field(description="a mouth style that the head-base mouth rig supports")
    nose_style: E("none", "dot", "tiny_hook", "shadow_tick") = Field(description="nose style drawn by code")
    cheek_mark: E("none", "blush_soft", "blush_hatch") = Field(
        description="blush on the cheeks; freckles, marks, hearts and stars are Makeup, never on the head")
    default_expression: E("neutral", "soft_smile", "smug", "sleepy", "determined", "cheerful") = Field(
        description="render preset for thumbnails; not paint")
    iris_ref: str = Field(description="palette id of the iris colour")
    iris_dark_ref: str = Field(description="palette id of the darker iris colour")
    pupil_ref: str = Field(description="palette id of the pupil colour")
    sclera_ref: str = Field(description="palette id of the eye-white colour")
    lash_ref: str = Field(description="single colour for upper lash line, liner and lower ticks")
    brow_ref: str = Field(description="palette id of the eyebrow colour")
    mouth_line_ref: str = Field(description="palette id of the mouth line colour")
    mouth_inner_ref: str = Field(description="palette id of the inside of the open mouth")
    tongue_ref: str = Field(description="palette id of the tongue colour")
    teeth_ref: str = Field(description="palette id, or 'none' when the mouth style shows no teeth")
    blush_ref: str = Field(description="palette id, or 'none' when cheek_mark is 'none'")


class Hair(Strict):
    kit_style_id: K.HairKit = Field(description="a kit style; hair_custom only when no kit style fits (Tripo backup path)")
    fringe_id: K.FringeKit = Field(description="a fringe module, kit_default for the style's own fringe, or none for a bare forehead")
    back_id: K.BackKit = Field(description="a back module, or kit_default for the style's own back")
    parting: E("left", "right", "centre", "none") = Field(
        description="which side the parting falls on, as seen from the front; "
                    "'centre' or 'none' means a symmetric front. For a kit style, code overwrites it from the style's manifest value")
    description: str = Field(description="at most 12 words; visible shape only (length, volume, bangs)")
    colour_ref: str = Field(description="palette id of the hair colour")
    shadow_ref: str = Field(description="palette id of the hair shadow colour")
    highlight_ref: str = Field(description="palette id or 'none'")


class Print(Strict):
    motif: str = Field(description="at most 12 words; one visual motif, no words, letters or numbers")
    region: Region = Field(description="template region that carries the print")
    scale: E("small", "medium", "large") = Field(description="print size on its region")
    colour_refs: list[str] = Field(description="1 to 4 palette ids")


class Top(Strict):
    recipe_id: K.TopRecipeKit = Field(description="a shirt recipe from the kit")
    sleeve: E("none", "short", "three_quarter", "long") = Field(description="sleeve length; the recipe lists what it can draw")
    hem: E("crop", "waist_tucked", "hip_untucked") = Field(description="hem line")
    neckline: E("crew", "v_neck", "collar", "hood", "high_zip", "square") = Field(description="neckline")
    front: E("closed", "open", "layered") = Field(description="closed, or open or layered over an inner layer")
    block_layout: E("solid", "contrast_sleeves", "raglan_split", "horizontal_band", "vertical_split", "yoke") = Field(
        description="how the colour blocks are laid out")
    inner_recipe_id: K.InnerTopKit = Field(description="layer visible inside an open or layered front, else 'none'")
    fabric_id: K.FabricKit = Field(description="a fabric from the kit")
    base_ref: str = Field(description="palette id of the main garment colour")
    second_ref: str = Field(description="palette id of the second colour, or 'none'")
    trim_ref: str = Field(description="palette id of the trim colour, or 'none'")
    prints: list[Print] = Field(description="0 or 1 hero print, plus at most 1 small print")
    arm_extras: list[E("bracelet_char_right", "bracelet_char_left", "gloves", "wristband_char_right", "wristband_char_left")] = Field(
        description="bracelets, gloves or wristbands, each at most once; the character's own sides")


class Shoes(Strict):
    style_id: K.ShoeKit = Field(description="a shoe style from the kit")
    base_ref: str = Field(description="palette id of the main shoe colour")
    sole_ref: str = Field(description="palette id of the sole colour")
    accent_ref: str = Field(description="palette id of the accent colour, or 'none'")
    motif: str = Field(description="small decal motif, at most 6 words, or empty string")


class Bottom(Strict):
    recipe_id: K.BottomRecipeKit = Field(description="a pants or skirt recipe from the kit")
    leg: E("mini", "above_knee", "knee", "midi", "full") = Field(description="leg length")
    waist: E("low", "mid", "high") = Field(description="waist height; a crop top needs high")
    fabric_id: K.FabricKit = Field(description="a fabric from the kit")
    base_ref: str = Field(description="palette id of the main garment colour")
    second_ref: str = Field(description="palette id of the second colour, or 'none'")
    trim_ref: str = Field(description="palette id of the trim colour, or 'none'")
    prints: list[Print] = Field(description="0 or 1")
    legwear: E("bare", "socks_ankle", "socks_crew", "socks_knee", "tights") = Field(description="what the legs wear under the pants")
    legwear_ref: str = Field(description="palette id of the legwear colour, or 'none' when the legs are bare")
    shoes: Shoes = Field(description="the shoes")


class Accessory(Strict):
    kind: E(*ACCESSORY_KINDS) = Field(description="what the accessory is")
    description: str = Field(description="at most 15 words; visible shape and parts; no text on it")
    category: E(*ACCESSORY_CATEGORIES) = Field(
        description="Roblox accessory type: items mostly above the neck are hat or face, complete hairstyles are hair")
    attachment: E(*ACCESSORY_ATTACHMENTS) = Field(description="attachment point; it must suit the category")
    size_class: E("small", "medium", "large") = Field(description="size class; it must fit the Classic box of the category")
    build: E("tripo", "sticker_slab", "code_primitive") = Field(
        description="tripo for volumetric props, sticker_slab for flat badge items, code_primitive for rings and straps")
    material: E("plush", "vinyl", "rubber", "knit", "canvas", "enamel_flat", "wood") = Field(description="surface material")
    linked_to_partner: bool = Field(description="true when this accessory forms a linked pair with one on the partner")
    colour_refs: list[str] = Field(description="1 to 4 palette ids")


class Makeup(Strict):
    kind: E("none", "freckles", "beauty_mark", "cheek_heart", "cheek_star", "eyeshadow",
            "multicolour_lips", "multicolour_lashes", "face_paint") = Field(
        description="a separate makeup item; none while the kit inventory says makeup is unavailable")
    description: str = Field(description="at most 10 words, or empty string when kind is 'none'")
    colour_refs: list[str] = Field(description="palette ids; empty when kind is 'none'")


class Character(Strict):
    presentation: E("boy", "girl") = Field(description="boy or girl; must match the combo")
    role_in_duo: str = Field(description="at most 8 words; metadata")
    dna: CharacterDNA = Field(description="the CHARACTER design DNA; A and B differ in at least two of its fields")
    body: Body = Field(description="skin tone and modesty layer")
    face: Face = Field(description="the face, picked from the face grammar")
    hair: Hair = Field(description="the hair")
    top: Top = Field(description="the shirt")
    bottom: Bottom = Field(description="the pants or skirt, legwear and shoes")
    accessories: list[Accessory] = Field(description="0 to 2 usually; more than 2 needs a maximal detail level")
    makeup: Makeup = Field(description="makeup item")


class DuoSpec(Strict):
    combo: Combo = Field(description="bb, gg, bg or gb; the first letter is character a, the second is character b")
    lead: E("a", "b") = Field(description="which character leads the duo")
    is_wildcard: bool = Field(description="true for the one bolder plan that ignores the taste profile")
    world: WorldDNA = Field(description="the WORLD fields shared by both characters")
    shared_anchors: list[Anchor] = Field(description="2 or 3, each visible on both characters from the front")
    contrasts: list[Contrast] = Field(description="at least 5, each on a different axis; every one must be measurable "
                                                  "from spec fields (contrasts code cannot verify do not count); not mostly colour axes")
    palette: list[Colour] = Field(description="5 to 12 colours; ids unique")
    a: Character = Field(description="character a")
    b: Character = Field(description="character b")

    @model_validator(mode="after")
    def _spec_rules(self, info: ValidationInfo) -> "DuoSpec":
        if (info.context or {}).get("skip_rules"):
            return self
        problems = spec_rules.spec_problems(self)
        if problems:
            raise spec_rules.SpecRuleError(problems)
        return self


class BriefConstraint(Strict):
    text: str = Field(description="one must-include line from <must_include>, copied or lightly shortened, at most 12 words")
    spec_paths: list[str] = Field(description="1 to 3 JSON Pointers into a DuoSpec that carry this line, for example /a/accessories/0; "
                                              "each pointer must resolve in all 3 specs")


class PlanSet(Strict):
    specs: list[DuoSpec] = Field(description="exactly 3, and exactly one has is_wildcard true. When the brief does not fix the "
                                             "pair structure, the 3 use different structures. When the brief fixes it, all 3 use it, "
                                             "and the wildcard keeps that structure but departs from the other two in palette family, "
                                             "anchor kind or theme")
    brief_constraints: list[BriefConstraint] = Field(description="one entry per line in <must_include>; empty list when there are none")
    how_they_differ: str = Field(description="at most 40 words")

    @model_validator(mode="after")
    def _plan_rules(self, info: ValidationInfo) -> "PlanSet":
        if (info.context or {}).get("skip_rules"):
            return self
        text = self.how_they_differ
        if spec_rules.words(text) > spec_rules.WORD_CAPS["plan.how_they_differ"]:
            raise spec_rules.SpecRuleError([spec_rules.Problem("word_cap", "/how_they_differ",
                                                               f"{spec_rules.words(text)} words, at most "
                                                               f"{spec_rules.WORD_CAPS['plan.how_they_differ']}", "caps")])
        return self


SPEC_SCHEMAS: dict[str, type] = {"DuoSpec": DuoSpec, "PlanSet": PlanSet}


def parse_spec(data: str | bytes | dict[str, Any], *, rules: bool = True) -> DuoSpec:
    """Validate a spec from JSON text or a dict. ``rules=False`` parses without ``spec_rules`` (for the linter)."""
    ctx = None if rules else {"skip_rules": True}
    if isinstance(data, (str, bytes)):
        return DuoSpec.model_validate_json(data, context=ctx)
    return DuoSpec.model_validate(data, context=ctx)


def parse_plan_set(data: str | bytes | dict[str, Any], *, rules: bool = True) -> PlanSet:
    """Validate a ``PlanSet`` from JSON text or a dict (see :func:`parse_spec`)."""
    ctx = None if rules else {"skip_rules": True}
    if isinstance(data, (str, bytes)):
        return PlanSet.model_validate_json(data, context=ctx)
    return PlanSet.model_validate(data, context=ctx)
