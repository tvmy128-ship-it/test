"""Dependency graph, invalidation and the two approval stamps (APP_SPEC §9.7, §9.8, plus the issue-list fix).

Public interface (APP_SPEC §5.4)::

    affected_parts(old, new, redo=(), *, existing_parts=None, gate3_open=False) -> InvalidationReport
    dna_field_users(field, character, *, spec=None, parts=None) -> list[PartId]

Specs are plain JSON dicts here (``DuoSpec.model_dump(mode="json")``); the engine never imports ``models.spec``.

Two stamps per part (issue file, ENG-01):

* ``approval_hash`` (Gate 2): spec slice of the part's own output-affecting paths, input shas, **board** outputs, prompt
  ids and hashes, model snapshots, kit subset, house-style version. It does *not* cover ``build_assets``.
* ``build_hash`` (``Part.build_stamp``; written when the part reaches BUILT, confirmed by the Gate 3 pick): the
  ``approval_hash`` it was built under, the sorted build asset shas and the BUILD step versions. CHK-D09 / CHK-E02
  compare each stamp with its own value, so a rebuilt mesh needs a Gate 3 look ("rebuilt since you last looked") but
  never a Gate 2 re-approval.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import Field

from duoskin.models.common import PartId, Strict, iso_utc, sha256_of, utcnow
from duoskin.models.part import ApprovalRecord, BuildStamp, DepEffect, DepRule, Part, PartState
from duoskin.models.project import VersionPins

if TYPE_CHECKING:
    from duoskin.db.repo import Repo

_EFFECT_ORDER = {DepEffect.RECHECK: 0, DepEffect.RECOMPOSE: 1, DepEffect.REGENERATE: 2}
_C = r"(?P<c>[ab])"
_TAIL = r"(?:/.*)?$"


# ---------------------------------------------------------------------------------------------------- JSON pointers
def _unescape(seg: str) -> str:
    return seg.replace("~1", "/").replace("~0", "~")


def _escape(seg: str) -> str:
    return seg.replace("~", "~0").replace("/", "~1")


def resolve(doc: Any, pointer: str, default: Any = None) -> Any:
    """RFC 6901 lookup; ``default`` when the pointer does not resolve."""
    if pointer in ("", "/"):
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        seg = _unescape(raw)
        if isinstance(cur, dict):
            if seg not in cur:
                return default
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def changed_pointers(old: Any, new: Any, base: str = "") -> list[str]:
    """Pointers of every leaf (or added/removed subtree) that differs between ``old`` and ``new``."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for key in sorted(set(old) | set(new)):
            ptr = f"{base}/{_escape(str(key))}"
            if key not in old or key not in new:
                out.append(ptr)
            else:
                out += changed_pointers(old[key], new[key], ptr)
        return out
    if isinstance(old, list) and isinstance(new, list):
        out = []
        for i in range(max(len(old), len(new))):
            ptr = f"{base}/{i}"
            if i >= len(old) or i >= len(new):
                out.append(ptr)
            else:
                out += changed_pointers(old[i], new[i], ptr)
        return out
    return [] if old == new else [base or "/"]


def expand_pattern(doc: Any, pattern: str, character: str | None = None) -> list[str]:
    """Concrete pointers matching a ``DepRule.pattern``: ``{c}`` is the part's character, ``*`` matches one segment.

    A non-glob pointer that does not exist in ``doc`` is still returned (its value hashes as ``None``), so adding or
    removing a field changes a part's spec slice."""
    pattern = pattern.replace("{c}", character or "")
    segs = [s for s in pattern.split("/") if s != ""]
    results: list[tuple[str, Any]] = [("", doc)]
    for seg in segs:
        nxt: list[tuple[str, Any]] = []
        for ptr, node in results:
            if seg == "*":
                if isinstance(node, dict):
                    nxt += [(f"{ptr}/{_escape(str(k))}", v) for k, v in node.items()]
                elif isinstance(node, list):
                    nxt += [(f"{ptr}/{i}", v) for i, v in enumerate(node)]
            else:
                child = node.get(_unescape(seg)) if isinstance(node, dict) else None
                if isinstance(node, list):
                    try:
                        child = node[int(seg)]
                    except (ValueError, IndexError):
                        child = None
                nxt.append((f"{ptr}/{seg}", child))
        results = nxt
    return [p for p, _ in results]


def part_dep_paths(part: Part, spec: Any) -> list[str]:
    paths: list[str] = []
    for rule in part.deps:
        for p in expand_pattern(spec, rule.pattern, None if part.character == "duo" else part.character):
            if p not in paths:
                paths.append(p)
    return sorted(paths)


# ---------------------------------------------------------------------------------------------------- the §9.8 table
@dataclass(frozen=True)
class DepRow:
    regex: re.Pattern[str]
    parts: tuple[tuple[str, DepEffect], ...]     # (part id template using {c} and {i}, effect)
    label: str


def _row(rx: str, parts: Sequence[tuple[str, DepEffect]], label: str) -> DepRow:
    return DepRow(re.compile(rx), tuple(parts), label)


R, C_, K = DepEffect.REGENERATE, DepEffect.RECOMPOSE, DepEffect.RECHECK

# First match wins, so the specific rows come before the general ones. Source: APP_SPEC §9.8.
DEP_TABLE: tuple[DepRow, ...] = (
    _row(rf"^/{_C}/face/(?:iris_style|lash_style|brow_style|mouth_style){_TAIL}", [("{c}.face", R)], "face style (AI parts)"),
    _row(rf"^/{_C}/face/(?:highlight_style|nose_style|cheek_mark|default_expression|eye_shape){_TAIL}", [("{c}.face", C_)],
         "face code layers"),
    _row(rf"^/{_C}/face/[a-z_]*_ref{_TAIL}", [("{c}.face", C_)], "face colour refs"),
    _row(rf"^/{_C}/hair/(?:kit_style_id|fringe_id|back_id|description|parting){_TAIL}", [("{c}.hair", R)], "hair shape"),
    _row(rf"^/{_C}/hair/(?:colour_ref|shadow_ref|highlight_ref){_TAIL}", [("{c}.hair", C_)], "hair colour"),
    _row(rf"^/{_C}/top/prints/(?P<i>\d+)/motif{_TAIL}", [("{c}.print.top.{i}", R), ("{c}.shirt", C_)], "top print motif"),
    _row(rf"^/{_C}/top/prints/(?P<i>\d+)/(?:region|scale){_TAIL}", [("{c}.shirt", C_)], "top print placement"),
    _row(rf"^/{_C}/top/prints/(?P<i>\d+)$", [("{c}.print.top.{i}", R), ("{c}.shirt", C_)], "top print added or removed"),
    _row(rf"^/{_C}/top{_TAIL}", [("{c}.shirt", C_)], "top recipe, fabric and colours"),
    _row(rf"^/{_C}/bottom/shoes/motif{_TAIL}", [("{c}.print.shoes.0", R), ("{c}.pants", C_)], "shoe motif"),
    _row(rf"^/{_C}/bottom/prints/(?P<i>\d+)/motif{_TAIL}", [("{c}.print.bottom.{i}", R), ("{c}.pants", C_)],
         "bottom print motif"),
    _row(rf"^/{_C}/bottom/prints/(?P<i>\d+)$", [("{c}.print.bottom.{i}", R), ("{c}.pants", C_)], "bottom print added or removed"),
    _row(rf"^/{_C}/bottom{_TAIL}", [("{c}.pants", C_)], "bottom, shoes and legwear"),
    _row(rf"^/{_C}/accessories/(?P<i>\d+)/(?:description|kind|material|build){_TAIL}", [("{c}.acc.{i}", R)], "accessory art"),
    _row(rf"^/{_C}/accessories/(?P<i>\d+)/(?:size_class|attachment|category){_TAIL}", [("{c}.acc.{i}", K)],
         "accessory scale and fit"),
    _row(rf"^/{_C}/accessories/(?P<i>\d+)$", [("{c}.acc.{i}", R)], "accessory added or removed"),
    _row(rf"^/{_C}/body/skin_tone{_TAIL}", [("{c}.colours", C_), ("{c}.face", K)], "skin tone"),
    _row(rf"^/{_C}/body/modesty_ref{_TAIL}", [("{c}.colours", C_)], "modesty colour"),
)
# Rows whose effect depends on which parts exist (DNA fields route to every tile that uses them).
_DNA_SHAPE = re.compile(rf"^/{_C}/dna/shape_language{_TAIL}")
_DNA_MOTIF = re.compile(rf"^/{_C}/dna/motif_object{_TAIL}")
_DNA_LINT_ONLY = re.compile(rf"^/{_C}/dna/(?:colour_plan|focal_location|accessory_style|energy){_TAIL}")
_WORLD_DETAIL = re.compile(rf"^/world/detail_level{_TAIL}")
_HAIR_ANY = re.compile(rf"^/{_C}/hair{_TAIL}")
_FACE_STYLE = re.compile(rf"^/{_C}/face/(?P<f>iris_style|lash_style|brow_style|mouth_style)(?:/.*)?$")
# Which AI face parts a style field regenerates (the tile's ``GateDecisionIn.target`` uses the same names, §6.9).
FACE_STYLE_TARGETS: dict[str, tuple[str, ...]] = {
    "iris_style": ("iris",), "lash_style": ("lash",), "brow_style": ("brow",), "mouth_style": ("mouth_closed", "mouth_open"),
}
# ``/world/detail_level`` regenerates only these face parts: lash, brow and mouth_closed carry shape language only (bible §3.3).
DETAIL_LEVEL_FACE_TARGETS: tuple[str, ...] = ("iris", "mouth_open")
HAIR_RECHECK_ACCESSORY_CATEGORIES = frozenset({"hat", "hair", "face"})
_WORLD_LINT_ONLY = re.compile(rf"^/(?:world/(?:material_family|theme|story|pair_structure|structure_note|palette_family)|"
                              rf"shared_anchors|contrasts){_TAIL}")
_NOT_PATCHABLE = re.compile(r"^/(?:combo|is_wildcard|[ab]/presentation|palette/\d+/id)(?:/.*)?$")
_PALETTE_HEX = re.compile(r"^/palette/(?P<n>\d+)/hex$")

_PRINT_ID = re.compile(r"^[ab]\.print\.")
_SHAPE_PARTS = ("face", "hair")


def default_parts(spec: dict[str, Any]) -> list[str]:
    """Parts that exist for ``spec`` by the §6.5 rules (a heuristic: pass the real part ids when you have them)."""
    out: list[str] = []
    for c in ("a", "b"):
        ch = spec.get(c) if isinstance(spec, dict) else None
        if not isinstance(ch, dict):
            continue
        out += [f"{c}.colours", f"{c}.face", f"{c}.hair", f"{c}.shirt", f"{c}.pants"]
        accs = ch.get("accessories")
        out += [f"{c}.acc.{i}" for i in range(len(accs) if isinstance(accs, list) else 0)]
        for slot, key in (("top", "top"), ("bottom", "bottom")):
            prints = (ch.get(key) or {}).get("prints") if isinstance(ch.get(key), dict) else None
            out += [f"{c}.print.{slot}.{i}" for i in range(len(prints) if isinstance(prints, list) else 0)]
        shoes = (ch.get("bottom") or {}).get("shoes") if isinstance(ch.get("bottom"), dict) else None
        if isinstance(shoes, dict) and shoes.get("motif") not in (None, "", "none"):
            out.append(f"{c}.print.shoes.0")
    return out


def dna_field_users(field_name: str, character: str, *, spec: dict[str, Any] | None = None,
                    parts: Iterable[str] | None = None) -> list[PartId]:
    """Parts of ``character`` whose templates route ``field_name`` (bible §3.3): ``shape_language`` feeds prints, face
    parts, the hair front view, accessory front views and badges; ``motif_object`` feeds accessories and badges (I5,
    I6); ``detail_level`` (a WORLD field) feeds prints and face parts of both characters. Pass ``parts`` (the project's
    part ids) or ``spec`` so only parts that exist are returned."""
    existing = list(parts) if parts is not None else default_parts(spec or {})
    chars = ("a", "b") if field_name == "detail_level" else (character,)
    out: list[str] = []
    for c in chars:
        mine = [p for p in existing if p.startswith(f"{c}.")]
        if field_name == "shape_language":
            out += [p for p in mine if _PRINT_ID.match(p) or p in (f"{c}.face", f"{c}.hair") or ".acc." in p]
        elif field_name == "motif_object":
            out += [p for p in mine if ".acc." in p]
        elif field_name == "detail_level":
            out += [p for p in mine if _PRINT_ID.match(p) or p == f"{c}.face"]
    return sorted(set(out))   # type: ignore[return-value]


class PartEffect(Strict):
    part_id: str
    effect: DepEffect
    reasons: list[str] = Field(default_factory=list)       # changed spec paths (or "redo")
    targets: list[str] = Field(default_factory=list)       # face tile only: the AI parts to redo; empty = the whole part
    estimate_usd: float | None = None


class InvalidationReport(Strict):
    parts: list[PartEffect] = Field(default_factory=list)
    pair_rechecks: list[str] = Field(default_factory=list)  # partner parts that only re-run pair-dependent checks (§2 S21)
    duo_stale: bool = False
    lint_only: list[str] = Field(default_factory=list)      # paths that only re-run the plan lint and SOFT warnings
    not_patchable: list[str] = Field(default_factory=list)  # paths that cannot change after Gate 1 ("New plan" instead)
    changed_paths: list[str] = Field(default_factory=list)
    estimate_usd: float | None = None

    def effect_of(self, part_id: str) -> DepEffect | None:
        for p in self.parts:
            if p.part_id == part_id:
                return p.effect
        return None

    def by_effect(self, effect: DepEffect) -> list[str]:
        return [p.part_id for p in self.parts if p.effect == effect]


def _normalise_redo(redo: Iterable[Any]) -> list[tuple[str, DepEffect]]:
    out: list[tuple[str, DepEffect]] = []
    for item in redo or ():
        if isinstance(item, str):
            out.append((item, DepEffect.REGENERATE))
            continue
        pid = item.get("part_id") if isinstance(item, dict) else getattr(item, "part_id", None)
        eff = item.get("effect") if isinstance(item, dict) else getattr(item, "effect", None)
        if pid:
            out.append((str(pid), DepEffect(str(eff)) if eff else DepEffect.REGENERATE))
    return out


def _palette_id(spec: dict[str, Any], index: int) -> str | None:
    pal = spec.get("palette") if isinstance(spec, dict) else None
    if isinstance(pal, list) and 0 <= index < len(pal) and isinstance(pal[index], dict):
        return pal[index].get("id")
    return None


def _ref_locations(spec: dict[str, Any], palette_id: str) -> list[str]:
    """Pointers of every string value equal to ``palette_id`` outside the palette itself."""
    found: list[str] = []

    def walk(node: Any, ptr: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if ptr == "" and k == "palette":
                    continue
                walk(v, f"{ptr}/{_escape(str(k))}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{ptr}/{i}")
        elif node == palette_id:
            found.append(ptr)

    walk(spec, "")
    return found


def _palette_part_effects(path_ptr: str, c: str, i: str | None) -> list[tuple[str, DepEffect]]:
    """Which part a spec location under ``/<c>/...`` feeds, with the §9.8 palette effect."""
    segs = path_ptr.split("/")[1:]
    if len(segs) < 2:
        return []
    group = segs[1]
    if group == "top":
        out = [(f"{c}.shirt", C_)]
        if len(segs) > 3 and segs[2] == "prints":
            out.append((f"{c}.print.top.{segs[3]}", K))
        return out
    if group == "bottom":
        out = [(f"{c}.pants", C_)]
        if len(segs) > 3 and segs[2] == "prints":
            out.append((f"{c}.print.bottom.{segs[3]}", K))
        if len(segs) > 2 and segs[2] == "shoes":
            out.append((f"{c}.print.shoes.0", K))
        return out
    if group == "hair":
        return [(f"{c}.hair", C_)]
    if group == "face":
        return [(f"{c}.face", C_)]
    if group == "body":
        return [(f"{c}.colours", C_)]
    if group == "accessories" and len(segs) > 2:
        return [(f"{c}.acc.{segs[2]}", K)]
    return []


def affected_parts(old: dict[str, Any], new: dict[str, Any], redo: Iterable[Any] = (), *,
                   existing_parts: Iterable[str] | None = None, gate3_open: bool = False,
                   approved_parts: Iterable[str] | None = None,
                   estimator: Callable[[PartEffect], float] | None = None) -> InvalidationReport:
    """Diff two specs path by path and apply the §9.8 table. ``redo`` (the L7 ``redo_parts``) is merged in (union).

    ``existing_parts``: the project's part ids (the pipeline passes ``[p.id for p in repo.list_parts(...)]``); without it
    the parts are derived from ``new`` with ``default_parts``. ``approved_parts``: the part ids whose Gate 2 approval
    exists; a hair change re-checks the hat/hair/face accessories only once the hair is approved (``None`` = assume it).
    ``estimator`` turns each affected part into USD."""
    existing = list(existing_parts) if existing_parts is not None else default_parts(new)
    effects: dict[str, PartEffect] = {}
    lint_only: list[str] = []
    not_patchable: list[str] = []
    changed_chars: set[str] = set()
    paths = changed_pointers(old, new)

    whole: set[str] = set()      # parts that some path redoes entirely (no face-part targets)

    def add(part_id: str, effect: DepEffect, reason: str, targets: Sequence[str] | None = None) -> None:
        cur = effects.get(part_id)
        if cur is None:
            cur = effects[part_id] = PartEffect(part_id=part_id, effect=effect, reasons=[reason])
        else:
            if _EFFECT_ORDER[effect] > _EFFECT_ORDER[cur.effect]:
                cur.effect = effect
            if reason not in cur.reasons:
                cur.reasons.append(reason)
        if targets:
            cur.targets = sorted({*cur.targets, *targets})
        elif effect != DepEffect.RECHECK:
            whole.add(part_id)

    approved = set(approved_parts) if approved_parts is not None else None
    accessories_of = {c: (new.get(c) or {}).get("accessories") if isinstance(new, dict) else None for c in ("a", "b")}

    for path in paths:
        if _NOT_PATCHABLE.match(path):
            not_patchable.append(path)
            continue
        m = _PALETTE_HEX.match(path)
        if m:
            pid = _palette_id(new, int(m.group("n"))) or _palette_id(old, int(m.group("n")))
            for loc in _ref_locations(new, pid) if pid else []:
                lm = re.match(r"^/(?P<c>[ab])/", loc)
                if lm:
                    for part_id, eff in _palette_part_effects(loc, lm.group("c"), None):
                        add(part_id, eff, path)
            continue
        hm = _HAIR_ANY.match(path)
        if hm and (approved is None or f"{hm.group('c')}.hair" in approved):
            hc = hm.group("c")
            accs = accessories_of.get(hc)
            for idx, acc in enumerate(accs if isinstance(accs, list) else []):
                category = acc.get("category") if isinstance(acc, dict) else None
                if category in HAIR_RECHECK_ACCESSORY_CATEGORIES and f"{hc}.acc.{idx}" in existing:
                    add(f"{hc}.acc.{idx}", K, path)     # the scale tile is re-rendered with the new hair; no new art
        handled = False
        for row in DEP_TABLE:
            m = row.regex.match(path)
            if m:
                gd = m.groupdict()
                c, i = gd.get("c", ""), gd.get("i", "")
                changed_chars.add(c)
                fm = _FACE_STYLE.match(path)
                targets = FACE_STYLE_TARGETS[fm.group("f")] if fm else None
                for template, eff in row.parts:
                    add(template.format(c=c, i=i), eff, path, targets if template.endswith(".face") else None)
                handled = True
                break
        if handled:
            continue
        m = _DNA_SHAPE.match(path)
        if m:
            c = m.group("c")
            changed_chars.add(c)
            for pid in dna_field_users("shape_language", c, parts=existing):
                add(pid, R, path)
            continue
        m = _DNA_MOTIF.match(path)
        if m:
            c = m.group("c")
            changed_chars.add(c)
            for pid in dna_field_users("motif_object", c, parts=existing):
                add(pid, R, path)
            continue
        if _WORLD_DETAIL.match(path):
            for pid in dna_field_users("detail_level", "a", parts=existing):
                add(pid, R, path, DETAIL_LEVEL_FACE_TARGETS if pid.endswith(".face") else None)
            continue
        if _DNA_LINT_ONLY.match(path) or _WORLD_LINT_ONLY.match(path):
            lint_only.append(path)
            continue
        lint_only.append(path)   # an unknown path never silently invalidates parts: it re-lints and is reported

    for part_id, eff in _normalise_redo(redo):
        add(part_id, eff, "redo")
        changed_chars.add(part_id.split(".")[0])

    for pid in whole:
        effects[pid].targets = []
    # a REGENERATE of a built part drops its build assets; the effect list already says so. Pair re-checks:
    pair: set[str] = set()
    changed_plain = {c for c in changed_chars if c in ("a", "b")}
    for c in changed_plain:
        partner = "b" if c == "a" else "a"
        for pid in existing:
            if pid.startswith(f"{partner}.") and not pid.startswith(f"{partner}.print.") and pid != f"{partner}.colours" \
                    and pid not in effects:
                pair.add(pid)

    parts = sorted(effects.values(), key=lambda e: (-_EFFECT_ORDER[e.effect], e.part_id))
    total: float | None = None
    if estimator is not None:
        for p in parts:
            p.estimate_usd = round(float(estimator(p)), 6)
        total = round(sum(p.estimate_usd or 0.0 for p in parts), 6)
    return InvalidationReport(parts=parts, pair_rechecks=sorted(pair), duo_stale=gate3_open and bool(paths or effects),
                              lint_only=lint_only, not_patchable=not_patchable, changed_paths=paths, estimate_usd=total)


# ---------------------------------------------------------------------------------------------------- the two stamps
@dataclass
class ApprovalFacts:
    """What the part's own provenance says went into it (inputs, prompts, models, kit subset)."""

    input_shas: list[str] = field(default_factory=list)
    prompts: list[list[Any]] = field(default_factory=list)       # [prompt_id, prompt_version, prompt_sha256]
    models: list[str] = field(default_factory=list)
    kit_subset_sha: str | None = None


def collect_facts(repo: Repo, part: Part) -> ApprovalFacts:
    """Read the facts of a part's *board* assets from the provenance of their asset links."""
    shown = set(part.board_assets.values())
    inputs: set[str] = set()
    prompts: set[tuple[Any, ...]] = set()
    models: set[str] = set()
    kits: set[str] = set()
    for link in repo.list_links(project_id=part.project_id, part_id=part.id):
        if link.asset_sha not in shown:
            continue
        prov = link.provenance
        inputs.update(prov.input_shas)
        if prov.mask_sha:
            inputs.add(prov.mask_sha)
        if prov.prompt_id:
            prompts.add((prov.prompt_id, prov.prompt_version, prov.prompt_sha256))
        if prov.model:
            models.add(prov.model)
        ks = prov.params.get("kit_subset_sha")
        if isinstance(ks, str):
            kits.add(ks)
    return ApprovalFacts(input_shas=sorted(inputs), prompts=[list(p) for p in sorted(prompts, key=str)],
                         models=sorted(models), kit_subset_sha=",".join(sorted(kits)) or None)


def spec_slice(part: Part, spec: Any) -> dict[str, Any]:
    return {p: resolve(spec, p) for p in part_dep_paths(part, spec)}


def pins_sha(pins: VersionPins | None) -> str:
    return sha256_of(pins.model_dump(mode="json") if pins else {})


def approval_hash(part: Part, spec: Any, pins: VersionPins | None, facts: ApprovalFacts | None = None) -> str:
    """The Gate 2 stamp (§9.7): spec slice, inputs, **board** outputs, prompts, models, kit subset, house style."""
    facts = facts or ApprovalFacts()
    return sha256_of({
        "part_id": part.id,
        "spec_slice": spec_slice(part, spec),
        "inputs": sorted(facts.input_shas),
        "outputs": sorted(part.board_assets.values()),          # never build_assets: they do not exist at Gate 2
        "prompts": facts.prompts,
        "models": sorted(facts.models),
        "kit_subset_sha": facts.kit_subset_sha,
        "house_style_version": pins.house_style_version if pins else None,
    })


def part_build_step_versions(repo: Repo, part: Part) -> list[list[Any]]:
    """Handler versions and params of the BUILD steps that made the part's build assets (from their provenance):
    ``[[step_kind, handler_version, sha256_of(params)], ...]``, sorted."""
    built = set(part.build_assets.values())
    seen: set[tuple[Any, ...]] = set()
    for link in repo.list_links(project_id=part.project_id, part_id=part.id):
        if link.asset_sha in built:
            prov = link.provenance
            seen.add((prov.step_kind or "", prov.handler_version, sha256_of(prov.params)))
    return [list(t) for t in sorted(seen, key=str)]


def build_hash(part: Part, build_steps: Sequence[Any] = ()) -> str:
    """Stamp 2 (§9.7): the ``approval_hash`` the part was built under, the sorted build asset shas and the BUILD step
    versions. A rebuilt mesh changes it without touching the approval; it never reads ``board_assets``."""
    return sha256_of({
        "part_id": part.id,
        "approval_hash": part.approval.approval_hash if part.approval else None,
        "build_assets": sorted(part.build_assets.values()),
        "build_steps": [list(x) if isinstance(x, (list, tuple)) else x for x in build_steps],
    })


def make_approval(part: Part, spec: Any, *, spec_id: str, spec_version: int, pins: VersionPins | None, decision_id: str,
                  facts: ApprovalFacts | None = None) -> ApprovalRecord:
    facts = facts or ApprovalFacts()
    return ApprovalRecord(
        part_id=part.id, approval_hash=approval_hash(part, spec, pins, facts), spec_id=spec_id, spec_version=spec_version,
        spec_slice_sha=sha256_of(spec_slice(part, spec)), input_shas=sorted(facts.input_shas),
        output_shas=sorted(part.board_assets.values()), pins_sha=pins_sha(pins), approved_at=utcnow(),
        decision_id=decision_id)   # type: ignore[arg-type]


class StampCheck(Strict):
    ok: bool
    reason: str = ""
    expected: str | None = None
    actual: str | None = None


def check_approval(part: Part, spec: Any, pins: VersionPins | None, facts: ApprovalFacts | None = None) -> StampCheck:
    """CHK-D09 / CHK-E02, first stamp: does the part still match what the user approved at Gate 2?"""
    if part.approval is None:
        return StampCheck(ok=False, reason="the part has no approval")
    now = approval_hash(part, spec, pins, facts)
    if now != part.approval.approval_hash:
        return StampCheck(ok=False, reason="the approved inputs or outputs changed: re-approve",
                          expected=part.approval.approval_hash, actual=now)
    return StampCheck(ok=True, expected=now, actual=now)


def check_build(part: Part, build_steps: Sequence[Any] = ()) -> StampCheck:
    """Stamp 2: do the build assets still match what was built (and shown at Gate 3)? Compared with its **own** stamp."""
    stamp = part.build_stamp
    if stamp is None:
        return StampCheck(ok=False, reason="the build was never stamped")
    now = build_hash(part, build_steps)
    if now != stamp.build_hash:
        return StampCheck(ok=False, reason="the build files changed after they were stamped: look again at Gate 3",
                          expected=stamp.build_hash, actual=now)
    return StampCheck(ok=True, expected=now, actual=now)


class Verification(Strict):
    part_id: str
    approval: StampCheck
    build: StampCheck | None = None
    stale: bool = False
    needs_gate3_look: bool = False


def stamp_build(repo: Repo, project_id: str, part_id: str, *, build_steps: Sequence[Any] | None = None) -> Part:
    """Write ``Part.build_stamp`` when the part reaches BUILT (the build files are in ``Part.build_assets``).

    The approval is untouched: the part stays APPROVED-by-the-user. A *re*build (an earlier stamp with another hash)
    flags ``build_changed`` ("rebuilt since you last looked") and marks the ``duo`` part STALE; the Gate 3 pick clears
    the flag (``confirm_build``)."""
    before = repo.get_part(project_id, part_id)
    steps = list(build_steps) if build_steps is not None else part_build_step_versions(repo, before)
    previous = before.build_stamp
    changed: list[bool] = []

    def apply(p: Part) -> None:
        if p.approval is None:
            raise ValueError(f"{p.id} has no approval to stamp a build on")
        digest = build_hash(p, steps)
        rebuilt = previous is not None and previous.build_hash != digest
        changed.append(rebuilt)
        p.state = PartState.BUILT
        p.build_stamp = BuildStamp(part_id=p.id, build_hash=digest, approval_hash=p.approval.approval_hash,   # type: ignore[arg-type]
                                   build_asset_shas=sorted(p.build_assets.values()), built_at=utcnow())
        flags = [f for f in p.flags if f != "build_changed"]
        p.flags = [*flags, "build_changed"] if rebuilt else flags

    part = repo.mutate_part(project_id, part_id, apply)
    if part.build_stamp is not None:
        repo.upsert_build_stamp(project_id, part.build_stamp, valid=True)
    if changed and changed[-1] and part.character in ("a", "b"):
        duo = repo.find_part(project_id, "duo")
        if duo is not None and duo.state not in (PartState.PLANNED, PartState.GENERATING, PartState.STALE):
            repo.mutate_part(project_id, "duo", lambda d: setattr(d, "state", PartState.STALE))
    return part


def confirm_build(repo: Repo, project_id: str, part_id: str, decision_id: str) -> Part:
    """Gate 3 pick: the user looked at this exact build."""
    def apply(p: Part) -> None:
        if p.build_stamp is None:
            raise ValueError(f"{p.id} has no build stamp to confirm")
        p.build_stamp = p.build_stamp.model_copy(update={"confirmed_decision_id": decision_id})
        p.flags = [f for f in p.flags if f != "build_changed"]

    part = repo.mutate_part(project_id, part_id, apply)
    if part.build_stamp is not None:
        repo.upsert_build_stamp(project_id, part.build_stamp, valid=True)
    return part


def verify_part(repo: Repo, part: Part, spec: Any, pins: VersionPins | None, *, facts: ApprovalFacts | None = None,
                build_steps: Sequence[Any] | None = None) -> Verification:
    """Compare a part with both stamps (read-only): the approval at every BUILD step, the DUO job and export (CHK-D09,
    CHK-E02); the build stamp at the DUO job and export, against its own value."""
    facts = facts if facts is not None else collect_facts(repo, part)
    a = check_approval(part, spec, pins, facts)
    b = None
    if part.build_assets:
        steps = list(build_steps) if build_steps is not None else part_build_step_versions(repo, part)
        b = check_build(part, steps)
    return Verification(part_id=part.id, approval=a, build=b, stale=not a.ok,
                        needs_gate3_look=bool(a.ok and b is not None and not b.ok))


def apply_verification(repo: Repo, project_id: str, v: Verification, bus: Any = None) -> Part:
    """Act on ``verify_part``: an approval mismatch sets the tile STALE ("re-approve", the step that noticed it opens
    the gate again); a build mismatch only flags ``build_changed`` and clears the Gate 3 confirmation (a new look, never
    a Gate 2 re-approval)."""
    def apply(p: Part) -> None:
        if v.stale and p.state not in (PartState.STALE,):
            p.state = PartState.STALE
        elif v.needs_gate3_look:
            if "build_changed" not in p.flags:
                p.flags = [*p.flags, "build_changed"]
            if p.build_stamp is not None:
                p.build_stamp = p.build_stamp.model_copy(update={"confirmed_decision_id": None})

    part = repo.mutate_part(project_id, v.part_id, apply)
    if v.stale:
        repo.invalidate_approvals(project_id, v.part_id)
    elif v.needs_gate3_look and part.build_stamp is not None:
        repo.upsert_build_stamp(project_id, part.build_stamp, valid=True)
    if bus is not None and (v.stale or v.needs_gate3_look):
        bus.emit("part.state", {"project_id": project_id, "part_id": v.part_id, "state": part.state.value,
                                "reason": v.approval.reason if v.stale else (v.build.reason if v.build else "")}, project_id)
    return part


def now_iso() -> str:
    return iso_utc(utcnow())


def rule(pattern: str, effect: DepEffect) -> DepRule:
    return DepRule(pattern=pattern, effect=effect)


def default_dep_rules(kind: str, character: str, index: int | None = None) -> list[DepRule]:
    """The ``Part.deps`` rules for a part kind, generated from the §9.8 table (what the stamp's spec slice covers)."""
    c = character
    i = "*" if index is None else str(index)
    table: dict[str, list[tuple[str, DepEffect]]] = {
        "face": [(f"/{c}/face/*", R), (f"/{c}/body/skin_tone", K), ("/world/detail_level", R), (f"/{c}/dna/shape_language", R)],
        "hair": [(f"/{c}/hair/*", R), (f"/{c}/dna/shape_language", R)],
        "shirt": [(f"/{c}/top/*", C_)],
        "pants": [(f"/{c}/bottom/*", C_)],
        "colours": [(f"/{c}/body/*", C_)],
        "accessory": [(f"/{c}/accessories/{i}", R), (f"/{c}/dna/shape_language", R), (f"/{c}/dna/motif_object", R)],
        "print": [(f"/{c}/top/prints/{i}", R), (f"/{c}/dna/shape_language", R), ("/world/detail_level", R)],
    }
    return [rule(p, e) for p, e in table.get(kind, [])]
