"""Patches and the change flow ("Change..." at a gate; APP_SPEC §6.4, §9.6; bible §9.6, §10.5).

Two kinds of text-to-spec changes meet here:

* the **reviser (L6)** fixes lint findings and high-severity critic fixes in the plan loop;
* the **change interpreter (L7)** turns what the person typed at a gate into a minimal patch, the parts to redo and one concrete image fix
  per part.

Both return an RFC 6902 patch whose values are JSON *text* (``value_json``). ``apply_patch`` is the one place that applies such a patch
(APP_SPEC §6.4, "Applying a patch"):

1. every ``value_json`` is parsed with ``json.loads``;
2. an op whose path is not allowed is rejected (CHK-G0-11, ASSERT): the reviser may touch only a path named by a finding, its children, or
   the palette entry a finding references; the change interpreter may touch anything except ``/combo``, ``/is_wildcard``,
   ``/a/presentation``, ``/b/presentation`` and a palette **id** (hexes may change);
3. the patch is applied to a copy (RFC 6902 ``replace``, ``add``, ``remove``);
4. the result is validated with Pydantic (and, with ``strict=True``, with the spec rules);
5. the caller re-lints (C1) and stores a new ``SpecRecord`` (version + 1) with a new ``DnaCard``.

A validation failure or a HARD lint failure rejects the patch and tells the person why; the old spec stays current.

The flow itself is a small state machine over ``ChangeRequest.status``: ``interpreting`` -> ``needs_clarification`` (a CLARIFY gate asks one
question; the answer is appended and L7 runs again) or ``awaiting_confirm`` (a CHANGE_CONFIRM gate shows the spec diff, the DNA-card diff,
the parts to redo, the estimate and the SOFT warnings) -> ``applied`` or ``cancelled``; ``rejected`` when a hard rule would break. Nothing
is applied before the person confirms.

Image fixes are routed by their scope (bible §10.5): at **Gate 1** a ``global_edit`` or ``local_edit`` goes to **I1e** (the chosen draft of
that character is edited, so "make her jacket teal" keeps the picture the person liked) and only ``regenerate`` runs I1 again
(CHK-G1-12, ASSERT, FAILURE_MODES CON-10).
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from duoskin.checks import plan_rules as PR
from duoskin.checks import runner
from duoskin.checks.model import CheckResult
from duoskin.models.common import Strict, sha256_of
from duoskin.models.llm_io import ChangeOp, ChangePlan, RevisionOp
from duoskin.models.spec import DuoSpec
from duoskin.models.spec_rules import Problem, problems_from_error

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

FORBIDDEN_CHANGE_PATHS = PR.FORBIDDEN_CHANGE_PATHS
CHARS = ("a", "b")
FIX_WORDS_MAX = 25            # a fix sentence has at most 25 words (CHK-G0-11, PRM-12)
GATE1_PARTS = ("concept_a", "concept_b")


# ---------------------------------------------------------------------------------------------------- JSON pointers
class PatchError(ValueError):
    """An op that cannot be applied (a pointer that does not resolve, a bad index, an unknown op)."""


def _unescape(seg: str) -> str:
    return seg.replace("~1", "/").replace("~0", "~")


def _split(pointer: str) -> list[str]:
    if pointer in ("", "/"):
        return []
    if not pointer.startswith("/"):
        raise PatchError(f"{pointer!r} is not a JSON Pointer")
    return [_unescape(s) for s in pointer[1:].split("/")]


def _step(node: Any, seg: str, *, create: bool = False) -> Any:
    if isinstance(node, dict):
        if seg not in node:
            raise PatchError(f"{seg!r} does not exist")
        return node[seg]
    if isinstance(node, list):
        if not seg.isdigit() or int(seg) >= len(node):
            raise PatchError(f"index {seg!r} is out of range")
        return node[int(seg)]
    raise PatchError(f"cannot step into {type(node).__name__} with {seg!r}")


def apply_op(doc: Any, op: str, path: str, value: Any) -> Any:
    """Apply one RFC 6902 ``replace``, ``add`` or ``remove`` to ``doc`` in place and return it."""
    segs = _split(path)
    if not segs:
        if op in ("replace", "add"):
            return value
        raise PatchError("the whole document cannot be removed")
    parent = doc
    for seg in segs[:-1]:
        parent = _step(parent, seg)
    last = segs[-1]
    if isinstance(parent, dict):
        if op == "remove":
            if last not in parent:
                raise PatchError(f"remove: {path} does not exist")
            del parent[last]
        elif op == "replace":
            if last not in parent:
                raise PatchError(f"replace: {path} does not exist")
            parent[last] = value
        elif op == "add":
            parent[last] = value
        else:
            raise PatchError(f"unknown op {op!r}")
        return doc
    if isinstance(parent, list):
        if op == "add":
            if last == "-":
                parent.append(value)
            elif last.isdigit() and int(last) <= len(parent):
                parent.insert(int(last), value)
            else:
                raise PatchError(f"add: bad index {last!r} in {path}")
        elif op == "replace":
            if not last.isdigit() or int(last) >= len(parent):
                raise PatchError(f"replace: {path} does not exist")
            parent[int(last)] = value
        elif op == "remove":
            if not last.isdigit() or int(last) >= len(parent):
                raise PatchError(f"remove: {path} does not exist")
            del parent[int(last)]
        else:
            raise PatchError(f"unknown op {op!r}")
        return doc
    raise PatchError(f"cannot apply {op} at {path}")


def parse_value(op: Any) -> Any:
    """``json.loads`` of an op's ``value_json`` (``remove`` carries an empty string and no value)."""
    if op.op == "remove":
        return None
    try:
        return json.loads(op.value_json)
    except ValueError as exc:
        raise PatchError(f"{op.path}: value_json is not JSON text ({exc})") from exc


def ops_of(items: Iterable[Any], *, kind: Literal["reviser", "change"]) -> list[RevisionOp | ChangeOp]:
    """Op models from dicts or models (the reason or the finding number is required by the model)."""
    cls = RevisionOp if kind == "reviser" else ChangeOp
    out: list[RevisionOp | ChangeOp] = []
    for it in items:
        out.append(it if isinstance(it, (RevisionOp, ChangeOp)) else cls.model_validate(dict(it)))
    return out


# ---------------------------------------------------------------------------------------------------- apply_patch
@dataclass
class PatchResult:
    """The outcome of ``apply_patch``. ``spec`` is None when the patch was rejected (``problems`` says why)."""

    ok: bool
    spec: dict[str, Any] | None
    problems: list[str] = field(default_factory=list)
    ops: list[RevisionOp | ChangeOp] = field(default_factory=list)
    check: CheckResult | None = None            # CHK-G0-11 (patch scope)
    changed_paths: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.problems)


def referenced_palette_ids(spec: Mapping[str, Any], paths: Iterable[str]) -> list[str]:
    """Palette ids that the values at ``paths`` name (a finding that points at ``/a/top/base_ref`` references that palette entry)."""
    from duoskin.engine.deps import resolve

    ids = {str(c.get("id")) for c in spec.get("palette", [])}
    out: list[str] = []
    for p in paths:
        v = resolve(spec, p, None)
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, str) and item in ids and item not in out:
                out.append(item)
    return out


def apply_patch(spec: Mapping[str, Any], ops: Sequence[Any], *, kind: Literal["reviser", "change"],
                finding_paths: Sequence[str] | None = None, strict: bool = False, subject_sha: str = "") -> PatchResult:
    """Apply an L6 or L7 patch to a copy of ``spec`` (see the module docstring for the five steps). ``finding_paths`` is required for
    the reviser. ``strict=True`` also enforces the spec rules (word caps, palette references, counts); the plan loop validates leniently
    and lets the linter report what is left, the change flow is strict because a rejected change leaves the old spec untouched."""
    doc = copy.deepcopy(dict(spec))
    try:
        models = ops_of(ops, kind=kind)
    except ValidationError as exc:
        return PatchResult(False, None, [f"the patch is malformed: {exc.errors()[0].get('msg', 'invalid')}"])
    if kind == "reviser" and finding_paths is None:
        raise ValueError("the reviser's scope needs the finding paths")
    palette_ids = [str(c.get("id", "")) for c in doc.get("palette", [])]
    scope_bad = PR.patch_scope_problems(
        models, finding_paths=(list(finding_paths) if kind == "reviser" else None), palette_ids=palette_ids,
        referenced_palette_ids=referenced_palette_ids(doc, finding_paths or ()) if kind == "reviser" else ())
    check = runner.build_result("CHK-G0-11", passed=not scope_bad, subject_sha=subject_sha, metric="patch_scope",
                                evidence="; ".join(scope_bad) or "every op is inside its allowed paths")
    if scope_bad:
        return PatchResult(False, None, scope_bad, models, check)
    problems: list[str] = []
    changed: list[str] = []
    for i, op in enumerate(models):
        try:
            apply_op(doc, op.op, op.path, parse_value(op))
            changed.append(op.path)
        except PatchError as exc:
            problems.append(f"op {i}: {exc}")
    if problems:
        return PatchResult(False, None, problems, models, check)
    try:
        DuoSpec.model_validate(doc, context=None if strict else {"skip_rules": True})
    except ValidationError as exc:
        found = problems_from_error(exc)
        shown = [str(p) for p in found] or [f"{'/'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]]
        return PatchResult(False, None, shown, models, check)
    return PatchResult(True, doc, [], models, check, changed)


def spec_problems_text(problems: Iterable[Problem]) -> str:
    return "; ".join(str(p) for p in problems)


# ---------------------------------------------------------------------------------------------------- the envelope
def apply_envelope_patch(envelope: Mapping[str, Any], ops: Sequence[Any], finding_paths: Sequence[str]) -> PatchResult:
    """The same patch rules for the plan-set envelope (``brief_constraints`` and ``how_they_differ``), which are not part of a ``DuoSpec``.
    Only paths named by a finding (or below one) may change."""
    doc = copy.deepcopy(dict(envelope))
    try:
        models = ops_of(ops, kind="reviser")
    except ValidationError as exc:
        return PatchResult(False, None, [f"the patch is malformed: {exc.errors()[0].get('msg', 'invalid')}"])
    bad = PR.patch_scope_problems(models, finding_paths=list(finding_paths), palette_ids=(), referenced_palette_ids=())
    if bad:
        return PatchResult(False, None, bad, models)
    problems: list[str] = []
    for i, op in enumerate(models):
        try:
            apply_op(doc, op.op, op.path, parse_value(op))
        except PatchError as exc:
            problems.append(f"op {i}: {exc}")
    return PatchResult(not problems, doc if not problems else None, problems, models)


# ---------------------------------------------------------------------------------------------------- diffs
def diff_specs(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Changed JSON pointers with old and new values (the spec diff that CHANGE_CONFIRM and ``/api/specs/{id}/diff`` show)."""
    from duoskin.engine import deps

    return [{"path": p, "old": deps.resolve(old, p), "new": deps.resolve(new, p)} for p in deps.changed_pointers(dict(old), dict(new))]


def characters_changed(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """Which characters a change touches: a path under ``/a`` or ``/b``, or a palette colour that one of them references."""
    from duoskin.engine import deps

    touched: set[str] = set()
    for p in deps.changed_pointers(dict(old), dict(new)):
        m = re.match(r"^/(a|b)(/|$)", p)
        if m:
            touched.add(m.group(1))
            continue
        pm = re.match(r"^/palette/(\d+)(/.*)?$", p)
        if pm:
            idx = int(pm.group(1))
            pal = new.get("palette", [])
            pid = pal[idx].get("id") if idx < len(pal) else None
            for c in CHARS:
                if pid and _uses_palette_id(new.get(c, {}), pid):
                    touched.add(c)
        elif p.startswith("/world/") or p == "/world":
            touched.update(CHARS)           # the shared world changes how both are drawn
    return [c for c in CHARS if c in touched]


def _uses_palette_id(node: Any, pid: str) -> bool:
    if isinstance(node, dict):
        return any(_uses_palette_id(v, pid) for v in node.values())
    if isinstance(node, list):
        return any(_uses_palette_id(v, pid) for v in node)
    return node == pid


# ---------------------------------------------------------------------------------------------------- image fixes
def character_of_part(part_id: str) -> str | None:
    """``a.shirt`` / ``concept_a`` -> ``a``; ``duo`` -> None."""
    m = re.match(r"^(?:([ab])\.|concept_([ab])$)", part_id)
    return (m.group(1) or m.group(2)) if m else None


@dataclass(frozen=True)
class ConceptFix:
    """What Gate 1 does for one character after a confirmed change."""

    character: str
    route: Literal["i1e", "i1"]
    fix_sentence: str
    region_hint: str = "none"
    keep: tuple[str, ...] = ()
    reason: str = ""


def code_fix_sentence(old: Mapping[str, Any], new: Mapping[str, Any], character: str) -> str:
    """A fix sentence built by code when L7 gave none for a character whose design changed (at most 25 words, positive, visible result).

    Colour changes name the old and the new dictionary colour; other changes say what the field is now. An empty string means "no safe
    sentence": the caller then draws the character again (I1)."""
    from duoskin.engine import deps
    from duoskin.imaging.colournames import name_palette

    paths = [p for p in deps.changed_pointers(dict(old), dict(new))]
    pal_old = {str(c["id"]): c["hex"] for c in old.get("palette", [])}
    pal_new = {str(c["id"]): c["hex"] for c in new.get("palette", [])}
    bits: list[str] = []
    for pid, hx in pal_new.items():
        if pid in pal_old and pal_old[pid].lower() != str(hx).lower() and _uses_palette_id(new.get(character, {}), pid):
            was, now = name_palette([pal_old[pid]], max_names=1), name_palette([hx], max_names=1)
            if was and now:
                bits.append(f"Change every {was[0]} area to {now[0]}")
    for p in paths:
        if re.match(rf"^/{character}/hair/description$", p):
            bits.append(f"Change the hair to {deps.resolve(new, p)}")
    sentence = "; ".join(bits[:2]).strip()
    return sentence if sentence and len(sentence.split()) <= FIX_WORDS_MAX else ""


def route_concept_fixes(plan: Mapping[str, Any], old: Mapping[str, Any], new: Mapping[str, Any], *, target: str = "both",
                        banned: Any = None) -> list[ConceptFix]:
    """The Gate 1 routing of a ``ChangePlan`` (bible §10.5): per affected character, ``global_edit`` and ``local_edit`` run **I1e** on the
    chosen draft, ``regenerate`` runs I1. A character with a changed design but no usable fix sentence is drawn again (I1).

    ``target`` ("a", "b" or "both") limits the characters; a change that touches only one character never redraws the other."""
    from duoskin.prompts import freetext
    from duoskin.prompts.catalog import default_ctx

    banned = banned or default_ctx().banned
    allowed = list(CHARS) if target in ("both", "", None) else [target]
    touched = [c for c in characters_changed(old, new) if c in allowed]
    fixes_by_char: dict[str, list[Mapping[str, Any]]] = {c: [] for c in CHARS}
    for fx in plan.get("image_fixes", []):
        c = character_of_part(str(fx.get("part_id", "")))
        if c:
            fixes_by_char[c].append(fx)
    out: list[ConceptFix] = []
    for c in allowed:
        fixes = fixes_by_char[c]
        if not fixes and c not in touched:
            continue
        if not fixes:
            sentence = code_fix_sentence(old, new, c)
            if sentence and not freetext.free_text_problems(sentence, FIX_WORDS_MAX, banned, strict=True):
                out.append(ConceptFix(c, "i1e", sentence, reason="the design changed"))
            else:
                out.append(ConceptFix(c, "i1", "", reason="the design changed and no safe edit sentence exists"))
            continue
        if any(f.get("scope") == "regenerate" for f in fixes):
            out.append(ConceptFix(c, "i1", "", reason="the plan asks to draw it again"))
            continue
        fx = fixes[0]
        sentence = " ".join(str(fx.get("fix_sentence", "")).split())
        problems = freetext.free_text_problems(sentence, FIX_WORDS_MAX, banned, strict=True)
        if problems or not sentence:
            out.append(ConceptFix(c, "i1", "", reason="the fix sentence did not pass the text check"))
            continue
        out.append(ConceptFix(c, "i1e", sentence, region_hint=str(fx.get("region_hint", "none")),
                              keep=tuple(str(k) for k in fx.get("keep", [])), reason=str(fx.get("scope", "global_edit"))))
    return out


def check_gate1_change_route(fixes: Sequence[ConceptFix], plan: Mapping[str, Any], steps_by_char: Mapping[str, Mapping[str, Any]],
                             current_chosen: Mapping[str, str], *, subject_sha: str = "") -> CheckResult:
    """CHK-G1-12 / CON-10 (ASSERT): when the L7 plan has only ``global_edit`` or ``local_edit`` image fixes, each affected character's step
    is I1e with Image 1 equal to that character's current chosen draft, and the other character's draft is untouched. I1 may run only
    for an L7 ``regenerate`` (or when no safe edit sentence exists, which the route table records)."""
    scopes = {str(f.get("scope")) for f in plan.get("image_fixes", [])}
    only_edits = bool(scopes) and scopes <= {"global_edit", "local_edit"}
    problems: list[str] = []
    touched = {f.character for f in fixes}
    for c, step in steps_by_char.items():
        if c not in touched:
            problems.append(f"character {c.upper()} was not changed but a step was made for it")
            continue
        if only_edits:
            fx = next((f for f in fixes if f.character == c), None)
            if step.get("mode") != "i1e" and fx is not None and fx.route == "i1e":
                problems.append(f"character {c.upper()}: expected I1e, the step is {step.get('mode')}")
            if step.get("mode") == "i1e" and step.get("base_sha") != current_chosen.get(c):
                problems.append(f"character {c.upper()}: Image 1 is not the current chosen draft")
    return runner.build_result("CHK-G1-12", passed=not problems, subject_sha=subject_sha, metric="gate1_change_route",
                               evidence="; ".join(problems) or "I1e edits the chosen draft of the affected character only")


# ---------------------------------------------------------------------------------------------------- L7 inputs
def parts_for_l7(*, gate: str, spec: Mapping[str, Any], parts: Sequence[Any] = (), target: str = "both") -> list[dict[str, Any]]:
    """``<parts>`` of L7: at Gate 1 the parts are ``concept_a`` and ``concept_b`` (one chosen draft each); later gates list the real parts
    with their state and the spec paths each depends on."""
    if gate == "concept":
        return [{"part_id": f"concept_{c}", "type": "concept", "status": "chosen draft", "spec_paths": [f"/{c}", "/palette", "/world"]}
                for c in CHARS if target in ("both", c, "", None)]
    out = []
    for p in parts:
        out.append({"part_id": p.id, "type": p.kind.value if hasattr(p.kind, "value") else str(p.kind), "status": getattr(p.state, "value", str(p.state)),
                    "spec_paths": [r.pattern for r in getattr(p, "deps", [])][:8]})
    return out


def clicked_tile_for(target: str, tile_part_id: str | None = None) -> str:
    """``<clicked_tile>``: the part id the person clicked, or ``none``. At Gate 1 a scope of "a" or "b" names that character's concept."""
    if tile_part_id:
        return tile_part_id
    return f"concept_{target}" if target in CHARS else "none"


def validate_plan(plan: ChangePlan, spec: Mapping[str, Any], *, kind: str = "change") -> list[str]:
    """Gate A of L7 beyond the patch (APP_SPEC §9.6): every ``fix_sentence`` is at most 25 words and passes the free-text lint; every
    ``redo_parts`` and ``image_fixes`` part id is a part id (or a Gate 1 concept)."""
    from duoskin.prompts import freetext
    from duoskin.prompts.catalog import default_ctx

    bans = default_ctx().banned
    out: list[str] = []
    part_ok = re.compile(r"^(duo|(a|b)\.(face|hair|shirt|pants|colours|acc\.[0-9]|print\.(top|bottom|shoes)\.[0-9])|concept_[ab])$")
    for fx in plan.image_fixes:
        if not part_ok.match(fx.part_id):
            out.append(f"image fix for an unknown part '{fx.part_id}'")
        for pr in freetext.free_text_problems(fx.fix_sentence, FIX_WORDS_MAX, bans, strict=True):
            out.append(f"fix sentence for {fx.part_id}: {pr}")
    for rd in plan.redo_parts:
        if not part_ok.match(rd.part_id):
            out.append(f"redo of an unknown part '{rd.part_id}'")
    _ = (spec, kind)
    return out


def change_fingerprint(spec_sha: str, text: str, answers: Sequence[str], target: str) -> str:
    """The identity of one L7 request (what the content cache of the call is keyed by)."""
    return sha256_of({"spec": spec_sha, "text": text, "answers": list(answers), "target": target})


def estimate_change(rt: Runtime, fixes: Sequence[ConceptFix], *, quality: str = "low") -> float:
    """USD estimate of redoing the concept pictures of a confirmed Gate 1 change: one draft call per fix (I1e and I1 cost the same)."""
    from duoskin.providers import pricing

    one = pricing.estimate_openai_image(quality=quality, n=4, n_input_images=2, prompt_chars=1900).usd
    judge = pricing.estimate_claude(rt.effective_settings().models.checker, input_tokens=2600, output_tokens=500).usd
    return round(len(fixes) * (one + 3 * judge) + len(fixes) * judge, 4)


# ====================================================================================================== the change flow
# ``change.interpret`` (L7), the CLARIFY and CHANGE_CONFIRM gates and what a confirmed change does. The state lives in a ``ChangeRequest``
# (``plan`` carries the origin gate and tile, the L7 plan, the patch, the new spec and the numbers the confirm gate shows). Nothing is
# applied, and nothing paid runs, before the person confirms.
MAX_ANSWERS = 2                      # L7 may ask one question, and one more when the answer was not enough

#: ``gate kind -> fn(ApplyContext, ChangeRequest) -> ApplyResult | None``: what a confirmed change does for the gate it was typed at. Gate 1
#: is registered below; the part-board lane registers PART_BOARD and FINAL_PICK (``register_router``).
ROUTERS: dict[str, Any] = {}


def register_router(gate_kind: str, fn: Any) -> None:
    """Register what a confirmed change does for ``gate_kind`` (``"concept"``, ``"part_board"``, ``"final_pick"``): ``fn(ac, change) ->
    ApplyResult | None`` runs inside the CHANGE_CONFIRM decision's transaction (rows and steps only)."""
    ROUTERS[str(gate_kind)] = fn


class InterpretParams(Strict):
    project_id: str
    change_id: str
    round: int = 0


def new_change_id() -> str:
    from duoskin.models.common import new_id

    return new_id("chg")


def start_change(ac: Any, *, spec_id: str | None = None) -> Any:
    """The applier part of "Change..." at any gate: store a ``ChangeRequest`` and start ``change.interpret`` (L7). Called by the Gate 1
    applier; other gates call it from theirs with the spec the change applies to. The gate stays open: the change is a side track."""
    from duoskin.engine.gates import ApplyResult, GateError
    from duoskin.models.gate import ChangeRequest

    rt, gate, tile, d = ac.rt, ac.gate, ac.tile, ac.decision
    text = " ".join((d.text or "").split())
    if len(text) < 2:
        raise GateError("say what should change", 422, "text_required")
    target = d.target or "both"
    project = rt.repo.get_project(gate.project_id)
    sid = spec_id or str(tile.facts.get("spec_id") or project.approved_spec_id or project.current_spec_id or "")
    if not sid:
        raise GateError("there is no plan to change yet", 422, "no_spec")
    rec = rt.repo.get_spec(sid)
    origin = {"gate_id": gate.id, "gate_kind": gate.kind.value, "tile_id": tile.tile_id, "part_id": tile.part_id, "spec_id": rec.id,
              "spec_sha": rec.sha256, "plan_set_id": rec.plan_set_id, "job_id": gate.job_id}
    cr = ChangeRequest(id=new_change_id(), project_id=gate.project_id, gate_id=gate.id, tile_id=tile.tile_id, text=text, mask_sha=d.mask_sha,
                       plan={"origin": origin, "target": target, "answers": []}, status="interpreting")
    rt.repo.save_change(cr)
    step = rt.ops.new_step("change.interpret", job_id=gate.job_id, project_id=gate.project_id,
                           params=InterpretParams(project_id=gate.project_id, change_id=cr.id).model_dump(mode="json"), nonce=cr.id, priority=10)
    rt.scheduler.spawn(gate.job_id, [step])
    return ApplyResult(close_gate=False, spawned_step_ids=[step.id], change_request_id=cr.id, step="keep")


REQUEST_MAX_CHARS = 800                # a change is a sentence or two; a pasted page is not a change and costs a paid call
PRESCREEN_GROUPS = ("ip_platform", "ip_brands", "ip_franchises", "ip_artists", "sexual")      # the IP groups; ordinary words ("baby blue") pass


def prescreen_request(text: str, banned: Any = None) -> list[str]:
    """Why a typed change request must not be sent to L7 at all (an empty list means it may go). Checked in code before any paid call:

    * brand, platform, franchise and artist names ("the Roblox logo", "Pikachu") and sexual words: the duo must stay original, so the request
      is answered with a plain sentence and nothing is drawn;
    * instruction-like phrases ("ignore the previous instructions"): not a change to the duo;
    * a request far longer than a change.

    Tag-like text is not rejected here: the L7 compiler turns it into plain text inside ``<user_change_request>`` (``llm.neutralise_tags``).
    Whatever L7 returns is still linted (fix sentence, patch values, plan rules), so this is a first gate, not the only one."""
    from duoskin.prompts import freetext
    from duoskin.prompts.catalog import default_ctx

    banned = banned or default_ctx().banned
    t = " ".join(str(text or "").split())
    problems: list[str] = []
    hits = banned.hits(t, list(PRESCREEN_GROUPS))
    if hits:
        problems.append("Brand names, game or show characters and real people cannot be used, so the duo stays original. "
                        "Describe the shape and the colours you want instead.")
    if freetext.INJECTION.search(t):
        problems.append("That does not describe a change to the duo. Say what should look different, for example a colour, a length or a shape.")
    if len(t) > REQUEST_MAX_CHARS:
        problems.append("Please describe the change in a sentence or two.")
    return problems


def _reject(rt: Runtime, cr: Any, doc: dict[str, Any], reason: str) -> Any:
    from duoskin.engine.registry import StepResult

    doc = {**doc, "reason": reason, "rejection": reason}
    rt.repo.save_change(cr.model_copy(update={"status": "rejected", "plan": doc}))
    rt.bus.emit("spec.updated", {"project_id": cr.project_id, "change_id": cr.id, "status": "rejected"}, cr.project_id)
    return StepResult(result={"change_id": cr.id, "status": "rejected", "reason": reason}, message="that change cannot be made")


def _problems_text(items: Sequence[str], limit: int = 2) -> str:
    return "; ".join(str(i) for i in items[:limit])


def soft_warning_rows(results: Iterable[CheckResult], risks: Iterable[str]) -> list[dict[str, Any]]:
    """The heads-up lines of a change (at most two are shown): the SOFT lint results that failed and the duo or Roblox risks L7 named."""
    rows = [{"id": r.check_id + ":" + r.metric, "text": r.evidence or r.metric, "severity": "medium"} for r in results
            if r.kind == "soft" and r.ran and not r.passed]
    rows += [{"id": f"risk{i}", "text": str(t), "severity": "medium"} for i, t in enumerate(risks)]
    return rows


def run_interpret(ctx: Any, p: InterpretParams, inputs: list[Any]) -> Any:
    """L7: turn the typed request into a patch, the parts to redo and one image fix per part; check it; then open the confirm gate (or a
    clarifying question, or reject it)."""
    from duoskin.engine.gates import allowed_actions_for
    from duoskin.engine.registry import StepResult
    from duoskin.models.common import utcnow
    from duoskin.models.dna import DnaCard, card_from_spec
    from duoskin.models.gate import Gate, GateKind, GateTile, TileState
    from duoskin.pipeline import brief as BR
    from duoskin.pipeline import lint as LI
    from duoskin.pipeline import plan as PL

    rt = ctx.rt
    cr = rt.repo.get_change(p.change_id)
    doc = dict(cr.plan or {})
    origin, target, answers = doc["origin"], doc.get("target", "both"), list(doc.get("answers", []))
    rec = rt.repo.get_spec(origin["spec_id"])
    spec = rec.spec
    gate_kind = origin["gate_kind"]
    parts = parts_for_l7(gate="concept" if gate_kind == "concept" else gate_kind, spec=spec,
                         parts=[] if gate_kind == "concept" else rt.repo.list_parts(cr.project_id), target=target)
    request = cr.text + "".join(f"\nAnswer: {a}" for a in answers)
    screened = [x for part in (cr.text, *answers) for x in prescreen_request(part)]
    if screened:                                  # no paid call for a request that asks for a brand, a known character or an instruction
        return _reject(rt, cr, doc, screened[0])
    ctx.progress(0.2, "reading your change")
    out = PL.llm_call(ctx, "L7.change_interpreter", {"spec_json": PL.canonical(spec), "parts": PL.canonical(parts),
                                                     "clicked_tile": clicked_tile_for(target, origin.get("part_id")), "user_change_request": request},
                      nonce=change_fingerprint(rec.sha256, cr.text, answers, target), think=True)
    plan: ChangePlan = out.parsed
    if plan.needs_clarification.strip():
        if len(answers) >= MAX_ANSWERS:
            return _reject(rt, cr, doc, "I could not work out what to change from that. Try describing it a different way.")
        question = " ".join(plan.needs_clarification.split())[:300]
        doc.update({"question": question})
        rt.repo.save_change(cr.model_copy(update={"status": "needs_clarification", "plan": doc}))
        tile = GateTile(tile_id="clarify", label=question[:120], state=TileState.READY, facts={"question": question, "change_id": cr.id},
                        allowed_actions=allowed_actions_for(GateKind.CLARIFY))
        ctx.open_gate(Gate(id="", project_id=cr.project_id, job_id=ctx.step.job_id, kind=GateKind.CLARIFY, tiles=[tile], opened_at=utcnow()))
        return StepResult(result={"change_id": cr.id, "status": "needs_clarification", "question": question}, message="one question for you")
    ops = [o.model_dump(mode="json") for o in plan.patch]
    pr = apply_patch(spec, ops, kind="change", strict=True, subject_sha=rec.sha256)
    checks: list[CheckResult] = [pr.check] if pr.check else []
    if not pr.ok or pr.spec is None:
        ctx.record_checks(checks)
        return _reject(rt, cr, doc, _problems_text(pr.problems) or "the change would break a design rule")
    new_spec = pr.spec
    if not ops and not plan.image_fixes and not plan.redo_parts:
        return _reject(rt, cr, doc, "that does not change anything in the design")
    bad = validate_plan(plan, new_spec)
    if bad:
        ctx.record_checks(checks)
        return _reject(rt, cr, doc, _problems_text(bad))
    lctx = LI.lint_context(rt, rt.repo.get_project(cr.project_id), recent_cards=BR.recent_cards(rt, cr.project_id))
    bundle = LI.lint_candidates([("old", spec), ("new", new_spec)], lctx, check_set=False)
    checks.extend(bundle.results("new"))
    hard = bundle.hard_findings("new")
    if hard:
        ctx.record_checks(checks)
        return _reject(rt, cr, doc, _problems_text([f.message for f in hard]))
    diff = diff_specs(spec, new_spec)
    prev = rt.repo.get_dna_card(rec.id)
    card = card_from_spec(new_spec, locked=False, source="change", previous=DnaCard.model_validate(prev) if prev else None)
    plan_json = plan.model_dump(mode="json")
    fixes: list[ConceptFix] = []
    if gate_kind == "concept":
        fixes = route_concept_fixes(plan_json, spec, new_spec, target=target)
        estimate = estimate_change(rt, fixes, quality=rt.repo.get_project(cr.project_id).settings.concept_quality)
        invalidation = {"parts": [{"part_id": f"concept_{f.character}", "effect": "regenerate"} for f in fixes], "pair_rechecks": [],
                        "duo_stale": False, "lint_only": [], "not_patchable": [], "changed_paths": [d["path"] for d in diff], "estimate_usd": estimate}
    else:
        from duoskin.engine import deps

        report = deps.affected_parts(spec, new_spec, [r.model_dump() for r in plan.redo_parts],
                                     existing_parts=[x.id for x in rt.repo.list_parts(cr.project_id)])
        invalidation = report.model_dump(mode="json")
        estimate = float(report.estimate_usd or 0.0)
    # the heads-up lists what THIS change makes worse: a suggestion the plan already had is not news (and would be shown again at every change)
    already = {r.check_id + ":" + r.metric for r in bundle.warnings("old")}
    warnings = soft_warning_rows([r for r in bundle.warnings("new") if r.check_id + ":" + r.metric not in already], plan.duo_contract_risks)
    spec_changes = [{"path": d["path"], "old": d["old"], "new": d["new"]} for d in diff]
    dna_changes = [c for c in spec_changes if "/dna/" in c["path"] or c["path"].startswith("/world") or c["path"].startswith("/shared_anchors")]
    doc.update({"l7": plan_json, "ops": ops, "new_spec": new_spec, "fixes": [asdict(f) | {"keep": list(f.keep)} for f in fixes],
                "diff": {"spec_changes": spec_changes, "dna_changes": dna_changes}, "dna_diff": list(card.diff_from_previous), "warnings": warnings[:2],
                "understood_as": plan.understood_as, "estimate_usd": estimate, "invalidation": invalidation})
    ctx.record_checks(checks)
    rt.repo.save_change(cr.model_copy(update={"status": "awaiting_confirm", "plan": doc, "invalidation": invalidation, "estimate_usd": estimate}))
    tile = GateTile(tile_id="change", label=plan.understood_as[:120] or "Confirm your change", state=TileState.READY,
                    facts={"change_id": cr.id, "diff": doc["diff"], "spec_changes": spec_changes, "dna_changes": dna_changes,
                           "dna_diff": list(card.diff_from_previous), "estimate_usd": estimate, "invalidation": invalidation,
                           "lint_warnings": warnings[:2], "understood_as": plan.understood_as, "origin": gate_kind},
                    allowed_actions=allowed_actions_for(GateKind.CHANGE_CONFIRM))
    ctx.open_gate(Gate(id="", project_id=cr.project_id, job_id=ctx.step.job_id, kind=GateKind.CHANGE_CONFIRM, tiles=[tile], opened_at=utcnow()))
    return StepResult(result={"change_id": cr.id, "status": "awaiting_confirm", "estimate_usd": estimate}, message="waiting for your confirmation")


def estimate_interpret(p: InterpretParams) -> float:
    from duoskin.pipeline import plan as PL

    return PL.estimate_llm(None, "L7.change_interpreter", in_tokens=9000, out_tokens=2500, cached=6000)


# ---------------------------------------------------------------------------------------------------- the gate appliers
def apply_clarify(ac: Any) -> Any:
    """CLARIFY: the answer (action ``change`` with text) is appended and L7 runs again; ``cancel`` drops the request."""
    from duoskin.engine.gates import ApplyResult, GateError
    from duoskin.models.gate import GateAction

    rt, tile, d = ac.rt, ac.tile, ac.decision
    cr = rt.repo.get_change(str(tile.facts["change_id"]))
    doc = dict(cr.plan or {})
    if d.action == GateAction.CANCEL:
        rt.repo.save_change(cr.model_copy(update={"status": "cancelled"}))
        return ApplyResult(close_gate=True, change_request_id=cr.id, step="auto")
    answer = " ".join((d.text or "").split())
    if len(answer) < 2:
        raise GateError("please type a short answer", 422, "text_required")
    doc["answers"] = [*doc.get("answers", []), answer]
    rt.repo.save_change(cr.model_copy(update={"status": "interpreting", "plan": doc}))
    step = rt.ops.new_step("change.interpret", job_id=ac.gate.job_id, project_id=ac.project_id,
                           params=InterpretParams(project_id=ac.project_id, change_id=cr.id, round=len(doc["answers"])).model_dump(mode="json"),
                           nonce=f"{cr.id}:{len(doc['answers'])}", priority=10)
    rt.scheduler.spawn(ac.gate.job_id, [step])
    return ApplyResult(close_gate=True, spawned_step_ids=[step.id], change_request_id=cr.id, step="auto")


def apply_change_confirm(ac: Any) -> Any:
    """CHANGE_CONFIRM: ``cancel`` drops the change; ``confirm`` applies it through the router of the gate it was typed at."""
    from duoskin.engine.gates import ApplyResult, GateError
    from duoskin.models.gate import GateAction

    rt, tile, d = ac.rt, ac.tile, ac.decision
    cr = rt.repo.get_change(str(tile.facts["change_id"]))
    if d.action == GateAction.CANCEL:
        rt.repo.save_change(cr.model_copy(update={"status": "cancelled"}))
        return ApplyResult(close_gate=True, change_request_id=cr.id, step="auto")
    origin = (cr.plan or {}).get("origin", {})
    router = ROUTERS.get(str(origin.get("gate_kind")))
    if router is None:
        raise GateError("a change at this gate cannot be applied yet", 422, "no_router")
    res = router(ac, cr) or ApplyResult()
    rt.repo.save_change(rt.repo.get_change(cr.id).model_copy(update={"status": "applied"}))
    rt.bus.emit("spec.updated", {"project_id": ac.project_id, "change_id": cr.id, "status": "applied"}, ac.project_id)
    res.close_gate = True
    res.change_request_id = cr.id
    res.step = "auto"
    return res


def apply_gate1_change(ac: Any, cr: Any) -> Any:
    """What a confirmed change does at Gate 1: a new spec version (version + 1, ``created_by="change"``), and for each affected character an
    **I1e edit of its chosen draft** (I1 only where L7 asked to regenerate); the other character's draft is not touched. Then the plan's
    sheet is checked again and the tile refreshed."""
    from duoskin.engine.gates import ApplyResult, GateError
    from duoskin.models.gate import TileState
    from duoskin.pipeline import concept as CO
    from duoskin.pipeline import plan as PL

    rt = ac.rt
    doc = dict(cr.plan or {})
    origin = doc["origin"]
    g1 = CO.open_concept_gate(rt, cr.project_id)
    if g1 is None:
        raise GateError("the concept screen is no longer open", 409, "gate_closed")
    tile = next((t for t in g1.tiles if t.tile_id == origin["tile_id"]), None)
    old = rt.repo.get_spec(origin["spec_id"])
    if tile is None or tile.facts.get("spec_id") != old.id or old.sha256 != origin["spec_sha"]:
        raise GateError("that plan changed while you were deciding: ask for the change again", 409, "stale_plan")
    if tile.state == TileState.GENERATING:
        raise GateError("this plan is still being drawn", 409, "tile_busy")
    ops = ops_of(doc["ops"], kind="change")
    new = PL.new_spec_record(rt, cr.project_id, old.plan_set_id, old.plan_index, doc["new_spec"], parent=old, created_by="change", ops=ops, status="shown")
    PL.save_spec(rt, new.model_copy(update={"rank": old.rank, "critic_levels": dict(old.critic_levels), "pairwise_wins": old.pairwise_wins}))
    PL.save_spec(rt, old.model_copy(update={"status": "superseded"}))
    fixes = [ConceptFix(character=f["character"], route=f["route"], fix_sentence=f["fix_sentence"], region_hint=f.get("region_hint", "none"),
                        keep=tuple(f.get("keep", ())), reason=f.get("reason", "")) for f in doc.get("fixes", [])]
    slot = CO.slot_of(tile)
    chosen_before: dict[str, str] = {}
    for c in CHARS:
        st = CO.get_state(rt, old.plan_set_id, slot, c)
        chosen_before[c] = str((st.get("chosen") or {}).get("sha", ""))
        st["spec_id"] = new.id
        CO.put_state(rt, old.plan_set_id, slot, c, st)
    if fixes:
        steps = CO.redraw_steps(rt, g1, tile, [f.character for f in fixes], reason="change", fixes={f.character: f for f in fixes}, spec_id=new.id)
    else:
        asm = CO.assemble_step(rt, g1.job_id, cr.project_id, old.plan_set_id, slot, new.id)
        steps = [asm, CO.gate_step(rt, g1.job_id, cr.project_id, old.plan_set_id, refresh=True, deps=[asm.id])]
    by_char = {s.params["char"]: s.params for s in steps if s.kind == "concept.char"}
    chk = check_gate1_change_route(fixes, doc.get("l7", {}), by_char, chosen_before, subject_sha=new.sha256)
    rt.repo.insert_checks([chk], project_id=cr.project_id, step_id=ac.gate.step_id)
    runner.raise_on_assert_failure([chk])
    CO.mark_gate_tile_generating(rt, cr.project_id, tile.tile_id)
    rt.repo.mutate_project(cr.project_id, lambda pr: setattr(pr, "current_spec_id", new.id) if pr.current_spec_id in (None, old.id) else None)
    return ApplyResult(spawned_step_ids=CO.spawn(rt, g1.job_id, steps), resulting_spec_id=new.id)


# ---------------------------------------------------------------------------------------------------- registration
def register_handlers() -> None:
    from duoskin.engine import registry as reg

    reg.register_handler("change.interpret", run_interpret, version=1, pool="api", paid=True, provider="anthropic", Params=InterpretParams,
                         estimate=estimate_interpret, cacheable=False)


def register(rt: Any = None) -> None:
    """Called by ``pipeline.register``: the L7 handler, the Gate 1 router and, with a runtime, the CLARIFY and CHANGE_CONFIRM appliers."""
    from duoskin.models.gate import GateKind

    register_handlers()
    register_router("concept", apply_gate1_change)
    if rt is not None:
        rt.gates.register_applier(GateKind.CLARIFY, apply_clarify)
        rt.gates.register_applier(GateKind.CHANGE_CONFIRM, apply_change_confirm)
