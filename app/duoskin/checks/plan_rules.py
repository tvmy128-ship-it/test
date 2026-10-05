"""C1, the plan linter: every rule of PROMPT_BIBLE §9.4 and FAILURE_MODES §7.2 as a pure function (APP_SPEC §10.2).

HARD only for the classes of APP_SPEC §3.1: Roblox rules, buildability (kits, the duo contract, the garment cut, the clone proxies),
IP and stray text (the free-text lint), the registry and integrity. Taste, colour distance, restraint and novelty are SOFT: they
warn, feed the critic and rank, and never trigger a revision round. The kind of every result comes from ``data/checks.json`` through
``checks/runner.py::build_result``, so this module cannot re-label a rule.

Entry points
    ``lint_spec(spec, ctx) -> LintReport``                 every per-spec rule (``CHK-G0-01..12``, ``PLN-DNA-01``, ``PLN-15``, ``TASTE_DETAIL``)
    ``lint_plan_set(specs, ctx, brief_constraints) -> LintReport``   plan-set rules (``CHK-G0-07``, ``PLN-STR-01``, soft distribution)
    ``lint_plan(plan, ctx) -> PlanReport``                  both, for a ``PlanSet``
    ``leak_sets(spec, character)``                          the partner-only colours for A_LEAK / dj_no_leak, keyed by the structure profile

``LintReport.reviser_findings()`` turns the HARD failures into the numbered findings that go to L6. Every threshold comes from
``checks/thresholds.py`` (through ``prompts/limits.py``, which marks the few fallbacks) or from ``data/*.json``; there are no literal
numbers here.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from duoskin.checks import policy, runner
from duoskin.checks.model import CheckResult
from duoskin.models import dna as dna_mod
from duoskin.models import spec_rules
from duoskin.models.dna import normalise_text
from duoskin.models.kitenums import KitError, KitInventory, current_inventory
from duoskin.models.spec import ACCESSORY_ATTACHMENTS, CONTRAST_AXES, DuoSpec, PlanSet
from duoskin.prompts import freetext
from duoskin.prompts.catalog import Banned, data_json, default_ctx
from duoskin.prompts.limits import describe, thr

CHARS = ("a", "b")
HAIR_CUSTOM = "hair_custom"
SLOT_SOURCE_KEYS = frozenset({"hair.description", "print.motif", "shoes.motif", "accessory.description", "dna.motif_object"})
COLOUR_AXES = ("colour_temperature", "value")
FACE_FIELDS = ("eye_shape", "iris_style", "highlight_style", "lash_style", "brow_style", "mouth_style", "cheek_mark")
CUT_ATTRIBUTES = ("sleeve", "hem", "leg", "neckline", "front", "block_layout")
ATTACHMENT_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "hat": ("hat",), "hair": ("hair",), "face": ("face_front", "face_center"), "neck": ("neck",),
    "shoulder": ("right_shoulder", "left_shoulder", "right_collar", "left_collar", "neck"), "front": ("body_front",),
    "back": ("body_back",), "waist": ("waist_front", "waist_center", "waist_back")}
HEAD_ATTACHMENTS = ("hat", "hair", "face_front", "face_center")
ROBLOX_TYPE = {"hat": "Hat", "hair": "Hair", "face": "Face", "neck": "Neck", "shoulder": "Shoulder", "front": "Front", "back": "Back",
               "waist": "Waist"}
LINKED_KINDS_OK = ("plush_pet", "keychain_charm", "prop")
# Roles whose colours belong to a character, and the roles shared by both
OWN_ROLES = {"a": ("a_main", "a_second", "hair_a"), "b": ("b_main", "b_second", "hair_b")}
SHARED_ROLES = ("shared",)
DNA_DUPLICATE_OF = {"shape_language": "shape_language", "hair.kit_style_id": "hair_shape"}    # code-side axis -> declared axis it duplicates


# --------------------------------------------------------------------------------------------------- data
@lru_cache(maxsize=1)
def _profiles() -> dict[str, Any]:
    return data_json("structure_profiles.json")


@lru_cache(maxsize=1)
def _rules_plan() -> dict[str, Any]:
    return data_json("rules.json")["plan"]


@lru_cache(maxsize=1)
def _season() -> dict[str, Any]:
    return data_json("season_map.json")


@lru_cache(maxsize=1)
def _style() -> dict[str, Any]:
    return data_json("style_guide.json")


def profile(structure: str) -> dict[str, Any]:
    """The structure profile row (``data/structure_profiles.json``)."""
    try:
        return _profiles()["profiles"][structure]
    except KeyError:
        raise KeyError(f"unknown pair structure {structure!r}") from None


def axis_group(axis: str) -> str:
    for g, axes in _profiles()["axis_groups"].items():
        if axis in axes:
            return g
    return ""


# --------------------------------------------------------------------------------------------------- colour helpers
def _de(a: str, b: str) -> float:
    from duoskin.imaging.palette import de2000_hex

    return de2000_hex(a, b)


def _lightness(h: str) -> float:
    from duoskin.imaging.palette import hex_to_lab

    return float(hex_to_lab(h)[0])


def season_group(hex_colour: str) -> str:
    """The season group of a colour (``data/season_map.json``): warm or cool by CIELAB hue angle, light or deep by lightness."""
    import math

    from duoskin.imaging.palette import hex_to_lab

    cfg = _season()
    lab = hex_to_lab(hex_colour)
    chroma = math.hypot(float(lab[1]), float(lab[2]))
    hue = math.degrees(math.atan2(float(lab[2]), float(lab[1])) % math.tau)
    light = float(lab[0]) >= cfg["light_l_min"]
    if chroma < cfg["min_chroma"]:
        return "summer" if light else "winter"
    lo, hi = cfg["warm_hue_deg"]
    warm = lo <= hue <= hi
    return ("spring" if light else "autumn") if warm else ("summer" if light else "winter")


def role_hex(spec: DuoSpec, role: str) -> str | None:
    for c in spec.palette:
        if c.role == role:
            return c.hex
    return None


def _pal(spec: DuoSpec) -> dict[str, str]:
    return {c.id: c.hex for c in spec.palette}


# --------------------------------------------------------------------------------------------------- report types
@dataclass(frozen=True)
class Finding:
    """One thing the reviser can act on: a failed rule with a JSON Pointer."""

    check_id: str
    rule: str
    path: str
    message: str
    hard: bool


@dataclass
class LintReport:
    results: list[CheckResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[CheckResult]:
        return [r for r in self.results if policy.is_blocking(r)]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.kind == "soft" and r.ran and not r.passed]

    @property
    def passed(self) -> bool:
        """No HARD or ASSERT rule failed or did not run (SOFT results never block)."""
        return not self.blocking

    def reviser_findings(self) -> list[dict[str, Any]]:
        """The HARD findings, numbered from 1, as the ``<findings>`` of L6 (bible §9.6)."""
        hard = [f for f in self.findings if f.hard]
        return [{"number": i, "path": f.path, "problem": f.message, "rule": f.rule} for i, f in enumerate(hard, 1)]

    def merge(self, other: LintReport) -> LintReport:
        self.results += other.results
        self.findings += other.findings
        return self

    def by_metric(self, metric: str) -> list[CheckResult]:
        return [r for r in self.results if r.metric == metric]


@dataclass
class PlanReport:
    per_spec: list[LintReport]
    set_report: LintReport

    @property
    def passed(self) -> bool:
        return self.set_report.passed and all(r.passed for r in self.per_spec)

    @property
    def all_results(self) -> list[CheckResult]:
        return [*self.set_report.results, *(r for rep in self.per_spec for r in rep.results)]


@dataclass
class PlanLintCtx:
    """Facts the linter reads (it reads nothing from disk by itself)."""

    inventory: KitInventory | None = None
    structure_request: str = "auto"                      # "auto" or a PairStructure (the brief form's dropdown, APP_SPEC §2 S18)
    must_include: tuple[str, ...] = ()                   # up to 5 lines from the brief form
    recent_cards: Sequence[Any] = ()                     # DnaCard objects or compact card dicts (the last few); a SOFT novelty hint only
    registry_lookup: Callable[[DuoSpec], Sequence[str]] | None = None   # registered asset ids a spec references (HARD exact reuse)
    sparkle_star_allowed: bool = True                    # Settings switch (bible D26)
    banned: Banned | None = None
    subject_sha: str = ""

    def inv(self) -> KitInventory:
        return self.inventory or current_inventory()

    def bans(self) -> Banned:
        return self.banned or default_ctx(self.inv()).banned


class _Rep:
    """Collects results and findings for one lint run."""

    def __init__(self, sha: str):
        self.sha = sha
        self.rep = LintReport()

    def add(self, check_id: str, ok: bool, metric: str, *, value: float | None = None, threshold: str = "", evidence: str = "",
            paths: Sequence[str] = (), messages: Sequence[str] = ()) -> None:
        res = runner.build_result(check_id, passed=ok, subject_sha=self.sha, metric=metric, value=value, threshold=threshold,
                                  evidence=evidence, fix_hint="revise_plan" if not ok else "none")
        self.rep.results.append(res)
        if not ok:
            hard = res.kind in ("hard", "assert")
            for i, p in enumerate(paths or ("",)):
                msg = messages[i] if i < len(messages) else (messages[0] if messages else evidence)
                self.rep.findings.append(Finding(check_id, metric, p, msg, hard))


def _sha(spec: DuoSpec) -> str:
    from duoskin.models.common import sha256_of

    return sha256_of(spec.model_dump(mode="json"))


def _short(items: Iterable[str], n: int = 4) -> str:
    items = list(items)
    return "; ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def _chars(spec: DuoSpec):
    return (("a", spec.a), ("b", spec.b))


# --------------------------------------------------------------------------------------------------- per-spec rules
def rule_palette_integrity(spec: DuoSpec, R: _Rep) -> None:
    """C1 #1 (palette part): hex pattern, unique ids, every ``*_ref`` resolves, palette size. CHK-G0-09."""
    probs = [p for p in spec_rules.spec_problems(spec) if p.rule in ("palette", "refs") or p.code == "palette_count"]
    R.add("CHK-G0-09", not probs, "palette_integrity", value=len(probs), threshold="0 problems", evidence=_short(map(str, probs)),
          paths=[p.path for p in probs], messages=[p.message for p in probs])
    face_probs: list[spec_rules.Problem] = []
    for ck, c in _chars(spec):
        if c.face.cheek_mark == "none" and c.face.blush_ref != "none":
            face_probs.append(spec_rules.Problem("blush", f"/{ck}/face/blush_ref", "blush_ref must be 'none' when cheek_mark is 'none'", "refs"))
        if c.face.cheek_mark != "none" and c.face.blush_ref == "none":
            face_probs.append(spec_rules.Problem("blush", f"/{ck}/face/blush_ref", "a cheek_mark needs a blush_ref colour", "refs"))
        if c.bottom.legwear != "bare" and c.bottom.legwear_ref == "none":
            face_probs.append(spec_rules.Problem("legwear", f"/{ck}/bottom/legwear_ref", "legwear needs a colour id", "refs"))
    R.add("CHK-G0-09", not face_probs, "blush_and_legwear_refs", value=len(face_probs), threshold="0 problems",
          evidence=_short(map(str, face_probs)), paths=[p.path for p in face_probs], messages=[p.message for p in face_probs])


def rule_lash_iris(spec: DuoSpec, R: _Rep) -> None:
    """C1 #18 (PLN-13): the lash colour is at least ΔE2000 10 from ``iris_ref`` and ``iris_dark_ref``. The pupil does not count."""
    pal = _pal(spec)
    need = float(thr("pln.lash_iris_de_min"))
    bad: list[str] = []
    paths: list[str] = []
    worst = None
    for ck, c in _chars(spec):
        lash = pal.get(c.face.lash_ref)
        for ref_name in ("iris_ref", "iris_dark_ref"):
            other = pal.get(getattr(c.face, ref_name))
            if lash is None or other is None:
                continue                                         # dangling refs are reported by palette_integrity
            d = _de(lash, other)
            worst = d if worst is None else min(worst, d)
            if d < need:
                bad.append(f"/{ck}/face lash vs {ref_name}: dE {d:.1f}")
                paths.append(f"/{ck}/face/lash_ref")
    R.add("CHK-G0-09", not bad, "lash_vs_iris", value=worst, threshold=describe("pln.lash_iris_de_min", ">="), evidence=_short(bad),
          paths=paths, messages=[f"{b}; pick a lash colour at least {need:g} dE2000 away from both iris colours" for b in bad])


def rule_free_text(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> None:
    """C1 #12: the free-text lint on every free-text field, and the word caps. CHK-G0-08 (HARD, class ip)."""
    banned = ctx.bans()
    caps: list[spec_rules.Problem] = [p for p in spec_rules.spec_problems(spec) if p.rule == "caps"]
    R.add("CHK-G0-08", not caps, "word_caps", value=len(caps), threshold="0 fields over their cap", evidence=_short(map(str, caps)),
          paths=[p.path for p in caps], messages=[p.message for p in caps])
    bad: list[tuple[str, str]] = []
    for path, key, text in spec_rules.iter_free_text(spec):
        probs = freetext.free_text_problems(text, None, banned, strict=key in SLOT_SOURCE_KEYS)
        for p in probs:
            bad.append((path, f"{p.kind}: {p.detail}"))
    R.add("CHK-G0-08", not bad, "free_text", value=len(bad), threshold="0 problems",
          evidence=_short(f"{p}: {m}" for p, m in bad), paths=[p for p, _ in bad], messages=[m for _, m in bad])


def _recipe_family(inv: KitInventory, recipe_id: str) -> str:
    try:
        return inv.recipe(recipe_id).family
    except KitError:
        return recipe_id


def rule_kit_ids(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> None:
    """C1 #5: kit ids exist and are compatible. CHK-G0-01."""
    inv = ctx.inv()
    bad: list[tuple[str, str]] = []
    for ck, c in _chars(spec):
        pairs = [("HairKit", c.hair.kit_style_id, "hair/kit_style_id"), ("FringeKit", c.hair.fringe_id, "hair/fringe_id"),
                 ("BackKit", c.hair.back_id, "hair/back_id"), ("TopRecipeKit", c.top.recipe_id, "top/recipe_id"),
                 ("InnerTopKit", c.top.inner_recipe_id, "top/inner_recipe_id"), ("BottomRecipeKit", c.bottom.recipe_id, "bottom/recipe_id"),
                 ("FabricKit", c.top.fabric_id, "top/fabric_id"), ("FabricKit", c.bottom.fabric_id, "bottom/fabric_id"),
                 ("ShoeKit", c.bottom.shoes.style_id, "bottom/shoes/style_id"), ("SkinToneKit", c.body.skin_tone, "body/skin_tone"),
                 ("EyeShapeKit", c.face.eye_shape, "face/eye_shape"), ("MouthKit", c.face.mouth_style, "face/mouth_style")]
        for kind, value, sub in pairs:
            if value not in inv.ids(kind):
                bad.append((f"/{ck}/{sub}", f"{value!r} is not a {kind} id (the kit does not have it)"))
        try:
            top = inv.recipe(c.top.recipe_id)
            for attr, val in (("sleeve", c.top.sleeve), ("hem", c.top.hem), ("neckline", c.top.neckline), ("front", c.top.front),
                              ("block_layout", c.top.block_layout)):
                allowed = top.cut.get(attr)
                if allowed and val not in allowed:
                    bad.append((f"/{ck}/top/{attr}", f"recipe {top.id} cannot draw {attr}={val!r}; it allows {', '.join(allowed)}"))
            for attr, allowed in top.requires_bottom.items():
                if getattr(c.bottom, attr, None) not in allowed:
                    bad.append((f"/{ck}/bottom/{attr}", f"{top.id} requires bottom.{attr} to be one of {', '.join(allowed)}"))
        except KitError:
            pass
        try:
            bottom = inv.recipe(c.bottom.recipe_id)
            for attr, val in (("leg", c.bottom.leg), ("waist", c.bottom.waist)):
                allowed = bottom.cut.get(attr)
                if allowed and val not in allowed:
                    bad.append((f"/{ck}/bottom/{attr}", f"recipe {bottom.id} cannot draw {attr}={val!r}; it allows {', '.join(allowed)}"))
        except KitError:
            pass
        try:
            top_row = inv.shoe(c.bottom.shoes.style_id).top_row
            lo, hi = thr("tpl.shoe_top_row_range")
            if top_row and not lo <= top_row <= hi:
                bad.append((f"/{ck}/bottom/shoes/style_id", f"shoe top row {top_row} is outside rows {lo} to {hi}"))
        except KitError:
            pass
        if c.top.inner_recipe_id != "none" and c.top.front == "closed":
            bad.append((f"/{ck}/top/inner_recipe_id", "inner_recipe_id is set only when front is open or layered"))
        if c.hair.kit_style_id == HAIR_CUSTOM and c.hair.fringe_id == "kit_default":
            bad.append((f"/{ck}/hair/fringe_id", "a hair_custom hair has no kit fringe to resolve: use a fringe module or none"))
        if c.hair.kit_style_id == HAIR_CUSTOM and not c.hair.description.strip():
            bad.append((f"/{ck}/hair/description", "a hair_custom hair needs a description (it is the only hair phrase)"))
    R.add("CHK-G0-01", not bad, "kit_ids_and_compatibility", value=len(bad), threshold="0 problems", evidence=_short(m for _, m in bad),
          paths=[p for p, _ in bad], messages=[m for _, m in bad])


def _attachment_name(att: str) -> str:
    return "".join(p.title() for p in att.split("_"))


def rule_roblox(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> None:
    """C1 #8 to #11 and the head-texture allow-list. CHK-G0-02 (HARD, class roblox)."""
    from duoskin.roblox import limits

    sizes = _rules_plan()["size_class_studs"]
    slot_bad: list[tuple[str, str]] = []
    cat_bad: list[tuple[str, str]] = []
    size_bad: list[tuple[str, str]] = []
    make_bad: list[tuple[str, str]] = []
    head_bad: list[tuple[str, str]] = []
    uniq_bad: list[tuple[str, str]] = []
    makeup_ok = ctx.inv().flags.makeup_available
    for ck, c in _chars(spec):
        used: dict[str, int] = {}
        for i, a in enumerate(c.accessories):
            p = f"/{ck}/accessories/{i}"
            ok_att = ATTACHMENT_BY_CATEGORY.get(a.category, ())
            if a.attachment not in ok_att:
                slot_bad.append((f"{p}/attachment", f"category {a.category} cannot use attachment {a.attachment}; use one of {', '.join(ok_att)}"))
            if a.attachment in HEAD_ATTACHMENTS and a.category not in ("hat", "face", "hair"):
                cat_bad.append((f"{p}/category", "an item attached to the head must be a hat or face item"))
            if a.kind in ("sticker_slab", "small_hat") and a.attachment in HEAD_ATTACHMENTS and a.category not in ("hat", "face"):
                cat_bad.append((f"{p}/category", f"a {a.kind} near the head must be category hat or face"))
            if a.kind == "hair_clip_slab" and a.category != "hat":
                cat_bad.append((f"{p}/category", "a hair_clip_slab is a partial hair ornament: category hat"))
            if a.category == "hair" and a.kind != "prop":
                cat_bad.append((f"{p}/category", "category hair is only for complete hairstyles; use hat or face for a hair ornament"))
            if a.category in ("shoulder",) and a.attachment in HEAD_ATTACHMENTS:
                cat_bad.append((f"{p}/category", "a shoulder item cannot attach to the head"))
            if a.attachment in used:
                uniq_bad.append((f"{p}/attachment", f"/{ck}/accessories/{used[a.attachment]} already uses the {a.attachment} attachment"))
            used[a.attachment] = i
            try:
                box = limits.box_for(ROBLOX_TYPE[a.category], _attachment_name(a.attachment))
                studs = float(sizes[a.size_class])
                if studs > max(box.size):
                    size_bad.append((f"{p}/size_class", f"size {a.size_class} (about {studs:g} studs) does not fit the {a.category} box "
                                                         f"{'x'.join(f'{x:g}' for x in box.size)} studs; use a smaller size class"))
            except KeyError:
                pass                                               # an invalid pair is already a slot_attachment finding
        mk = c.makeup
        if mk.kind != "none" and not makeup_ok:
            make_bad.append((f"/{ck}/makeup/kind", "makeup is unavailable in this kit inventory: makeup.kind must be none"))
        if c.face.highlight_style == "sparkle_star" and not ctx.sparkle_star_allowed:
            head_bad.append((f"/{ck}/face/highlight_style", "sparkle_star is switched off in Settings: use dual_dot"))
    for metric, bad in (("slot_attachment", slot_bad), ("category_policy", cat_bad), ("size_class_box", size_bad),
                        ("makeup_routing", make_bad), ("head_texture_allow_list", head_bad), ("attachment_unique", uniq_bad)):
        R.add("CHK-G0-02", not bad, metric, value=len(bad), threshold="0 problems", evidence=_short(m for _, m in bad),
              paths=[p for p, _ in bad], messages=[m for _, m in bad])


# ---- contrasts ----------------------------------------------------------------------------------
def _hair_custom_pair(spec: DuoSpec) -> bool:
    return spec.a.hair.kit_style_id == HAIR_CUSTOM and spec.b.hair.kit_style_id == HAIR_CUSTOM


def _acc_set(c: Any, attr: str) -> frozenset[str]:
    return frozenset(getattr(a, attr) for a in c.accessories)


def measure_axis(axis: str, spec: DuoSpec, inv: KitInventory) -> tuple[bool, str]:
    """Whether a declared contrast axis can be verified from the spec fields, with the evidence (PLN-04: a contrast counts only if
    code can measure it)."""
    a, b = spec.a, spec.b
    if axis in COLOUR_AXES:
        pairs = [(role_hex(spec, "a_main"), role_hex(spec, "b_main")), (role_hex(spec, "a_second"), role_hex(spec, "b_second"))]
        pairs = [(x, y) for x, y in pairs if x and y]
        if not pairs:
            return False, "the palette has no a_main/b_main role colours to compare"
        if axis == "colour_temperature":
            d = max(_de(x, y) for x, y in pairs)
            return d >= thr("pln.contrast_colour_de"), f"role colours differ by dE2000 {d:.1f}"
        dl = max(abs(_lightness(x) - _lightness(y)) for x, y in pairs)
        return dl >= thr("pln.contrast_value_dl"), f"role colours differ by dL* {dl:.1f}"
    if axis == "hair_shape":
        if _hair_custom_pair(spec):
            return False, "both hair_custom: satisfies the hair rule but is not countable"
        return a.hair.kit_style_id != b.hair.kit_style_id, "different hair kit styles"
    if axis == "hair_length":
        sa, sb = inv.hair_style(a.hair.kit_style_id), inv.hair_style(b.hair.kit_style_id)
        if sa is None or sb is None or not sa.length_class or not sb.length_class:
            return False, "no kit length class to compare (hair_custom)"
        return sa.length_class != sb.length_class, f"kit length classes {sa.length_class} and {sb.length_class}"
    if axis == "top_type":
        fa, fb = _recipe_family(inv, a.top.recipe_id), _recipe_family(inv, b.top.recipe_id)
        return fa != fb, f"top recipe families {fa} and {fb}"
    if axis == "bottom_type":
        fa, fb = _recipe_family(inv, a.bottom.recipe_id), _recipe_family(inv, b.bottom.recipe_id)
        return fa != fb, f"bottom recipe families {fa} and {fb}"
    simple = {"sleeve_length": lambda: (a.top.sleeve, b.top.sleeve), "leg_length": lambda: (a.bottom.leg, b.bottom.leg),
              "neckline": lambda: (a.top.neckline, b.top.neckline), "layering": lambda: (a.top.front, b.top.front),
              "block_layout": lambda: (a.top.block_layout, b.top.block_layout), "face_eyes": lambda: (a.face.eye_shape, b.face.eye_shape),
              "face_mouth": lambda: (a.face.mouth_style, b.face.mouth_style), "expression": lambda: (a.face.default_expression, b.face.default_expression),
              "shape_language": lambda: (a.dna.shape_language, b.dna.shape_language)}
    if axis in simple:
        x, y = simple[axis]()
        return x != y, f"{x} and {y}"
    if axis == "pattern_scale":
        pa = (a.top.prints or a.bottom.prints)
        pb = (b.top.prints or b.bottom.prints)
        if not pa or not pb:
            return False, "a character has no print to compare"
        return pa[0].scale != pb[0].scale, f"print scales {pa[0].scale} and {pb[0].scale}"
    if axis == "fabric":
        same = a.top.fabric_id == b.top.fabric_id and a.bottom.fabric_id == b.bottom.fabric_id
        return not same, "different fabric ids"
    if axis in ("accessory_kind", "accessory_slot"):
        attr = "kind" if axis == "accessory_kind" else "category"
        sa, sb = _acc_set(a, attr), _acc_set(b, attr)
        if not sa and not sb:
            return False, "neither character has an accessory"
        return sa != sb, f"{attr} sets {sorted(sa)} and {sorted(sb)}"
    return False, f"axis {axis!r} is unknown"


@dataclass
class ContrastTally:
    declared: list[tuple[int, str]]
    counted: dict[str, str]                   # axis -> evidence, declared and measurable, distinct
    unmeasurable: list[tuple[int, str, str]]  # (index, axis, why)
    duplicates: list[tuple[int, str]]
    dna_credits: dict[str, str]               # "dna_<field>" -> field


def tally_contrasts(spec: DuoSpec, inv: KitInventory) -> ContrastTally:
    """Which declared contrasts count, plus the code-side ``dna_<field>`` credits (bible §3.2 ``Contrast``)."""
    counted: dict[str, str] = {}
    unmeasurable: list[tuple[int, str, str]] = []
    dups: list[tuple[int, str]] = []
    seen: set[str] = set()
    for i, c in enumerate(spec.contrasts):
        if c.axis in seen:
            dups.append((i, c.axis))
            continue
        seen.add(c.axis)
        ok, why = measure_axis(c.axis, spec, inv)
        if ok:
            counted[c.axis] = why
        else:
            unmeasurable.append((i, c.axis, why))
    card = dna_mod.card_from_spec(spec)
    hair_contrast = _hair_contrast_declared(spec)
    credits: dict[str, str] = {}
    for f in dna_mod.character_field_differences(card, hair_custom_contrast=hair_contrast):
        dup = DNA_DUPLICATE_OF.get(f)
        if dup and dup in counted:
            continue
        credits[f"dna_{f}"] = f
    return ContrastTally(declared=[(i, c.axis) for i, c in enumerate(spec.contrasts)], counted=counted, unmeasurable=unmeasurable,
                         duplicates=dups, dna_credits=credits)


def _hair_contrast_declared(spec: DuoSpec) -> bool:
    """Two hair_custom hairs differ only when a hair_shape or hair_length contrast is declared with differing descriptions (§2 S19)."""
    declared = any(c.axis in ("hair_shape", "hair_length") and normalise_text(c.a_value) != normalise_text(c.b_value) for c in spec.contrasts)
    differ = normalise_text(spec.a.hair.description) != normalise_text(spec.b.hair.description)
    return declared and differ


def rule_contrasts(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> ContrastTally:
    """C1 #3: at least 5 contrasts on distinct axes, every one measurable, colour axes limited. CHK-G0-03 and CHK-G0-04."""
    inv = ctx.inv()
    t = tally_contrasts(spec, inv)
    n_decl = len({ax for _, ax in t.declared})
    need = int(thr("pln.contrasts_min"))
    anchors_lo, anchors_hi = thr("pln.anchors")
    # count of distinct declared axes (the schema asks for at least 5 on different axes)
    dup_paths = [f"/contrasts/{i}/axis" for i, _ in t.duplicates]
    R.add("CHK-G0-03", n_decl >= need and not t.duplicates, "contrast_axes_distinct", value=n_decl, threshold=f">= {need} distinct axes",
          evidence=f"{n_decl} distinct axes declared" + (f"; repeated: {_short(ax for _, ax in t.duplicates)}" if t.duplicates else ""),
          paths=dup_paths or ["/contrasts"], messages=["each contrast must be on a different axis"] if dup_paths else [f"declare at least {need} contrasts on different axes"])
    n_counted = len(t.counted) + len(t.dna_credits)
    R.add("CHK-G0-04", n_counted >= need, "contrasts_measurable", value=n_counted, threshold=describe("pln.contrasts_min", ">="),
          evidence=f"{len(t.counted)} measurable + {len(t.dna_credits)} DNA credit(s); not countable: "
                   + (_short(f"{ax} ({why})" for _, ax, why in t.unmeasurable) or "none"),
          paths=[f"/contrasts/{i}" for i, _, _ in t.unmeasurable] or ["/contrasts"],
          messages=[f"{ax} is not measurable: {why}; replace it with a contrast code can verify" for _, ax, why in t.unmeasurable]
          or [f"only {n_counted} countable contrasts; add structural contrasts"])
    structure = spec.world.pair_structure
    cfg = profile(structure)["contrast_rule"]
    which = 1 if cfg.get("colour_axes") == "same_club" else 0
    limit = int(thr("pln.colour_axes_max")[which])
    colour = [ax for ax in t.counted if ax in COLOUR_AXES]
    R.add("CHK-G0-03", len(colour) <= limit, "colour_axes", value=len(colour), threshold=f"<= {limit} colour axes ({structure})",
          evidence=f"colour axes counted: {', '.join(colour) or 'none'}",
          paths=[f"/contrasts/{i}" for i, ax in t.declared if ax in COLOUR_AXES], messages=["too many colour contrasts: recolours read as clones; add structural contrasts"])
    n = len(spec.shared_anchors)
    bad_vis = [i for i, a in enumerate(spec.shared_anchors) if a.visible_from not in ("front", "both") or not a.on_a.strip() or not a.on_b.strip()]
    R.add("CHK-G0-03", anchors_lo <= n <= anchors_hi and not bad_vis, "anchors", value=n, threshold=describe("pln.anchors", "in"),
          evidence=f"{n} anchors" + (f"; not visible on both from the front: {bad_vis}" if bad_vis else ""),
          paths=[f"/shared_anchors/{i}" for i in bad_vis] or ["/shared_anchors"],
          messages=["each anchor must show on both characters from the front"] if bad_vis else [f"{anchors_lo} or {anchors_hi} shared anchors are required"])
    return t


def rule_combo(spec: DuoSpec, R: _Rep) -> None:
    """C1 #4: ``combo`` matches the presentations (``bg``: a is a boy, b is a girl)."""
    want = tuple("boy" if ch == "b" else "girl" for ch in spec.combo)
    have = (spec.a.presentation, spec.b.presentation)
    R.add("CHK-G0-03", want == have, "combo_presentation", threshold="presentations equal the combo",
          evidence=f"combo {spec.combo} wants {want}, spec has {have}", paths=["/combo"],
          messages=[f"combo {spec.combo} needs a={want[0]} and b={want[1]}"])


def rule_face_grammar(spec: DuoSpec, R: _Rep) -> None:
    """C1 #6: A and B differ in at least 3 of the 7 face grammar fields."""
    diff = [f for f in FACE_FIELDS if getattr(spec.a.face, f) != getattr(spec.b.face, f)]
    need = int(thr("pln.face_features_diff_min"))
    R.add("CHK-G0-03", len(diff) >= need, "face_grammar_difference", value=len(diff), threshold=describe("pln.face_features_diff_min", ">="),
          evidence=f"differing: {', '.join(diff) or 'none'}", paths=["/b/face"],
          messages=[f"A and B differ in {len(diff)} of 7 face fields ({', '.join(diff) or 'none'}); change at least {need}"])


def rule_hair_pairing(spec: DuoSpec, R: _Rep) -> None:
    """C1 #13: hair kit A != B, unless both are hair_custom with a hair_shape or hair_length contrast and differing descriptions."""
    a, b = spec.a.hair, spec.b.hair
    if a.kit_style_id == HAIR_CUSTOM and b.kit_style_id == HAIR_CUSTOM:
        ok = _hair_contrast_declared(spec)
        R.add("CHK-G0-03", ok, "hair_pairing", threshold="hair_shape or hair_length contrast + differing descriptions",
              evidence="two hair_custom hairs" + ("" if ok else " without a hair_shape or hair_length contrast and differing descriptions"),
              paths=["/contrasts", "/b/hair/description"], messages=["two hair_custom hairs need a hair_shape or hair_length contrast", "give B a different hair description"])
        return
    R.add("CHK-G0-03", a.kit_style_id != b.kit_style_id, "hair_pairing", threshold="A and B use different hair kit styles",
          evidence=f"A {a.kit_style_id}, B {b.kit_style_id}", paths=["/b/hair/kit_style_id"], messages=["A and B must not use the same hair kit style"])


def _acc_signature(a: Any) -> tuple[str, str, str]:
    return a.kind, a.category, normalise_text(a.description)


def rule_accessory_complement(spec: DuoSpec, R: _Rep) -> None:
    """C1 #16: accessories complement, never repeat; a linked pair differs in kind or category."""
    sig_a = {_acc_signature(x): i for i, x in enumerate(spec.a.accessories)}
    bad: list[tuple[str, str]] = []
    for j, x in enumerate(spec.b.accessories):
        s = _acc_signature(x)
        if s in sig_a:
            bad.append((f"/b/accessories/{j}", f"B's accessory repeats A's (/a/accessories/{sig_a[s]}): same kind, category and description"))
    if any(an.kind == "accessory_pair" for an in spec.shared_anchors):
        la = [x for x in spec.a.accessories if x.linked_to_partner]
        lb = [x for x in spec.b.accessories if x.linked_to_partner]
        for x in la:
            for y in lb:
                if x.kind == y.kind and x.category == y.category:
                    bad.append(("/b/accessories", "a linked accessory pair must differ in kind or category"))
    R.add("CHK-G0-03", not bad, "accessory_complement", value=len(bad), threshold="0 repeats", evidence=_short(m for _, m in bad),
          paths=[p for p, _ in bad], messages=[m for _, m in bad])


def rule_garment_cut(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> None:
    """C1 #7 and the profile's cut row: A and B differ in top or bottom type and in at least 2 cut attributes. CHK-G0-05."""
    inv = ctx.inv()
    cfg = profile(spec.world.pair_structure)["cut_rule"]
    fa, fb = _recipe_family(inv, spec.a.top.recipe_id), _recipe_family(inv, spec.b.top.recipe_id)
    ga, gb = _recipe_family(inv, spec.a.bottom.recipe_id), _recipe_family(inv, spec.b.bottom.recipe_id)
    type_differs = fa != fb or ga != gb
    vals = {"sleeve": (spec.a.top.sleeve, spec.b.top.sleeve), "hem": (spec.a.top.hem, spec.b.top.hem),
            "leg": (spec.a.bottom.leg, spec.b.bottom.leg), "neckline": (spec.a.top.neckline, spec.b.top.neckline),
            "front": (spec.a.top.front, spec.b.top.front), "block_layout": (spec.a.top.block_layout, spec.b.top.block_layout)}
    diff = [k for k in cfg["attributes"] if vals[k][0] != vals[k][1]]
    need = int(cfg["min_cut_differences"])
    ok = (type_differs or not cfg["top_or_bottom_type_differs"]) and len(diff) >= need
    R.add("CHK-G0-05", ok, "garment_cut", value=len(diff), threshold=f"top or bottom type differs and >= {need} of {', '.join(cfg['attributes'])}",
          evidence=f"types top {fa}/{fb}, bottom {ga}/{gb}; differing attributes: {', '.join(diff) or 'none'}",
          paths=["/b/top/recipe_id"] if not type_differs else ["/b/top"],
          messages=["A and B need a different top or bottom garment type" if not type_differs
                    else f"A and B differ in only {len(diff)} cut attribute(s); change at least {need}"])


def rule_dna(spec: DuoSpec, R: _Rep) -> None:
    """C1 #17 (PLN-DNA-01): A and B differ in at least ``pln.dna_char_diff_min`` CHARACTER DNA fields."""
    card = dna_mod.card_from_spec(spec)
    diff = dna_mod.character_field_differences(card, hair_custom_contrast=_hair_contrast_declared(spec))
    need = int(thr("pln.dna_char_diff_min"))
    R.add("PLN-DNA-01", len(diff) >= need, "dna_character_difference", value=len(diff), threshold=describe("pln.dna_char_diff_min", ">="),
          evidence=f"differing: {', '.join(diff) or 'none'}", paths=["/b/dna"],
          messages=[f"A and B share too much design DNA; differ in at least {need} of shape_language, colour_plan, focal_location, "
                    "hair kit, motif_object, accessory_style"])


def rule_profile_contrast(spec: DuoSpec, R: _Rep, t: ContrastTally, inv: KitInventory) -> None:
    """C1 #19: the contrast row of the structure profile (HARD): same_club, mirror, leader_chaotic, seasonal_twins, object_mascot."""
    structure = spec.world.pair_structure
    cfg = profile(structure)["contrast_rule"]
    problems: list[str] = []
    paths: list[str] = []
    if "min_from_groups" in cfg:
        g = cfg["min_from_groups"]
        n = sum(1 for ax in t.counted if axis_group(ax) in g["groups"])
        if n < g["min"]:
            problems.append(f"{structure} needs at least {g['min']} contrasts from {', '.join(g['groups'])} axes, has {n}")
            paths.append("/contrasts")
    if cfg.get("min_non_colour"):
        n = len([ax for ax in t.counted if ax not in COLOUR_AXES]) + len(t.dna_credits)
        if n < cfg["min_non_colour"]:
            problems.append(f"{structure} needs at least {cfg['min_non_colour']} non-colour contrasts, has {n}")
            paths.append("/contrasts")
    if cfg.get("required_any_axes"):
        have = [ax for ax in cfg["required_any_axes"] if ax in t.counted or f"dna_{ax}" in t.dna_credits]
        if not have:
            problems.append(f"{structure} needs one of {', '.join(cfg['required_any_axes'])} among the contrasts")
            paths.append("/contrasts")
    if cfg.get("must_differ"):
        for what in cfg["must_differ"]:
            if what == "hair_kit_style":
                ok = (spec.a.hair.kit_style_id != spec.b.hair.kit_style_id) if not _hair_custom_pair(spec) else _hair_contrast_declared(spec)
                if not ok:
                    problems.append("mirror pairs need a different hair kit style")
                    paths.append("/b/hair/kit_style_id")
            elif what == "garment_type":
                if (_recipe_family(inv, spec.a.top.recipe_id) == _recipe_family(inv, spec.b.top.recipe_id)
                        and _recipe_family(inv, spec.a.bottom.recipe_id) == _recipe_family(inv, spec.b.bottom.recipe_id)):
                    problems.append("mirror pairs need a different garment type")
                    paths.append("/b/top/recipe_id")
            elif what == "accessory_category":
                ca, cb = _acc_set(spec.a, "category"), _acc_set(spec.b, "category")
                if not (ca or cb) or ca == cb:
                    problems.append("mirror pairs need a different accessory category")
                    paths.append("/b/accessories")
    if cfg.get("mascot"):
        m = cfg["mascot"]
        linked = [x for ch in (spec.a, spec.b) for x in ch.accessories if x.kind in m["kinds"] and x.linked_to_partner]
        pair_anchor = any(an.kind == m["anchor_kind"] for an in spec.shared_anchors)
        if not linked and not pair_anchor:
            problems.append("object_mascot needs a linked plush_pet, keychain_charm or prop accessory, or an accessory_pair anchor")
            paths.append("/a/accessories")
    R.add("CHK-G0-03", not problems, "structure_profile_contrast", value=len(problems), threshold=f"{structure} contrast row",
          evidence=_short(problems), paths=paths, messages=problems)


def rule_registry(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> None:
    """C1 #14: no exact reuse of a registered print or face-part file (the ids a spec references). HARD, class registry."""
    refs = list(ctx.registry_lookup(spec)) if ctx.registry_lookup else []
    R.add("A_REGISTRY", not refs, "registry_reuse", value=len(refs), threshold="0 registered files referenced",
          evidence=_short(refs) if refs else "no registered file is referenced", paths=["/"], messages=[f"registered files are referenced: {_short(refs)}"])


# ---- soft rules -----------------------------------------------------------------------------------
def rule_structure_colours(spec: DuoSpec, R: _Rep) -> None:
    """The colour row of the structure profile and the anchor colour distance. CHK-G0-06 (SOFT: warning only)."""
    structure = spec.world.pair_structure
    rule = profile(structure)["colour_rule"]
    am, bm = role_hex(spec, "a_main"), role_hex(spec, "b_main")
    a2, b2 = role_hex(spec, "a_second"), role_hex(spec, "b_second")
    kind = rule["kind"]
    ok, ev = True, f"{structure}: no main-colour rule"
    if kind == "main_contrast" and am and bm:
        need = float(thr(rule["threshold_key"]))
        d = _de(am, bm)
        ok, ev = d >= need, f"a_main and b_main differ by dE2000 {d:.1f} (want >= {need:g})"
    elif kind == "role_swap" and am and bm and a2 and b2:
        need = float(thr(rule["threshold_key"]))
        d1, d2 = _de(am, b2), _de(bm, a2)
        ok, ev = max(d1, d2) <= need, f"a_main vs b_second {d1:.1f}, b_main vs a_second {d2:.1f} (want <= {need:g})"
    elif kind == "season_split" and am and bm:
        need = float(thr(rule["threshold_key"]))
        sa, sb = season_group(am), season_group(bm)
        d = _de(am, bm)
        ok, ev = sa != sb and d >= need, f"season groups {sa} and {sb}; dE2000 {d:.1f} (want different groups and >= {need:g})"
    R.add("CHK-G0-06", ok, "structure_colour_rule", threshold=rule["text"], evidence=ev, paths=["/palette"], messages=[ev])
    # anchor colour: only anchors of kind colour (SOFT)
    pal = _pal(spec)
    shared_ids = {c.id for c in spec.palette if c.role in (*SHARED_ROLES, "accent")}
    worst: float | None = None
    msgs: list[str] = []
    for i, an in enumerate(spec.shared_anchors):
        if an.kind != "colour":
            continue
        ids = {ck: _anchor_refs(c, shared_ids) for ck, c in _chars(spec)}
        if not ids["a"] or not ids["b"]:
            msgs.append(f"/shared_anchors/{i}: the colour anchor is not worn on both characters")
            continue
        d = min(_de(pal[x], pal[y]) for x in ids["a"] for y in ids["b"])
        worst = d if worst is None else max(worst, d)
        if d > thr("pln.anchor_colour_de_max"):
            msgs.append(f"/shared_anchors/{i}: the anchor colours differ by dE2000 {d:.1f}")
    R.add("CHK-G0-06", not msgs, "anchor_colour", value=worst, threshold=describe("pln.anchor_colour_de_max", "<="),
          evidence=_short(msgs) or "colour anchors match on both characters (or there is none)", paths=["/shared_anchors"], messages=msgs)


def _anchor_refs(c: Any, shared_ids: set[str]) -> set[str]:
    refs = {c.top.second_ref, c.top.trim_ref, c.bottom.second_ref, c.bottom.trim_ref, c.bottom.shoes.accent_ref, c.hair.highlight_ref,
            c.bottom.legwear_ref}
    for a in c.accessories:
        refs |= set(a.colour_refs)
    for p in (*c.top.prints, *c.bottom.prints):
        refs |= set(p.colour_refs)
    return {r for r in refs if r in shared_ids}


def _override_restraint(spec: DuoSpec, c: Any) -> bool:
    return spec.world.detail_level == "maximal" or c.dna.colour_plan == "allover_pattern"


def rule_restraint(spec: DuoSpec, R: _Rep) -> None:
    """Restraint (SOFT, CHK-G0-10-SOFT): main colours, hero prints and accessories. ``detail_level = maximal`` or
    ``colour_plan = allover_pattern`` may override. The accessory warnings are 3 and 4 or more; a real Roblox limit is CHK-G0-02."""
    cfg = _rules_plan()["restraint"]
    warn_at, again_at = thr("pln.acc_per_char_warn_hard")
    msgs: list[str] = []
    paths: list[str] = []
    for ck, c in _chars(spec):
        if _override_restraint(spec, c):
            continue
        n = len(c.accessories)
        if n >= again_at:
            msgs.append(f"{n} accessories on {ck.upper()}: noise disappears at thumbnail size")
            paths.append(f"/{ck}/accessories")
        elif n >= warn_at:
            msgs.append(f"{n} accessories on {ck.upper()}; most duos need fewer")
            paths.append(f"/{ck}/accessories")
        colours = {c.top.base_ref, c.top.second_ref, c.bottom.base_ref, c.bottom.second_ref, c.bottom.shoes.base_ref} - {"none"}
        if len(colours) > cfg["main_colours_max"]:
            msgs.append(f"{len(colours)} main colours on {ck.upper()}'s outfit")
            paths.append(f"/{ck}/top")
        for part in ("top", "bottom"):
            heroes = [p for p in getattr(c, part).prints if p.scale in ("medium", "large")]
            if len(heroes) > cfg["hero_prints_max"]:
                msgs.append(f"{len(heroes)} hero prints on {ck.upper()}'s {part}")
                paths.append(f"/{ck}/{part}/prints")
    R.add("CHK-G0-10-SOFT", not msgs, "restraint", value=len(msgs), threshold="few separate details", evidence=_short(msgs), paths=paths, messages=msgs)


def rule_detail_range(spec: DuoSpec, R: _Rep) -> None:
    """Detail level versus detail count (SOFT, TASTE_DETAIL): minimal 1-2, standard 2-3, maximal 4-5 details (prints, trims, accessories)."""
    lo, hi = _style()["detail_density"][spec.world.detail_level]
    msgs: list[str] = []
    for ck, c in _chars(spec):
        n = (len(c.top.prints) + len(c.bottom.prints) + (1 if c.bottom.shoes.motif.strip() else 0) + len(c.accessories)
             + sum(1 for r in (c.top.trim_ref, c.bottom.trim_ref) if r != "none"))
        if not lo <= n <= hi:
            msgs.append(f"{ck.upper()} has {n} details; {spec.world.detail_level} suggests {lo} to {hi}")
    R.add("TASTE_DETAIL", not msgs, "detail_count", threshold=f"{spec.world.detail_level}: {lo}-{hi} details", evidence=_short(msgs),
          paths=["/world/detail_level"], messages=msgs)


def rule_soft_misc(spec: DuoSpec, R: _Rep, ctx: PlanLintCtx) -> None:
    """CHK-G0-12 (SOFT): adjacent-colour contrast, fabric versus world material, accessory visibility, nearest past card; PLN-15 hair IoU."""
    inv = ctx.inv()
    pal = _pal(spec)
    need = float(thr("pln.adjacent_de_min"))
    msgs: list[str] = []
    for ck, c in _chars(spec):
        pairs: list[tuple[str, str, str, str]] = []
        try:
            skin = inv.skin_hex(c.body.skin_tone)
            pairs.append(("skin", skin, "top base", pal.get(c.top.base_ref, "")))
        except KitError:
            pass
        pairs.append(("top base", pal.get(c.top.base_ref, ""), "bottom base", pal.get(c.bottom.base_ref, "")))
        for p in c.top.prints[:1]:
            pairs.append(("top print", pal.get(p.colour_refs[0], "") if p.colour_refs else "", "top base", pal.get(c.top.base_ref, "")))
        for n1, h1, n2, h2 in pairs:
            if h1 and h2:
                d = _de(h1, h2)
                if d < need:
                    msgs.append(f"{ck.upper()}: {n1} and {n2} differ by only dE2000 {d:.1f}")
    R.add("CHK-G0-12", not msgs, "adjacent_colour_contrast", threshold=describe("pln.adjacent_de_min", ">="), evidence=_short(msgs),
          paths=["/a/top/base_ref"], messages=msgs)
    mats: list[str] = []
    for ck, c in _chars(spec):
        for part in ("top", "bottom"):
            fid = getattr(c, part).fabric_id
            fab = inv.fabrics.get(fid)
            if fab and fab.material and fab.material != spec.world.material_family:
                mats.append(f"{ck.upper()} {part} fabric {fid} is {fab.material}, the world material is {spec.world.material_family}")
    R.add("CHK-G0-12", not mats, "fabric_vs_world_material", threshold="fabric material family equals world.material_family",
          evidence=_short(mats), paths=["/world/material_family"], messages=mats)
    small_back = [f"/{ck}/accessories/{i}" for ck, c in _chars(spec) for i, a in enumerate(c.accessories) if a.size_class == "small" and a.category == "back"]
    R.add("CHK-G0-12", not small_back, "accessory_visible_at_phone_size", threshold="not a small back item", evidence=_short(small_back),
          paths=small_back, messages=["a small back item will not be visible at phone size"])
    a, b = spec.a.hair, spec.b.hair
    if a.kit_style_id != HAIR_CUSTOM and b.kit_style_id != HAIR_CUSTOM and a.kit_style_id != b.kit_style_id:
        iou = inv.hair_iou(a.kit_style_id, b.kit_style_id)
        if iou is None:
            R.add("PLN-15", True, "kit_hair_pair_iou", evidence="no precomputed IoU for this pair")
        else:
            R.add("PLN-15", iou <= thr("pln.kit_hair_iou_warn"), "kit_hair_pair_iou", value=iou, threshold=describe("pln.kit_hair_iou_warn", "<="),
                  evidence=f"silhouette IoU {iou:.2f}", paths=["/b/hair/kit_style_id"], messages=["the two hairstyles have similar outlines"])
    else:
        R.add("PLN-15", True, "kit_hair_pair_iou", evidence="hair_custom: the 2D silhouette is measured at Gate 2")
    near = nearest_card_share(spec, ctx.recent_cards)
    R.add("CHK-G0-12", near is None or near < thr("pln.nearest_card_share"), "nearest_past_card", value=near,
          threshold=describe("pln.nearest_card_share", "<"), evidence="no recent cards" if near is None else f"{near:.2f} of the card fields repeat a recent duo",
          paths=["/world"], messages=["this plan is very close to a recent duo"])


_CARD_FIELDS = ("pair_structure", "palette_family", "theme", "a.shape_language", "b.shape_language", "a.motif_object", "b.motif_object",
                "a.hair", "b.hair", "anchor_kinds")


def _card_values(spec_or_card: Any) -> dict[str, Any]:
    if isinstance(spec_or_card, DuoSpec):
        s = spec_or_card
        return {"pair_structure": s.world.pair_structure, "palette_family": s.world.palette_family, "theme": normalise_text(s.world.theme),
                "a.shape_language": s.a.dna.shape_language, "b.shape_language": s.b.dna.shape_language,
                "a.motif_object": normalise_text(s.a.dna.motif_object), "b.motif_object": normalise_text(s.b.dna.motif_object),
                "a.hair": s.a.hair.kit_style_id, "b.hair": s.b.hair.kit_style_id,
                "anchor_kinds": tuple(sorted({a.kind for a in s.shared_anchors}))}
    if isinstance(spec_or_card, dna_mod.DnaCard):
        c = spec_or_card
        return {"pair_structure": c.world.pair_structure, "palette_family": c.world.palette_family, "theme": normalise_text(c.world.theme),
                "a.shape_language": c.a.shape_language, "b.shape_language": c.b.shape_language,
                "a.motif_object": normalise_text(c.a.motif_object), "b.motif_object": normalise_text(c.b.motif_object),
                "a.hair": c.hair_kit.get("a", ""), "b.hair": c.hair_kit.get("b", ""),
                "anchor_kinds": tuple(sorted({a.kind for a in c.anchors}))}
    m = dict(spec_or_card)                                           # the compact dict of ``dna.recent_cards_json``
    a, b = m.get("a", {}), m.get("b", {})
    return {"pair_structure": m.get("pair_structure"), "palette_family": m.get("palette_family"), "theme": normalise_text(m.get("theme", "")),
            "a.shape_language": a.get("shape_language"), "b.shape_language": b.get("shape_language"),
            "a.motif_object": normalise_text(a.get("motif_object", "")), "b.motif_object": normalise_text(b.get("motif_object", "")),
            "a.hair": a.get("hair_kit"), "b.hair": b.get("hair_kit"), "anchor_kinds": tuple(sorted(m.get("anchor_kinds", ())))}


def nearest_card_share(spec: DuoSpec, cards: Sequence[Any]) -> float | None:
    """The largest share of identical card fields between ``spec`` and any recent card (``None`` without cards). SOFT hint only: there is
    no cap, quota or field-combination lint (APP_SPEC §3.7)."""
    if not cards:
        return None
    mine = _card_values(spec)
    best = 0.0
    for c in cards:
        theirs = _card_values(c)
        same = sum(1 for k in _CARD_FIELDS if mine[k] == theirs[k])
        best = max(best, same / len(_CARD_FIELDS))
    return best


# --------------------------------------------------------------------------------------------------- the per-spec entry point
def lint_spec(spec: DuoSpec | Mapping[str, Any], ctx: PlanLintCtx | None = None) -> LintReport:
    """Every per-spec C1 rule. ``spec`` may be a ``DuoSpec`` or its dict (parsed without the spec rules, so that every problem shows)."""
    ctx = ctx or PlanLintCtx()
    if not isinstance(spec, DuoSpec):
        spec = DuoSpec.model_validate(dict(spec), context={"skip_rules": True})
    R = _Rep(ctx.subject_sha or _sha(spec))
    rule_palette_integrity(spec, R)
    rule_lash_iris(spec, R)
    rule_free_text(spec, R, ctx)
    rule_kit_ids(spec, R, ctx)
    rule_roblox(spec, R, ctx)
    rule_combo(spec, R)
    tally = rule_contrasts(spec, R, ctx)
    rule_face_grammar(spec, R)
    rule_hair_pairing(spec, R)
    rule_accessory_complement(spec, R)
    rule_garment_cut(spec, R, ctx)
    rule_dna(spec, R)
    rule_profile_contrast(spec, R, tally, ctx.inv())
    rule_registry(spec, R, ctx)
    other = spec.world.pair_structure == "other"
    probs = [p for p in spec_rules.spec_problems(spec) if p.code == "structure_note"]
    R.add("PLN-STR-01", not probs and (not other or bool(spec.world.structure_note.strip())), "structure_note",
          evidence=_short(map(str, probs)) or "ok", paths=[p.path for p in probs] or ["/world/structure_note"], messages=[p.message for p in probs])
    other_probs = [p for p in spec_rules.spec_problems(spec) if p.rule in ("counts", "completeness") and p.code not in ("structure_note", "palette_count")
                   and p.code not in ("anchor_count", "contrast_count")]
    R.add("CHK-G0-03", not other_probs, "schema_counts", value=len(other_probs), threshold="0 problems", evidence=_short(map(str, other_probs)),
          paths=[p.path for p in other_probs], messages=[p.message for p in other_probs])
    rule_structure_colours(spec, R)
    rule_restraint(spec, R)
    rule_detail_range(spec, R)
    rule_soft_misc(spec, R, ctx)
    return R.rep


# --------------------------------------------------------------------------------------------------- plan-set rules
def _resolve_pointer(doc: Any, pointer: str) -> bool:
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    node = doc
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return False
    return True


def _norm_line(text: str) -> str:
    return normalise_text(text)


def lint_plan_set(specs: Sequence[DuoSpec], ctx: PlanLintCtx | None = None, brief_constraints: Sequence[Any] | None = None,
                  how_they_differ: str = "") -> LintReport:
    """Plan-set rules (C1 #2 and #20): exactly 3 specs and exactly 1 wildcard (HARD, always); the structure the brief form named; the
    must-include lines mapped to paths that resolve in every spec; and the SOFT distribution rules (APP_SPEC §2 S18)."""
    ctx = ctx or PlanLintCtx()
    R = _Rep(ctx.subject_sha or "")
    cfg = _rules_plan()["plan_set"]
    n = len(specs)
    R.add("CHK-G0-07", n == cfg["specs"], "plan_set_size", value=n, threshold=f"exactly {cfg['specs']} specs", evidence=f"{n} specs",
          paths=["/specs"], messages=[f"the plan set needs exactly {cfg['specs']} specs"])
    wild = [i for i, s in enumerate(specs) if s.is_wildcard]
    R.add("CHK-G0-07", len(wild) == cfg["wildcards"], "wildcard_count", value=len(wild), threshold=f"exactly {cfg['wildcards']} wildcard",
          evidence=f"wildcards: {wild}", paths=["/specs"], messages=[f"exactly {cfg['wildcards']} spec must have is_wildcard true, also when the brief fixes the structure"])
    req = (ctx.structure_request or "auto").strip().lower()
    structures = [s.world.pair_structure for s in specs]
    if req != "auto":
        off = [i for i, st in enumerate(structures) if st != req]
        R.add("PLN-STR-01", not off, "structure_request", value=len(off), threshold=f"all specs use {req}", evidence=f"specs off the requested structure: {off}",
              paths=[f"/specs/{i}/world/pair_structure" for i in off], messages=[f"the brief form named {req}: every spec must use it (the wildcard too)"] * len(off))
    else:
        R.add("PLN-STR-01", True, "structure_request", evidence="auto: the structure mix is a planner instruction, not a rule")
    # ---- must-include lines (C1 #20) ----------------------------------------------------------
    wanted = [m for m in ctx.must_include if str(m).strip()]
    constraints = list(brief_constraints or [])
    docs = [s.model_dump(mode="json") for s in specs]
    bad: list[str] = []
    for line in wanted:
        want = _norm_line(line)
        hit = next((c for c in constraints if want and (want in _norm_line(c.text) or _norm_line(c.text) in want)), None)
        if hit is None:
            bad.append(f"must-include line {line!r} is missing from brief_constraints")
    for k, c in enumerate(constraints):
        if not c.spec_paths:
            bad.append(f"brief_constraints/{k} has no spec path")
        for p in c.spec_paths:
            for i, d in enumerate(docs):
                if not _resolve_pointer(d, p):
                    bad.append(f"brief_constraints/{k}: path {p} does not resolve in spec {i}")
    R.add("CHK-G0-07", not bad, "brief_constraints", value=len(bad), threshold="every must-include line maps to paths in all specs",
          evidence=_short(bad), paths=["/brief_constraints"] * max(1, len(bad)), messages=bad or [""])
    # ---- soft distribution ---------------------------------------------------------------------
    _soft_distribution(specs, req, R)
    cap = int(spec_rules.WORD_CAPS["plan.how_they_differ"])
    if how_they_differ:
        probs = freetext.free_text_problems(how_they_differ, cap, ctx.bans(), strict=False)
        R.add("CHK-G0-08", not probs, "how_they_differ", evidence=_short(map(str, probs)), paths=["/how_they_differ"], messages=[str(p) for p in probs])
    return R.rep


def _soft_distribution(specs: Sequence[DuoSpec], req: str, R: _Rep) -> None:
    msgs: list[str] = []
    structures = [s.world.pair_structure for s in specs]
    if req == "auto" and len(specs) > 1 and len(set(structures)) < len(specs):
        counts = Counter(structures)
        msgs.append(f"structure mix {dict(counts)}: an open brief asks for different structures (a planner instruction)")
    for i in range(len(specs)):
        for j in range(i + 1, len(specs)):
            a, b = specs[i], specs[j]
            same = (a.world.palette_family == b.world.palette_family
                    and {x.kind for x in a.shared_anchors} == {x.kind for x in b.shared_anchors}
                    and (req != "auto" or a.world.pair_structure == b.world.pair_structure))
            if same:
                msgs.append(f"specs {i} and {j} differ in neither palette family nor anchor kinds" + (" nor structure" if req == "auto" else ""))
    wild = [s for s in specs if s.is_wildcard]
    if len(wild) == 1:
        w = wild[0]
        for o in specs:
            if o is w:
                continue
            departs = (w.world.palette_family != o.world.palette_family or {x.kind for x in w.shared_anchors} != {x.kind for x in o.shared_anchors}
                       or normalise_text(w.world.theme) != normalise_text(o.world.theme))
            if not departs:
                msgs.append("the wildcard does not depart from another plan in palette family, anchor kind or theme")
    R.add("CHK-G0-12", not msgs, "plan_set_distribution", value=len(msgs), threshold="plans differ pairwise; the wildcard departs",
          evidence=_short(msgs), paths=["/specs"], messages=msgs)


def lint_plan(plan: PlanSet | Mapping[str, Any], ctx: PlanLintCtx | None = None) -> PlanReport:
    """C1 for a whole ``PlanSet``: the plan-set rules plus every per-spec rule. Parses without the spec rules so that every problem is
    reported at once (the strict parse is ``models.spec.parse_plan_set``)."""
    ctx = ctx or PlanLintCtx()
    if not isinstance(plan, PlanSet):
        plan = PlanSet.model_validate(dict(plan), context={"skip_rules": True})
    set_rep = lint_plan_set(plan.specs, ctx, plan.brief_constraints, plan.how_they_differ)
    return PlanReport(per_spec=[lint_spec(s, ctx) for s in plan.specs], set_report=set_rep)


# --------------------------------------------------------------------------------------------------- A_LEAK, spec side
@dataclass(frozen=True)
class LeakSets:
    """The colour sets of A_LEAK (CHK-G1-04) and the L11 rule ``dj_no_leak`` for one character (APP_SPEC §3.3)."""

    own_hex: list[str]
    partner_hex: list[str]
    shared_hex: list[str]
    partner_only_hex: list[str]
    exempt_roles: list[str]


def leak_sets(spec: DuoSpec, character: str, structure: str | None = None) -> LeakSets:
    """Partner-only colours of ``character``: the partner's role colours more than ``con.partner_only_de`` (dE2000 12) from **every**
    own role colour and shared anchor colour, minus the colours the structure profile shares on purpose (``same_club`` shares a main,
    ``mirror`` swaps roles). A ``complement`` pair in which A wears B's main still fails; a same-club pair that shares a main passes."""
    if character not in CHARS:
        raise ValueError("character must be a or b")
    partner = "b" if character == "a" else "a"
    structure = structure or spec.world.pair_structure
    pol = profile(structure)["leak_policy"]
    exempt: set[str] = set()
    for key in ("shared_roles", "swapped_roles"):
        for pair in pol.get(key, ()):
            exempt.update(pair)
    own = [h for r in OWN_ROLES[character] if (h := role_hex(spec, r))]
    other = [(r, h) for r in OWN_ROLES[partner] if (h := role_hex(spec, r))]
    shared = [h for r in SHARED_ROLES if (h := role_hex(spec, r))]
    min_de = float(thr("con.partner_only_de"))
    only = [h for r, h in other if r not in exempt and all(_de(h, o) > min_de for o in (*own, *shared))]
    return LeakSets(own_hex=own, partner_hex=[h for _, h in other], shared_hex=shared, partner_only_hex=only, exempt_roles=sorted(exempt))


# --------------------------------------------------------------------------------------------------- patch scope (CHK-G0-11)
FORBIDDEN_CHANGE_PATHS = ("/combo", "/is_wildcard", "/a/presentation", "/b/presentation")


def patch_scope_problems(ops: Sequence[Any], *, finding_paths: Sequence[str] | None = None, palette_ids: Iterable[str] = ()) -> list[str]:
    """CHK-G0-11 (ASSERT): which patch ops step outside their allowed paths.

    ``finding_paths`` (the reviser, L6): an op may touch only a path named by a finding, or a child of one, or the palette entry that a
    finding references. Without ``finding_paths`` (the change interpreter, L7) any path is allowed except ``/combo``, ``/is_wildcard``,
    ``/a/presentation``, ``/b/presentation`` and a palette **id** (hexes may change).
    """
    bad: list[str] = []
    ids = set(palette_ids)
    for i, op in enumerate(ops):
        p = op.path
        if finding_paths is None:
            if p in FORBIDDEN_CHANGE_PATHS or any(p.startswith(f + "/") for f in FORBIDDEN_CHANGE_PATHS):
                bad.append(f"op {i}: {p} may not be changed by a change request")
            m = re.fullmatch(r"/palette/(\d+)/id", p)
            if m:
                bad.append(f"op {i}: palette ids stay stable ({p})")
        else:
            allowed = any(p == f or p.startswith(f.rstrip("/") + "/") for f in finding_paths)
            if not allowed and not (re.fullmatch(r"/palette/\d+(/.*)?", p) and ids):
                bad.append(f"op {i}: {p} is not named by a finding")
    return bad


__all__ = ["Finding", "LintReport", "PlanReport", "PlanLintCtx", "lint_spec", "lint_plan_set", "lint_plan", "leak_sets", "LeakSets",
           "measure_axis", "tally_contrasts", "season_group", "profile", "nearest_card_share", "patch_scope_problems",
           "CONTRAST_AXES", "ACCESSORY_ATTACHMENTS"]
