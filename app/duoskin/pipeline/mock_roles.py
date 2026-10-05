"""Mock role builders for the plan loop and the change flow (registered lazily by ``providers.mock.llm``: its ``LAZY_ROLE_MODULES``
lists this module, so mock mode needs no keys and no import order).

The generic fabricator of the mock provider answers any schema, but its plans, critiques and revisions would not be worth looking at.
These builders read what the real prompts send (the tags of ``prompts/L*.md``) and answer like a sensible model would:

* ``PlanSet`` (L3): three specs from ``providers.mock.roles`` (the generator written with the schema). It honours ``<structure_request>``, a
  structure the brief names ("twin sisters", "team uniform", "seasons"), ``<avoid>`` (a new plan round differs from the rejected plans) and
  the replacement request (one spec, wildcard as asked). Test hooks live in the brief text only: ``[mock:dangling]`` (plan 2 refers to a
  palette colour that does not exist: HARD lint, the reviser fixes it), ``[mock:unfixable]`` (the same, but the reviser cannot fix it, so the
  plan is dropped and replaced), ``[mock:unfixable_wildcard]`` (the wildcard is the broken one), ``[mock:no_wildcard_replacement]`` (the
  replacement is no wildcard: the notice appears), ``[mock:notbuildable]`` (the plan has a cape the kits cannot build: the not-buildable
  panel), ``[mock:critic_fix]`` (the critic reports one high-severity fix).
* ``Critique`` / ``PairJudgment`` (L4, L5): deterministic levels from a hash of the spec; the pair verdict is the same in both orders.
* ``Revision`` (L6): fixes dangling palette references, the wildcard flag, brief-constraint paths and critic fixes, and leaves what it cannot
  fix alone.
* ``ReferenceAnalysis``, ``TasteProfile`` (L1, L2), ``ElementList`` (L15) and ``ChangePlan`` (L7: "her" and "his" are resolved from the
  clicked tile or the characters' presentation).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any

from duoskin.providers.mock import _draw as D

HOOKS = ("[mock:dangling]", "[mock:unfixable]", "[mock:unfixable_wildcard]", "[mock:no_wildcard_replacement]", "[mock:notbuildable]",
         "[mock:critic_fix]")
PHRASES = (("twin", "mirror"), ("mirror", "mirror"), ("matching", "same_club"), ("uniform", "same_club"), ("team", "same_club"),
           ("club", "same_club"), ("season", "seasonal_twins"), ("mascot", "object_mascot"), ("opposite", "complement"),
           ("complement", "complement"), ("leader", "leader_chaotic"), ("chaos", "leader_chaotic"))
UNBUILDABLE_WORDS = ("cape", "wings")


def register_mock_roles(register: Any) -> None:
    """Called by ``providers.mock.llm`` after the built-in roles: these replace the generic answers for the plan loop."""
    register("PlanSet", planner_builder)
    register("Critique", critique_builder)
    register("PairJudgment", pair_builder)
    register("Revision", revision_builder)
    register("ReferenceAnalysis", reference_builder)
    register("TasteProfile", taste_builder)
    register("ElementList", inventory_builder)
    register("ChangePlan", change_builder)


# ---------------------------------------------------------------------------------------------------- helpers
def _tag(text: str, name: str) -> str:
    m = re.search(rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _json_tag(text: str, name: str) -> Any:
    raw = _tag(text, name)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _hash(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:8], 16)


def _words(text: str, cap: int) -> str:
    return " ".join(str(text).split()[:cap])


def _get(doc: Any, pointer: str) -> Any:
    cur = doc
    for seg in [s for s in pointer.split("/") if s != ""]:
        seg = seg.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def structure_from_brief(brief: str) -> str | None:
    """The pair structure a brief names or implies ("twin sisters" is a mirror pair; "team uniform" is a club), or None for an open brief."""
    from duoskin.providers.mock import roles as R

    named = R._named_structure(brief)
    if named:
        return named
    low = brief.lower()
    for word, structure in PHRASES:
        if re.search(rf"\b{word}", low):
            return structure
    return None


# ---------------------------------------------------------------------------------------------------- L3 planner
HAIR_CHOICES = ("#3B2A1E", "#1F1F2E", "#7A4B2A", "#B5834A", "#2E4A3B", "#6B2F4A", "#D9C7A0", "#C45A3A")


def _separate_hair_colours(spec: dict[str, Any], inv: Any) -> None:
    """Move the two hair colours away from every other palette colour (the generator derives them from the neutral, and a hair colour that
    sits within dE 12 of a neutral, or of a skin tone, makes the partner's clothing or skin a colour leak in the concept check A_LEAK)."""
    from duoskin.imaging.palette import de2000_hex
    from duoskin.providers.mock import roles as R

    for role in ("hair_a", "hair_b"):
        entry = next((c for c in spec["palette"] if c["role"] == role), None)
        if entry is None:
            continue
        others = [c["hex"] for c in spec["palette"] if c is not entry] + [t.hex for t in inv.skin_tones.values()]
        best = max(HAIR_CHOICES, key=lambda h: min(de2000_hex(h, o) for o in others))
        entry["hex"] = best
        entry["name"] = R._name_of((int(best[1:3], 16), int(best[3:5], 16), int(best[5:7], 16)))


def _separate_skin(spec: dict[str, Any], inv: Any) -> None:
    """Pick each character's skin tone as far as possible from every palette colour (a skin tone near the partner's colour is a leak)."""
    from duoskin.imaging.palette import de2000_hex

    hexes = [c["hex"] for c in spec["palette"]]
    for c in ("a", "b"):
        spec[c]["body"]["skin_tone"] = max(inv.skin_tones, key=lambda sid: min(de2000_hex(inv.skin_tones[sid].hex, h) for h in hexes))


def _lint_ready(spec: dict[str, Any], plan: int, inv: Any) -> None:
    """Touch up what the base generator leaves for the plan linter: real kit hair styles (never ``hair_custom``), a hair ornament in the
    hat category, no banned word in a description, and a lash colour at least 10 dE2000 away from both iris colours (the generator reuses
    the dark neutral for all three)."""
    from duoskin.imaging.palette import de2000_hex

    _separate_hair_colours(spec, inv)
    _separate_skin(spec, inv)
    pal = {c["id"]: c["hex"] for c in spec["palette"]}
    styles = [h for h in inv.ids("HairKit") if h != "hair_custom"]
    for k, c in enumerate(("a", "b")):
        ch = spec[c]
        if len(styles) >= 2:
            ch["hair"]["kit_style_id"] = styles[(plan + k) % len(styles)]
            ch["hair"]["fringe_id"] = ch["hair"]["back_id"] = "kit_default"
        for acc in ch.get("accessories", []):
            acc["description"] = re.sub(r",?\s*no text\b", "", acc["description"]).strip()
            if acc.get("kind") == "hair_clip_slab":
                acc["category"], acc["attachment"] = "hat", "hat"
        if spec["world"]["pair_structure"] == "object_mascot" and ch.get("accessories"):
            kind, cat, att, desc = (("plush_pet", "shoulder", "right_shoulder", "a small plush pet in the main colours") if c == "a" else
                                    ("keychain_charm", "waist", "waist_front", "a tiny charm on a short chain in the second colours"))
            ch["accessories"][0].update({"kind": kind, "category": cat, "attachment": att, "build": "tripo",
                                         "material": "plush" if c == "a" else "enamel_flat", "linked_to_partner": True, "description": desc})
        face = ch["face"]
        refs = [face["iris_ref"], face["iris_dark_ref"]]
        best = max(pal, key=lambda pid: min(de2000_hex(pal[pid], pal[r]) for r in refs if r in pal))
        face["lash_ref"] = best


def planner_builder(call: Any) -> dict[str, Any]:
    """``PlanSet`` for L3 (see the module docstring)."""
    from duoskin.models import kitenums
    from duoskin.models.spec import PlanSet
    from duoskin.providers.mock import roles as R

    inv = kitenums.current_inventory()
    text, rng, brief = call.content_text, call.rng, call.brief
    low = brief.lower()
    combo = _tag(text, "combo")
    combo = combo if combo in ("bb", "gg", "bg", "gb") else R._combo(brief)
    request = _tag(text, "structure_request")
    named = request if request in R.STRUCTURES else structure_from_brief(brief)
    avoid = _json_tag(text, "avoid")
    avoid = [a for a in avoid if isinstance(a, dict)] if isinstance(avoid, list) else []
    replacement = "Return exactly one replacement spec" in text
    want = re.search(r"with is_wildcard (true|false)", text)
    structure_pool = [s for s in R.STRUCTURES if s not in {a.get("pair_structure") for a in avoid}]
    structure_pool = structure_pool if len(structure_pool) >= 3 else list(R.STRUCTURES)
    family_pool = [f for f in R.FAMILY_HUES if f not in {a.get("palette_family") for a in avoid}]
    family_pool = family_pool if len(family_pool) >= 3 else list(R.FAMILY_HUES)
    structures = [named] * 3 if named else rng.sample(structure_pool, 3)
    families = rng.sample(family_pool, 3)
    wildcard = rng.randrange(3)
    if replacement:
        wildcard = 0 if (want and want.group(1) == "true" and "[mock:no_wildcard_replacement]" not in low) else -1
    brief_colors = D.colors_from_text(brief, 3)
    count = 1 if replacement else 3
    plan_offset = rng.randrange(3) if replacement else 0
    specs = [R._spec(inv, (i + plan_offset) % 3, structures[i], families[i], i == wildcard, combo, brief, brief_colors if i != wildcard else [], rng)
             for i in range(count)]
    used_themes = {a.get("theme") for a in avoid}
    themes = [t for t in R.THEMES if t not in used_themes] or list(R.THEMES)
    for i, spec in enumerate(specs):
        spec["world"]["theme"] = _words(themes[(i + rng.randrange(len(themes))) % len(themes)], 8)
    if not replacement and "[mock:notbuildable]" in low:
        specs[0]["b"]["accessories"][0]["description"] = "a flowing cape on the back in the main colours"
    for i, spec in enumerate(specs):
        _lint_ready(spec, i + plan_offset, inv)
    must = [ln.strip(" -*\t") for ln in _tag(text, "must_include").splitlines() if ln.strip(" -*\t") and ln.strip() != "none"]
    constraints = [{"text": _words(ln, 12), "spec_paths": ["/a/accessories/0"]} for ln in must]
    out = {"specs": specs, "brief_constraints": constraints,
           "how_they_differ": _words("The plans differ in pair structure, theme, palette family and accessories; one is the wildcard with a bolder "
                                     "palette and anchor.", 40)}
    broken = False
    if not replacement:
        if "[mock:dangling]" in low:
            specs[1]["a"]["top"]["base_ref"], broken = "p99", True
        elif "[mock:unfixable]" in low:
            specs[1]["a"]["top"]["base_ref"], broken = "p98", True
        elif "[mock:unfixable_wildcard]" in low:
            specs[wildcard]["a"]["top"]["base_ref"], broken = "p98", True
    if not broken and not replacement:
        PlanSet.model_validate(out)                  # fail loudly here if a rule changed under this builder
    return out


# ---------------------------------------------------------------------------------------------------- L4, L5
def _spec_from(text: str, tag_name: str = "spec") -> Any:
    m = re.search(rf"<{tag_name}(?:\s[^>]*)?>(.*?)</{tag_name}>", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def critique_builder(call: Any) -> dict[str, Any]:
    from duoskin.models.llm_io import CRITERIA

    text = call.content_text
    spec_text = (re.search(r"<spec(?:\s[^>]*)?>(.*?)</spec>", text, re.DOTALL) or [None, ""])[1]
    h = _hash(spec_text)
    wildcard = "wildcard: ignore taste_fit" in text
    levels = ("ok", "strong", "ok", "weak", "strong", "ok")
    scores = []
    for i, crit in enumerate(CRITERIA):
        level = levels[(h + i) % len(levels)]
        if crit == "taste_fit" and wildcard:
            level = "ok"
        scores.append({"criterion": crit, "evidence": f"{crit.replace('_', ' ')} judged from the contrasts and anchors in the spec", "level": level})
    fixes = []
    if "[mock:critic_fix]" in text.lower():
        fixes.append({"path": "/a/accessories/0/description", "problem": "the accessory description is vague", "severity": "high",
                      "direction": "name the colour and the shape in fewer words"})
    return {"scores": scores, "fixes": fixes}


def _strength(spec: Any) -> tuple[int, int]:
    if not isinstance(spec, dict):
        return (0, 0)
    return (len(spec.get("contrasts", []) or []), _hash(json.dumps(spec, sort_keys=True)) % 97)


def pair_builder(call: Any) -> dict[str, Any]:
    from duoskin.models.llm_io import CRITERIA

    text = call.content_text
    first = _spec_from(text.replace('<spec id="first">', '<spec id="first" >'), "spec")
    second_m = re.search(r'<spec id="second">(.*?)</spec>', text, re.DOTALL)
    second = None
    if second_m:
        try:
            second = json.loads(second_m.group(1))
        except ValueError:
            second = None
    s1, s2 = _strength(first), _strength(second)
    overall = "first" if s1 > s2 else "second" if s2 > s1 else "tie"
    rows = []
    for i, crit in enumerate(CRITERIA):
        better = overall if i % 4 else "tie"
        rows.append({"criterion": crit, "evidence": f"{crit.replace('_', ' ')} compared on the spec fields", "better": better})
    return {"per_criterion": rows, "overall": overall}


# ---------------------------------------------------------------------------------------------------- L6 reviser
_FINDING = re.compile(r"^\s*(\d+)\.\s+(/\S*):\s*(.*?)(?:\s+\(([\w\-]+)\))?\s*$")
_REF_ROLE = {"a": {"base_ref": "p1", "second_ref": "p2", "trim_ref": "p5"}, "b": {"base_ref": "p3", "second_ref": "p4", "trim_ref": "p5"}}


def revision_builder(call: Any) -> dict[str, Any]:
    text = call.content_text
    spec = _spec_from(text) or {}
    ids = {c.get("id") for c in spec.get("palette", []) if isinstance(c, dict)}
    patch: list[dict[str, str]] = []
    for line in _tag(text, "findings").splitlines():
        m = _FINDING.match(line)
        if not m:
            continue
        number, path, problem, rule = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        value = _get(spec, path)
        op: dict[str, str] | None = None
        if path.endswith("/is_wildcard"):
            op = {"op": "replace", "path": path, "value_json": "true" if "to true" in problem else "false", "finding": number}
        elif isinstance(value, str) and re.fullmatch(r"p\d+", value) and value not in ids:
            if value != "p98":                                    # p98 is the unfixable test reference
                who = path.split("/")[1] if path.count("/") > 1 else "a"
                new = _REF_ROLE.get(who, _REF_ROLE["a"]).get(path.rsplit("/", 1)[-1], "p5")
                op = {"op": "replace", "path": path, "value_json": json.dumps(new), "finding": number}
        elif rule == "critic" and isinstance(value, str):
            words = value.split()
            new = " ".join(words[:6] + ["tidy"]) if len(words) > 6 else value + " tidy"
            op = {"op": "replace", "path": path, "value_json": json.dumps(new), "finding": number}
        else:
            m2 = re.match(r"^/brief_constraints/(\d+)", path)
            if m2:
                op = {"op": "replace", "path": f"/brief_constraints/{m2.group(1)}/spec_paths", "value_json": json.dumps(["/a/accessories/0"]),
                      "finding": number}
        if op:
            patch.append(op)
    return {"patch": patch, "note": _words(f"fixed {len(patch)} finding(s) with the smallest change", 40)}


# ---------------------------------------------------------------------------------------------------- L1, L2
def reference_builder(call: Any) -> dict[str, Any]:
    images = sum(1 for b in call.content if isinstance(b, dict) and b.get("type") == "image")
    rules = [
        ("line_weight", "outlines are thin and even", "keep every outline thin and even in weight"),
        ("shading", "shadows are flat single tones", "use flat single-tone shadows without gradients"),
        ("palette", "three colours dominate", "let three colours cover most of each outfit"),
        ("silhouette", "the shapes stay simple and rounded", "keep simple rounded silhouettes that read small"),
        ("duo_linking", "one shared detail ties the pair together", "tie the pair with one shared detail on both"),
        ("detail_density", "few details per garment", "limit each garment to one hero detail"),
    ]
    return {"rules": [{"axis": a, "observation": o, "rule": r} for a, o, r in rules[:max(3, min(6, 2 + images))]],
            "duo_devices": ["a shared colour at the same body spot"], "quality_bar": ["clean readable shapes at thumbnail size"],
            "do_not_copy": ["the exact motif drawn in the first picture"], "brand_or_character_flags": []}


def taste_builder(call: Any) -> dict[str, Any]:
    tables = _json_tag(call.content_text, "frequency_tables") or {}
    likes: list[dict[str, Any]] = []
    dislikes: list[dict[str, Any]] = []
    for field_name, values in (tables.get("fields") or {}).items():
        for value, row in sorted((values or {}).items()):
            approved, rejected = list(row.get("approved", [])), list(row.get("rejected", []))
            if len(approved) >= 2 and len(likes) < 4:
                likes.append({"field": field_name, "tendency": _words(f"prefers {value.replace('_', ' ')}", 15), "evidence_ids": approved[:3],
                              "strength": "moderate"})
            elif len(rejected) >= 2 and len(dislikes) < 4:
                dislikes.append({"field": field_name, "tendency": _words(f"avoids {value.replace('_', ' ')}", 15), "evidence_ids": rejected[:3],
                                 "strength": "weak"})
    return {"likes": likes, "dislikes": dislikes, "open_questions": ["Which hair style does the person prefer?"],
            "explore": ["a calmer palette", "a different pair structure"]}


# ---------------------------------------------------------------------------------------------------- L15
def inventory_builder(call: Any) -> dict[str, Any]:
    spec = _spec_from(call.content_text) or {}
    blob = json.dumps(spec).lower()
    items: list[dict[str, Any]] = []
    for c in ("a", "b"):
        ch = spec.get(c) or {}
        top = (ch.get("top") or {}).get("recipe_id")
        if top:
            items.append({"element": _words(f"{str(top).replace('_', ' ')} top", 8), "where": f"front, character {c}", "in_spec": True,
                          "buildable": True, "suggested_spec_path": ""})
        for i, acc in enumerate(ch.get("accessories") or []):
            desc = str(acc.get("description", "")).lower()
            bad = any(w in desc for w in UNBUILDABLE_WORDS)
            name = "flowing cape" if "cape" in desc else ("wings" if "wings" in desc else _words(str(acc.get("kind", "accessory")).replace("_", " "), 8))
            items.append({"element": name, "where": f"back, character {c}" if bad else f"front, character {c}", "in_spec": True,
                          "buildable": not bad, "suggested_spec_path": ""})
    if not items:
        items.append({"element": "plain outfit", "where": "front", "in_spec": True, "buildable": True, "suggested_spec_path": ""})
    _ = blob
    return {"items": items[:20]}


# ---------------------------------------------------------------------------------------------------- L7
def change_builder(call: Any) -> dict[str, Any]:
    """``ChangePlan``: the built-in answer of ``providers.mock.roles`` with "her" and "his" resolved to a character first (from the clicked
    tile, else from the characters' presentation in ``<spec>``)."""
    from duoskin.providers.mock import roles as R

    text = call.content_text
    m = re.search(r"<user_change_request>(.*?)</user_change_request>", text, re.DOTALL)
    request = (m.group(1) if m else text).strip()
    spec = _spec_from(text) or {}
    clicked = _tag(text, "clicked_tile")
    who = ""
    mt = re.search(r"(?:^|[._])([ab])(?:[._]|$)", clicked) if clicked and clicked != "none" else None
    if mt:
        who = mt.group(1)
    if re.search(r"\b(her|she|hers)\b", request, re.IGNORECASE) or re.search(r"\b(his|he|him)\b", request, re.IGNORECASE):
        want = "girl" if re.search(r"\b(her|she|hers)\b", request, re.IGNORECASE) else "boy"
        if not who:
            who = next((c for c in ("a", "b") if (spec.get(c) or {}).get("presentation") == want), "a")
        request = re.sub(r"\b(her|his)\b", f"{who}'s", request, flags=re.IGNORECASE)
        request = re.sub(r"\b(she|he|him)\b", who, request, flags=re.IGNORECASE)
    elif who and not re.search(r"\b[ab]\b", request, re.IGNORECASE):
        request = f"{request} ({who}'s)"
    new_text = text.replace(m.group(0), f"<user_change_request>{request}</user_change_request>") if m else text
    return R.change_builder(dataclasses.replace(call, content_text=new_text))
