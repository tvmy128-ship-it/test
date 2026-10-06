"""Role I/O schemas for the Claude routes L1, L2, L4 to L7 and L9 to L15 (APP_SPEC §6.2, §6.4; PROMPT_BIBLE §9, §10.5, §10.6,
§12.2, §16, §17).

Copied from the bible section of the same name. They follow bible §2.8: no ``Optional``, no unions, no defaults, no ``dict``,
lowercase snake_case enums (lowercased by a ``BeforeValidator``), every field has a ``Field(description=...)``, and evidence
or observation fields come **before** the verdict field. ``RevisionOp`` (L6, with ``finding``) and ``ChangeOp`` (L7, with
``reason``) are two separate classes so that ``prompts/SCHEMAS.lock`` compares like with like (APP_SPEC §6.4).

The planner's ``PlanSet`` lives in ``models/spec.py``. ``ROLE_SCHEMAS`` maps every route to its schema class.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field

from duoskin.models.common import Strict
from duoskin.models.kitenums import K
from duoskin.models.spec import E, PlanSet

_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "rules.json"


# ---- the Gate B rule library (data/rules.json, bible §7.2) ---------------------------------------------------------
@dataclass(frozen=True)
class RuleDef:
    """One yes/no rule. ``statement`` is the only text sent to the judge; ``slots`` are the ``{...}`` names in it."""

    id: str
    group: str
    statement: str
    hard: bool
    used_by: str = ""
    slots: tuple[str, ...] = ()
    note: str = ""
    hard_note: str = ""


@lru_cache(maxsize=1)
def _rules_doc() -> dict[str, Any]:
    return json.loads(_RULES_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def rule_library() -> dict[str, RuleDef]:
    """Every Gate B rule by id (stable order of the bible table)."""
    out: dict[str, RuleDef] = {}
    for r in _rules_doc()["rules"]:
        out[r["id"]] = RuleDef(id=r["id"], group=r.get("group", ""), statement=r["statement"], hard=bool(r["hard"]),
                               used_by=r.get("used_by", ""), slots=tuple(r.get("slots", ())), note=r.get("note", ""),
                               hard_note=r.get("hard_note", ""))
    return out


ALL_RULE_IDS: tuple[str, ...] = tuple(sorted(rule_library()))
RULES_VERSION: int = int(_rules_doc()["version"])

# ---- L1 reference analyst ----------------------------------------------------------------------------------------
Axis = E("line_weight", "shading", "face_style", "palette", "value_contrast", "detail_density",
         "print_scale", "accessory_scale", "hair_volume", "silhouette", "back_design", "duo_linking")


class StructureRule(Strict):
    axis: Axis = Field(description="which construction axis this rule is about")
    observation: str = Field(description="what is visible, at most 25 words")
    rule: str = Field(description="content-free construction rule, at most 25 words, reusable on an unrelated design")


class Flag(Strict):
    what: str = Field(description="what looks like a brand mark, platform logo or known character")
    where: str = Field(description="image number and position")
    confidence: E("low", "medium", "high") = Field(description="how sure you are")


class ReferenceAnalysis(Strict):
    rules: list[StructureRule] = Field(description="construction rules, each axis at most 3 times")
    duo_devices: list[str] = Field(description="how the pair is linked, each at most 15 words")
    quality_bar: list[str] = Field(description="observable quality markers to match, each at most 15 words")
    do_not_copy: list[str] = Field(description="distinctive content to avoid, each at most 12 words")
    brand_or_character_flags: list[Flag] = Field(description="anything that looks like a brand mark, platform logo or known character")


# ---- L2 taste profile --------------------------------------------------------------------------------------------
class TasteRule(Strict):
    field: E("palette_temperature", "saturation", "hair_style", "garment_recipe", "print_density",
             "accessory_kind", "face_eyes", "face_mouth", "anchor_kind", "theme_family", "pair_structure") = Field(
        description="which design field this tendency is about")
    tendency: str = Field(description="at most 15 words")
    evidence_ids: list[str] = Field(description="at least 2 ids from the tables")
    strength: E("weak", "moderate", "strong") = Field(description="how well the evidence supports the tendency")


class TasteProfile(Strict):
    likes: list[TasteRule] = Field(description="tendencies the person likes")
    dislikes: list[TasteRule] = Field(description="tendencies the person dislikes")
    open_questions: list[str] = Field(description="where the evidence is thin or split")
    explore: list[str] = Field(description="2 or 3 items, each at most 15 words")


# ---- L4 critic and L5 pairwise ranker ------------------------------------------------------------------------------
Crit = E("belong_together", "not_clones", "theme_clarity", "buildable", "thumbnail_readability", "originality",
         "restraint", "taste_fit", "back_view_interest", "structure_readable", "accessory_pair_expresses")
CRITERIA = ("belong_together", "not_clones", "theme_clarity", "buildable", "thumbnail_readability", "originality",
            "restraint", "taste_fit", "back_view_interest", "structure_readable", "accessory_pair_expresses")
RANKING_ONLY_CRITERIA = ("structure_readable", "accessory_pair_expresses")     # never pass/fail (PROPOSAL_DECISION)


class Score(Strict):
    criterion: Crit = Field(description="the criterion being scored")
    evidence: str = Field(description="one sentence citing spec fields")
    level: E("fail", "weak", "ok", "strong") = Field(description="how well the plan meets the criterion")


class Fix(Strict):
    path: str = Field(description="JSON Pointer into the spec, e.g. /b/hair/kit_style_id")
    problem: str = Field(description="what is wrong at that path")
    severity: E("low", "medium", "high") = Field(description="how much it matters")
    direction: str = Field(description="at most 20 words")


class Critique(Strict):
    scores: list[Score] = Field(description="one score per criterion, each criterion exactly once")
    fixes: list[Fix] = Field(description="every problem you see, however minor")


class CritCompare(Strict):
    criterion: Crit = Field(description="the criterion being compared")
    evidence: str = Field(description="one sentence of evidence for the comparison")
    better: E("first", "second", "tie") = Field(description="which plan is better on this criterion")


class PairJudgment(Strict):
    per_criterion: list[CritCompare] = Field(description="one comparison per criterion, each criterion exactly once")
    overall: E("first", "second", "tie") = Field(description="overall preference")


# ---- L6 reviser ----------------------------------------------------------------------------------------------------
class RevisionOp(Strict):               # L6 only; a separate class from ChangeOp (APP_SPEC §6.4)
    op: E("replace", "add", "remove") = Field(description="RFC 6902 operation")
    path: str = Field(description="RFC 6902 JSON Pointer, e.g. /a/hair/colour_ref")
    value_json: str = Field(description="JSON text of the new value; empty string for remove")
    finding: str = Field(description="the finding number this op resolves")


class Revision(Strict):
    patch: list[RevisionOp] = Field(description="the smallest patch that resolves the findings")
    note: str = Field(description="at most 40 words")


# ---- L7 change interpreter ----------------------------------------------------------------------------------------
class ImageFix(Strict):
    part_id: str = Field(description="the part this fix is for, e.g. a.hair")
    fix_sentence: str = Field(description="one sentence, at most 25 words, positive, visible result only")
    scope: E("global_edit", "local_edit", "regenerate") = Field(description="edit the whole image, edit one area, or draw again")
    region_hint: E("none", "top_left", "top", "top_right", "left", "centre", "right", "bottom_left", "bottom",
                   "bottom_right", "whole") = Field(description="where on the image the change belongs")
    keep: list[str] = Field(description="things that must stay unchanged, each at most 8 words")


class Redo(Strict):
    part_id: str = Field(description="a part that must be redone")
    reason: str = Field(description="why this part must change")


class ChangeOp(Strict):                 # L7 only: a user change has no findings, so each op gives a reason
    op: E("replace", "add", "remove") = Field(description="RFC 6902 operation")
    path: str = Field(description="RFC 6902 JSON Pointer, e.g. /a/hair/colour_ref")
    value_json: str = Field(description="JSON text of the new value; empty string for remove")
    reason: str = Field(description="why the user's request needs this op, at most 20 words")


class ChangePlan(Strict):
    understood_as: str = Field(description="at most 30 words")
    needs_clarification: str = Field(description="empty string when the request is clear")
    patch: list[ChangeOp] = Field(description="the smallest spec patch that does what the user asked")
    redo_parts: list[Redo] = Field(description="parts that must be redone")
    image_fixes: list[ImageFix] = Field(description="one concrete image fix per part tile that changes")
    duo_contract_risks: list[str] = Field(description="duo-contract or Roblox rules the change may strain")


# ---- L15 concept inventory -----------------------------------------------------------------------------------------
class InventoryItem(Strict):
    element: str = Field(description="one visible element, at most 8 words, described visually; no brand or character names")
    where: str = Field(description="front, back or both, and where on the figure, at most 8 words")
    in_spec: bool = Field(description="true when a spec field already describes this element")
    buildable: bool = Field(description="true when the kits and recipes can build it as drawn")
    suggested_spec_path: str = Field(description="JSON Pointer where it would go when in_spec is false and buildable is true, else an empty string")


class ElementList(Strict):
    items: list[InventoryItem] = Field(description="at most 20 items, most prominent first")


# ---- L9 hair kit matcher -------------------------------------------------------------------------------------------
class HairAdjust(Strict):
    param: E("volume", "fringe_length", "back_length", "side_length", "part_side", "clump_size") = Field(description="what to adjust")
    value: E("less", "same", "more", "left", "right", "centre") = Field(description="how to adjust it; part_side takes left, right or centre")


class HairMatch(Strict):
    observations: list[str] = Field(description="visible features of the approved hair, each at most 15 words")
    choice: E("c1", "c2", "c3", "c4", "c5", "none_fit") = Field(description="the candidate to start from, or none_fit")
    fringe_id: K.FringeKit = Field(description="the fringe module to use")
    back_id: K.BackKit = Field(description="the back module to use")
    adjustments: list[HairAdjust] = Field(description="discrete adjustments the chosen style supports")
    mismatch_notes: list[str] = Field(description="what the kit cannot reproduce")


# ---- L11 asset checker ---------------------------------------------------------------------------------------------
RuleId = E(*ALL_RULE_IDS)
LOCATIONS = ("none", "top_left", "top", "top_right", "left", "centre", "right", "bottom_left", "bottom", "bottom_right", "whole")


class Verdict(Strict):
    rule_id: RuleId = Field(description="the rule this verdict answers")
    observation: str = Field(description="what is visible, at most 25 words")
    verdict: E("pass", "fail", "unsure") = Field(description="pass if the statement is true, fail if false, unsure if you cannot tell")
    location: E(*LOCATIONS) = Field(description="where on the image the problem is, or none")


class AssetCheck(Strict):
    verdicts: list[Verdict] = Field(description="one verdict per listed rule, no others")


# ---- L10 repair writer ---------------------------------------------------------------------------------------------
class RepairOp(Strict):
    fixes_rule: RuleId = Field(description="the failed rule this operation fixes")
    method: E("code_palette_snap", "code_alpha_cleanup", "code_recrop", "code_stroke_normalise",
              "masked_edit", "global_edit", "simplify", "regenerate", "change_technique") = Field(description="the fix method; prefer code fixes")
    mask_id: str = Field(description="one of the listed mask ids, or 'none'")
    edit_prompt: str = Field(description="empty unless method is masked_edit, global_edit or regenerate; "
                                         "at most 4 sentences, at most 60 words, positive phrasing")
    keep: list[str] = Field(description="each at most 8 words")


class RepairPlan(Strict):
    ops: list[RepairOp] = Field(description="the repair operations")
    subject_sentence: str = Field(description="empty unless an op is masked_edit or global_edit; otherwise one sentence of at most "
                                              "30 words that describes the entire final image after the repair, "
                                              "naming the kept parts and the change (it becomes the I11 SUBJECT)")
    give_up_reason: str = Field(description="empty unless giving up")


# ---- L12 duo judge -------------------------------------------------------------------------------------------------
DuoCrit = E("cohesion", "distinctness", "anchor_visibility", "thumbnail_silhouette", "colour_harmony",
            "artifacts_and_seams", "spec_fidelity", "back_view")


class DuoCompare(Strict):
    criterion: DuoCrit = Field(description="the criterion being compared")
    evidence: str = Field(description="one sentence pointing at something visible")
    better: E("first", "second", "tie") = Field(description="which candidate is better on this criterion")


class DuoJudgment(Strict):
    per_criterion: list[DuoCompare] = Field(description="one comparison per criterion, each exactly once")
    overall: E("first", "second", "tie") = Field(description="overall preference")
    blocking_defects: list[str] = Field(description="each at most 20 words, with where it is")


class DuoLevel(Strict):
    criterion: DuoCrit = Field(description="the criterion being reviewed")
    evidence: str = Field(description="one sentence pointing at something visible")
    level: E("fail", "weak", "ok", "strong") = Field(description="how well the duo meets the criterion")


class DuoReview(Strict):
    per_criterion: list[DuoLevel] = Field(description="one level per criterion, each exactly once")
    blocking_defects: list[str] = Field(description="any defect that would stop an upload, each at most 20 words, with where it is")


# ---- L13 IP screen and L14 reference similarity ---------------------------------------------------------------------
class IpItem(Strict):
    rule_id: E("ip_no_brand", "ip_no_known_character", "ip_no_text", "ip_age_appropriate") = Field(description="the rule this answers")
    observation: str = Field(description="what you see, before the verdict")
    verdict: E("pass", "fail", "unsure") = Field(description="pass, fail, or unsure when in doubt")
    resembles: str = Field(description="what it resembles, or empty string")
    location: E(*LOCATIONS) = Field(description="where on the image, or none")


class IpCheck(Strict):
    items: list[IpItem] = Field(description="one item per rule")


class SimAspect(Strict):
    aspect: E("outfit", "print_or_motif", "hairstyle", "colour_scheme", "accessory", "face", "whole_character") = Field(
        description="the aspect compared")
    evidence: str = Field(description="what is shared or different")
    level: E("none", "general_style", "specific_element", "near_copy") = Field(description="how much of the reference is reproduced")


class SimCheck(Strict):
    aspects: list[SimAspect] = Field(description="one entry per aspect")


# ---- route table ---------------------------------------------------------------------------------------------------
ROLE_SCHEMAS: dict[str, type[Strict]] = {
    "L1": ReferenceAnalysis, "L2": TasteProfile, "L3": PlanSet, "L4": Critique, "L5": PairJudgment, "L6": Revision,
    "L7": ChangePlan, "L9": HairMatch, "L10": RepairPlan, "L11": AssetCheck, "L12a": DuoJudgment, "L12b": DuoReview,
    "L13": IpCheck, "L14": SimCheck, "L15": ElementList,
}

#: every schema class by name (what ``SCHEMAS.lock`` hashes and the templates name in their front matter)
SCHEMA_CLASSES: dict[str, type[Strict]] = {c.__name__: c for c in ROLE_SCHEMAS.values()}
SCHEMA_CLASSES.update({c.__name__: c for c in (StructureRule, Flag, TasteRule, Score, Fix, CritCompare, RevisionOp, ImageFix, Redo,
                                               ChangeOp, InventoryItem, HairAdjust, Verdict, RepairOp, DuoCompare, DuoLevel,
                                               IpItem, SimAspect)})
