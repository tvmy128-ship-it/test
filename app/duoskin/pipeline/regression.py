"""The regression job and the variety guard (APP_SPEC §3.9, §17.2; FAILURE_MODES ENG-08, PROPOSAL_DECISION "Variety guard").

Why it exists: a change that makes designs safer also makes them more alike, and "safer" always wins on a pass rate. So every change to
the prompts, the models, the thresholds or the kits is run on the SAME inputs before and after, and it is kept only if

* the mean pairwise picture distance of the concept sheets (DreamSim, or pHash while there is no ``dreamsim.onnx``) does not fall by
  more than ``calib.variety_drop_max`` (5%, relative),
* the **variety index** (evenness and spread of hair kits, eye shapes, mouths, pair structures, palette families, anchor kinds and
  accessory types, the counts of distinct structures, hair styles and palette families, the mean pairwise spec distance and the spread
  of face-grammar combinations) does not fall by more than the same 5%, and
* the **quality score** (the critic's levels and the plan linter's pass share) does not fall (``calib.quality_drop_max`` = 0).

Stages (APP_SPEC §3.9): ``plan`` runs the fixed brief set (``DATA/regression/briefs.json``: 40 briefs, deterministic, user-editable,
``generate_briefs``) through the plan loop up to Gate 1 and measures the top-ranked plan; ``parts`` runs ONLY the affected part
templates on a frozen set of 10 to 20 approved specs (``DATA/regression/parts_set/``). Nothing is approved, exported or registered:
the hidden ``[regression] <id>`` projects are archived, their assets are tagged ``stream="regression"``, their specs never become taste
evidence, and the weekly report leaves them out.

Cost is gated: ``estimate`` quotes the job, the REGRESSION job carries it (``params["estimate_usd"]``) and the scheduler opens one
BUDGET gate above ``budgets.regression_ask_usd``. Mock providers cost nothing and need no confirmation.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import shutil
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from pydantic import Field, field_validator

from duoskin.checks import thresholds as TH
from duoskin.engine import calibration as cal
from duoskin.engine import registry as eng
from duoskin.engine import scheduler as sched
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import Pending, StepResult
from duoskin.models.common import Strict, iso_utc, new_id, parse_iso, sha256_of, utcnow
from duoskin.models.job import Job, JobKind, JobState, Step
from duoskin.models.project import Project, ProjectSettings, Stage
from duoskin.models.settings import ModelPins

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.regression")

STAGES = ("plan", "parts")
RUNS_KEY = "regression:runs"                     # kv: run ids, oldest first
RUN_PREFIX = "regression:run:"
BASELINE_PREFIX = "regression:baseline:"         # + stage: the run id the guard compares candidates with
BRIEF_PREFIX = "regression:brief:"               # + run:arm:index: the state and result of one brief
BRIEF_TIMEOUT_S = {"mock": 600.0, "real": 2400.0}
POLL_S = {"mock": 1.5, "real": 6.0}
CONCURRENCY = 3                                  # briefs of one arm running at once (the Claude concurrency default)
#: what one brief costs with real providers: the plan loop, $1.2 to $3.0, and the Gate 1 previews, $0.8 to $1.5 (APP_SPEC §3.9, bible §20.2)
BRIEF_USD = (2.0, 4.5)
PART_USD = (0.10, 0.30)                          # one part of one frozen spec on one template: "a few dollars per template" over 10 to 20 specs
BRIEF_CAP_USD = 25.0                             # the hidden projects ask for nothing: the regression job was confirmed as a whole
MODEL_ROLES = tuple(f for f, t in ModelPins.model_fields.items() if t.annotation is str)
LEVEL_POINTS = {"fail": 0, "weak": 1, "ok": 2, "strong": 3}            # bible §9.5 (the critic's levels), the same table the planner ranks with
LEVEL_MAX = max(LEVEL_POINTS.values())
CATEGORIES = ("hair_kit_ids", "eye_shapes", "mouths", "structures", "palette_families", "anchor_kinds", "accessory_types")
#: template id of the bible -> the part kind it makes (``--template I2`` of the CLI); a kind name is accepted too
TEMPLATE_KINDS = {"R1": "face", "I3": "face", "C4": "face", "I2": "print", "R2": "print", "I6": "accessory", "I4": "hair"}
PART_KINDS = ("face", "print", "accessory", "hair", "shirt", "pants")


class RegressionError(RuntimeError):
    """The regression cannot be started or promoted; ``code`` is the API error code (409 for ``busy``/``guard``, 422 otherwise)."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


# ======================================================================================================================
# the brief set
# ======================================================================================================================
COMBOS = ("bb", "gg", "bg", "gb")
#: 40 themes, one per brief: no brand, no known character, and none of the words that name a pair structure ("twin", "club", "team", ...), so an
#: open brief stays open (``brief.brief_structure`` finds nothing in any of them)
THEMES = (
    "a night market of paper lanterns", "a rooftop garden above a busy street", "a deep-sea research station", "a desert caravan at dusk",
    "an old library after closing time", "a snowy mountain cabin", "a roller rink in the eighties", "a tide-pool adventure at low tide",
    "a bakery before sunrise", "a space-port cafe", "a rainy bus stop in a seaside town", "a hidden greenhouse of giant ferns",
    "a school science fair", "a lighthouse keeper's holiday", "a skate park under neon lights", "a thrift-store treasure hunt",
    "a forest camp with fireflies", "a rainy-day picnic under one umbrella", "a retro arcade on a Friday night", "a quiet tea house in the hills",
    "a harvest festival with scarecrows", "a robot repair shop", "a moonlit ballroom rehearsal", "a mossy ruin explored by torchlight",
    "a flower stall on market day", "a lazy river float on a hot afternoon", "a clockwork toy workshop", "a mountain bike trail at sunrise",
    "a street-food alley after a storm", "a stargazing night on a hilltop", "a quiet museum of tiny things", "a pirate-ship birthday party",
    "a pottery studio with clay-stained aprons", "a pop-up circus tent", "a coral-reef snorkel trip", "a cosy blanket fort sleepover",
    "a vintage train carriage journey", "a graffiti wall in a sunny alley", "a bonfire on a winter beach", "a tiny orchard in blossom")
MOODS = ("calm", "mischievous", "dreamy", "bold", "cosy", "energetic", "mysterious", "playful", "elegant", "rebellious")
RELATIONS = ("best friends", "cousins", "classmates", "neighbours", "pen pals", "travel buddies", "old rivals", "band mates")
STRUCTURE_CYCLE = ("auto", "complement", "auto", "leader_chaotic", "auto", "same_club", "auto", "mirror", "auto", "seasonal_twins", "auto",
                   "object_mascot", "auto", "other")
#: must-include lines by accessory kind (7 kinds x 5 lines), each at most 12 words
ACCESSORY_LINES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plush_pet", ("a small plush fox on one shoulder", "a plush frog sitting on the hat", "a little plush octopus clipped to the bag",
                   "a plush cloud friend tied to the wrist", "a tiny plush dragon on the back")),
    ("keychain_charm", ("a star keychain charm on the belt", "a teal ribbon keychain", "a paper-crane charm on the waist", "a tiny lantern charm",
                        "a pair of dice charms on the hip")),
    ("bag", ("a bright yellow satchel", "a small backpack with a round pocket", "a patched messenger bag", "a mini bucket bag on the hip",
             "a canvas tote with a button")),
    ("small_hat", ("a flat cap with a button on top", "a knitted beanie with a pompom", "a bucket hat with a ribbon",
                   "a tiny top hat tilted to the side", "a paper boat hat")),
    ("hair_clip_slab", ("two star hair clips", "a big bow hair clip", "a row of tiny leaf hair clips", "a moon hair clip", "a flower hair slide")),
    ("sticker_slab", ("a round sticker badge on the chest", "a patch badge on the shoulder", "a lightning bolt sticker on the sleeve",
                      "a heart badge on the pocket", "a small mountain badge on the back")),
    ("prop", ("a folded paper fan", "a small umbrella carried in one hand", "a tiny telescope", "a toy kite on a string", "a lantern on a stick")),
)
LINES = tuple((kind, line) for kind, lines in ACCESSORY_LINES for line in lines)         # 35 (kind, line) pairs, five per kind


class RegressionBrief(Strict):
    """One brief of the fixed set: the fields of the brief form (APP_SPEC §6.3) plus tags that say why it is in the set."""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,24}$")
    combo: Literal["bb", "gg", "bg", "gb"]
    brief: str = Field(max_length=2000)
    structure_request: str = "auto"
    must_include: list[str] = Field(default_factory=list, max_length=5)
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("structure_request")
    @classmethod
    def _known_structure(cls, v: str) -> str:
        from duoskin.models.spec import PAIR_STRUCTURES

        if v != "auto" and v not in PAIR_STRUCTURES:
            raise ValueError(f"unknown pair structure {v!r}")
        return v

    @field_validator("must_include")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        for line in v:
            if len(line.split()) > 12:
                raise ValueError("each must-include line has at most 12 words")
        return v

    @field_validator("brief")
    @classmethod
    def _no_key(cls, v: str) -> str:
        from duoskin.logsetup import reject_key_text

        return reject_key_text(v)


def generate_briefs(n: int | None = None) -> list[RegressionBrief]:
    """The fixed regression set, a pure function of ``n`` (no randomness, no clock): the same 40 briefs on every machine.

    Every axis cycles with a stride that is coprime to its length, so the axes do not line up: gender combination (10 of each of the 4),
    theme (all different), mood (10), the relationship between the two, the pair structure (half open, half one of the seven structures; the
    index also steps once per block of four briefs, so no gender combination is tied to "open" or to one structure) and the accessory lines (35, every kind five times; a fifth of the briefs have none, a fifth have two)."""
    count = int(n if n is not None else TH.get("calib.regression_briefs"))
    out: list[RegressionBrief] = []
    for i in range(count):
        theme = THEMES[i % len(THEMES)]
        mood = MOODS[(i * 3) % len(MOODS)]
        relation = RELATIONS[(i * 5) % len(RELATIONS)]
        lines: list[tuple[str, str]] = []
        if i % 5 != 4:
            lines.append(LINES[(i * 3) % len(LINES)])
            if i % 5 == 2:
                lines.append(LINES[(i * 3 + 11) % len(LINES)])
        out.append(RegressionBrief(
            id=f"rb{i + 1:02d}", combo=COMBOS[i % len(COMBOS)], brief=f"Two {relation} in {theme}. The mood is {mood}.",
            structure_request=STRUCTURE_CYCLE[(i * 3 + i // 4) % len(STRUCTURE_CYCLE)], must_include=[line for _, line in lines],
            tags={"theme": theme, "mood": mood, "relation": relation, "accessory_kinds": ",".join(kind for kind, _ in lines) or "none"}))
    return out


def briefs_sha(briefs: Sequence[RegressionBrief]) -> str:
    return sha256_of([b.model_dump(mode="json") for b in briefs])


@dataclass
class BriefSet:
    briefs: list[RegressionBrief]
    sha256: str
    path: Path
    edited: bool                  # the person changed the file: the set is theirs, and its sha is part of every run
    version: int = 1


def ensure_briefs(rt: Runtime | None = None, *, path: Path | None = None) -> BriefSet:
    """Load ``DATA/regression/briefs.json`` (written on first use). A file the person edited is used as it is; one that cannot be read is
    moved aside (``briefs.json.bad``) and the generated set is written again."""
    target = path or (rt.paths.regression_dir / "briefs.json" if rt is not None else None)
    if target is None:
        raise ValueError("ensure_briefs needs a runtime or a path")
    generated = generate_briefs()
    gen_sha = briefs_sha(generated)
    if target.exists():
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
            briefs = [RegressionBrief.model_validate(b) for b in doc["briefs"]]
            if not briefs or len({b.id for b in briefs}) != len(briefs):
                raise ValueError("empty set or duplicate brief ids")
            sha = briefs_sha(briefs)
            return BriefSet(briefs, sha, target, edited=sha != gen_sha, version=int(doc.get("version", 1)))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            bad = target.with_suffix(".json.bad")
            log.warning("the regression brief set could not be read (%s); it was moved to %s", exc, bad.name)
            with contextlib.suppress(OSError):
                shutil.move(str(target), str(bad))
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "generator": "duoskin.pipeline.regression.generate_briefs", "count": len(generated), "sha256": gen_sha,
           "note": "The fixed brief set of the regression test (APP_SPEC 3.9). You may edit it; every run records its sha256.",
           "briefs": [b.model_dump(mode="json") for b in generated]}
    target.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return BriefSet(generated, gen_sha, target, edited=False)


def sample_briefs(briefs: Sequence[RegressionBrief], n: int | None) -> list[RegressionBrief]:
    """A deterministic sample of ``n`` briefs that keeps the mix: the quota of every gender combination is as even as it can be and each
    combination contributes briefs spread evenly through its part of the set. ``None`` or ``n >= len`` returns all of them."""
    if n is None or n >= len(briefs):
        return list(briefs)
    n = max(1, int(n))
    groups: dict[str, list[RegressionBrief]] = {}
    for b in briefs:
        groups.setdefault(b.combo, []).append(b)
    keys = sorted(groups)
    quota = {k: n // len(keys) + (1 if i < n % len(keys) else 0) for i, k in enumerate(keys)}
    picked: list[RegressionBrief] = []
    for k in keys:
        g = groups[k]
        q = min(quota[k], len(g))
        picked += [g[(j * len(g)) // q + (len(g) // q) // 2 if q else 0] for j in range(q)]
    order = {b.id: i for i, b in enumerate(briefs)}
    return sorted({b.id: b for b in picked}.values(), key=lambda b: order[b.id])


# ======================================================================================================================
# variety metrics
# ======================================================================================================================
def spec_features(spec: Mapping[str, Any]) -> dict[str, list[str]]:
    """The values of each variety category in one spec (both characters count, so a duo adds two hair kits, two eye shapes ...)."""
    chars = [spec.get("a") or {}, spec.get("b") or {}]
    world = spec.get("world") or {}
    return {
        "hair_kit_ids": [str((c.get("hair") or {}).get("kit_style_id", "")) for c in chars],
        "eye_shapes": [str((c.get("face") or {}).get("eye_shape", "")) for c in chars],
        "mouths": [str((c.get("face") or {}).get("mouth_style", "")) for c in chars],
        "structures": [str(world.get("pair_structure", ""))],
        "palette_families": [str(world.get("palette_family", ""))],
        "anchor_kinds": [str(a.get("kind", "")) for a in spec.get("shared_anchors") or []],
        "accessory_types": [str(a.get("kind", "")) for c in chars for a in c.get("accessories") or []],
    }


FACE_FIELDS = ("eye_shape", "iris_style", "lash_style", "brow_style", "mouth_style", "nose_style", "cheek_mark")


def face_grammar(character: Mapping[str, Any]) -> tuple[str, ...]:
    """The face-grammar combination of one character (APP_SPEC §10.6): which eye, iris, lash, brow, mouth, nose and cheek the face is made of."""
    face = character.get("face") or {}
    return tuple(str(face.get(f, "")) for f in FACE_FIELDS)


#: the fields a pair of whole specs is compared on (spec distance = the share that differ)
SPEC_PATHS = (
    "world.pair_structure", "world.palette_family", "world.material_family", "world.detail_level", "lead",
    *[f"{c}.{p}" for c in ("a", "b") for p in (
        "hair.kit_style_id", "hair.fringe_id", "hair.back_id", "hair.parting", "face.eye_shape", "face.iris_style", "face.highlight_style",
        "face.lash_style", "face.brow_style", "face.mouth_style", "face.nose_style", "face.cheek_mark", "top.recipe_id", "top.sleeve", "top.hem",
        "top.neckline", "top.block_layout", "top.fabric_id", "bottom.recipe_id", "bottom.leg", "bottom.fabric_id", "bottom.shoes.style_id",
        "dna.shape_language", "dna.colour_plan", "dna.focal_location", "makeup.kind")])


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(part)
    return obj


def spec_signature(spec: Mapping[str, Any]) -> tuple[Any, ...]:
    sig = [_dig(spec, p) for p in SPEC_PATHS]
    sig.append(tuple(sorted(str(a.get("kind", "")) for a in spec.get("shared_anchors") or [])))
    for c in ("a", "b"):
        sig.append(tuple(sorted(str(a.get("kind", "")) for a in (spec.get(c) or {}).get("accessories") or [])))
    return tuple(sig)


def mean_pairwise_spec_distance(specs: Sequence[Mapping[str, Any]]) -> float:
    """Mean over all pairs of the share of compared spec fields that differ (0: every duo is the same, 1: no two share a field)."""
    sigs = [spec_signature(s) for s in specs]
    if len(sigs) < 2:
        return 0.0
    total, pairs = 0.0, 0
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            total += sum(1 for x, y in zip(sigs[i], sigs[j], strict=True) if x != y) / len(sigs[i])
            pairs += 1
    return total / pairs


def distribution(values: Sequence[str], domain: int | None = None) -> dict[str, Any]:
    """Counts, Shannon entropy (bits), evenness (entropy over the log of the domain size) and the share of the most used value."""
    vals = [v for v in values if v]
    n = len(vals)
    counts = Counter(vals)
    if not n:
        return {"n": 0, "distinct": 0, "entropy_bits": 0.0, "evenness": 0.0, "max_share": 1.0, "top": []}      # no choice at all: nothing is spread
    probs = [c / n for c in counts.values()]
    h = -sum(p * math.log2(p) for p in probs)
    size = max(int(domain or 0), len(counts), 2)
    return {"n": n, "distinct": len(counts), "entropy_bits": h, "evenness": h / math.log2(size), "max_share": max(probs),
            "top": [[k, c] for k, c in counts.most_common(3)]}


def category_domains() -> dict[str, int]:
    """How many values each category could take (for evenness): the static enums and the live kit inventory."""
    from duoskin.models import kitenums as K
    from duoskin.models.spec import ACCESSORY_KINDS, ANCHOR_KINDS, PAIR_STRUCTURES

    sizes = {"structures": len(PAIR_STRUCTURES), "palette_families": 10, "anchor_kinds": len(ANCHOR_KINDS), "accessory_types": len(ACCESSORY_KINDS)}
    try:
        inv = K.current_inventory()
        sizes.update({"hair_kit_ids": len(inv.ids("HairKit")), "eye_shapes": len(inv.ids("EyeShapeKit")), "mouths": len(inv.ids("MouthKit"))})
    except Exception:    # noqa: BLE001 - the domain only normalises; without it the observed count is used
        log.warning("the kit inventory could not be read for the variety metrics")
    return sizes


def spec_variety(specs: Sequence[Mapping[str, Any]], *, domains: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Everything about the variety of a set of specs that needs no picture (see the module docstring for the guard's use of it)."""
    domains = dict(domains if domains is not None else category_domains())
    pools: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    grammar: list[tuple[str, ...]] = []
    for spec in specs:
        for cat, vals in spec_features(spec).items():
            pools[cat] += vals
        grammar += [face_grammar(spec.get("a") or {}), face_grammar(spec.get("b") or {})]
    cats = {c: distribution(pools[c], domains.get(c)) for c in CATEGORIES}
    entropy = float(np.mean([cats[c]["evenness"] for c in CATEGORIES]))
    spread = float(np.mean([1.0 - cats[c]["max_share"] for c in CATEGORIES]))
    distinct = {"structures": cats["structures"]["distinct"], "hair_kits": cats["hair_kit_ids"]["distinct"],
                "palette_families": cats["palette_families"]["distinct"]}
    ratios = [distinct["structures"] / max(domains.get("structures", 0), distinct["structures"], 1),
              distinct["hair_kits"] / max(domains.get("hair_kit_ids", 0), distinct["hair_kits"], 1),
              distinct["palette_families"] / max(domains.get("palette_families", 0), distinct["palette_families"], 1)]
    combos = Counter(grammar)
    g_n = len(grammar)
    g_h = -sum((c / g_n) * math.log2(c / g_n) for c in combos.values()) if g_n else 0.0
    sd = mean_pairwise_spec_distance(specs)
    gs = len(combos) / g_n if g_n else 0.0
    index = float(np.mean([entropy, spread, float(np.mean(ratios)), sd, gs]))
    return {"n_specs": len(specs), "categories": cats, "category_entropy": entropy, "category_spread": spread, "distinct": distinct,
            "distinct_ratio": float(np.mean(ratios)), "spec_distance": sd,
            "grammar": {"combinations": len(combos), "characters": g_n, "entropy_bits": g_h}, "grammar_spread": gs, "variety_index": index}


def image_distance(images: Sequence[Any], model: Any = None) -> tuple[float, str]:
    """Mean pairwise distance of pictures: DreamSim when a model is given, else the normalised pHash Hamming distance (labelled degraded).
    Returns ``(mean, mode)``; fewer than two pictures give ``(0.0, mode)``."""
    from duoskin.imaging import similarity as S

    if model is not None:
        mode = "dreamsim"
        if len(images) < 2:
            return 0.0, mode
        if getattr(model, "kind", "") == "embedding":
            embs = [model.embed(im) for im in images]
            d = [S.DreamSimModel.cosine_distance(embs[i], embs[j]) for i in range(len(embs)) for j in range(i + 1, len(embs))]
        else:
            d = [model.distance(images[i], images[j]) for i in range(len(images)) for j in range(i + 1, len(images))]
        return float(np.mean(d)), mode
    if len(images) < 2:
        return 0.0, "degraded"
    bits = float(TH.get("sim.hash_bits"))
    hashes = [S.phash(im, crop_to_subject=True) for im in images]
    d = [S.hamming(hashes[i], hashes[j]) / bits for i in range(len(hashes)) for j in range(i + 1, len(hashes))]
    return float(np.mean(d)), "degraded"


# ======================================================================================================================
# quality
# ======================================================================================================================
def critic_score(levels: Mapping[str, str]) -> float | None:
    """The mean of the critic's levels (fail 0, weak 1, ok 2, strong 3) as a share of the best (0..1); ``None`` without any level."""
    pts = [LEVEL_POINTS[v] for v in levels.values() if v in LEVEL_POINTS]
    return float(np.mean(pts)) / LEVEL_MAX if pts else None


def lint_score(lint: Mapping[str, Any]) -> float | None:
    """The plan linter's pass share (C1: every rule that ran, HARD and SOFT) for one plan."""
    total = int(lint.get("total", 0))
    return int(lint.get("passed", 0)) / total if total else None


def plan_quality(plan: Mapping[str, Any]) -> float:
    """One plan's quality: the mean of its critic score and its lint score (the one it has when it has only one of them)."""
    parts = [x for x in (critic_score(plan.get("critic_levels") or {}), lint_score(plan.get("lint") or {})) if x is not None]
    return float(np.mean(parts)) if parts else 0.0


def brief_quality(result: Mapping[str, Any]) -> float:
    """A brief's quality: the mean over the plans Gate 1 shows; a brief that made no plans scores 0 (a regression that breaks the planner is a
    quality drop, not a missing data point)."""
    plans = result.get("plans") or []
    return float(np.mean([plan_quality(p) for p in plans])) if result.get("ok") and plans else 0.0


def lint_summary(results: Iterable[Any]) -> dict[str, int]:
    rows = [r for r in results if getattr(r, "ran", True)]
    return {"total": len(rows), "passed": sum(1 for r in rows if r.passed), "hard_failed": sum(1 for r in rows if r.kind in ("hard", "assert") and not r.passed),
            "soft_failed": sum(1 for r in rows if r.kind == "soft" and not r.passed)}


# ======================================================================================================================
# runs and the guard
# ======================================================================================================================
def run_key(run_id: str) -> str:
    return RUN_PREFIX + run_id


def save_run(rt: Runtime, run: Mapping[str, Any]) -> None:
    with rt.db.tx():
        rt.repo.kv_set(run_key(run["id"]), dict(run))
        ids = list(rt.repo.kv_get(RUNS_KEY) or [])
        if run["id"] not in ids:
            ids.append(run["id"])
            rt.repo.kv_set(RUNS_KEY, ids)


def load_run(rt: Runtime, run_id: str) -> dict[str, Any] | None:
    doc = rt.repo.kv_get(run_key(run_id))
    return dict(doc) if isinstance(doc, dict) else None


def reconcile_runs(rt: Runtime) -> int:
    """A run whose job was cancelled or stopped at the budget gate never reaches ``regression.finish``: mark it ``stopped`` (and switch a candidate
    model override off) so nothing waits for it. Returns how many runs changed."""
    changed = 0
    for rid in rt.repo.kv_get(RUNS_KEY) or []:
        run = load_run(rt, rid)
        if not run or run.get("state") in ("done", "stopped"):
            continue
        job = rt.repo.find_job(run.get("job_id", "")) if run.get("job_id") else None
        if job is not None and job.state in (JobState.FAILED, JobState.CANCELLED):
            run["state"] = "stopped"
            save_run(rt, run)
            changed += 1
    sweep_override(rt)
    return changed


def list_runs(rt: Runtime, stage: str | None = None) -> list[dict[str, Any]]:
    """Run records, newest first."""
    reconcile_runs(rt)
    runs = [r for r in (load_run(rt, i) for i in reversed(rt.repo.kv_get(RUNS_KEY) or [])) if r]
    return [r for r in runs if stage is None or r.get("stage") == stage]


def baseline_of(rt: Runtime, stage: str) -> dict[str, Any] | None:
    rid = rt.repo.kv_get(BASELINE_PREFIX + stage)
    return load_run(rt, str(rid)) if rid else None


def comparable(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """Why two runs cannot be compared (empty: they can). The guard only means something on the SAME inputs."""
    why = []
    for key, text in (("stage", "they test different stages"), ("brief_set_sha", "the brief sets differ"), ("n_briefs", "they used different numbers of briefs"),
                      ("image_mode", "one measured pictures with DreamSim and the other with the degraded hash")):
        if a.get(key) != b.get(key):
            why.append(f"{text} ({a.get(key)} vs {b.get(key)})")
    if sorted(a.get("template_kinds") or []) != sorted(b.get("template_kinds") or []):
        why.append("they tested different part templates")
    return why


@dataclass
class GuardDecision:
    """The variety guard's answer. ``decision`` is ``accept``, ``reject``, ``needs_baseline`` or ``incomparable``; only ``accept`` allows a
    promotion. ``checks`` lists every comparison with the numbers; ``reasons`` is the same in words, for the person."""

    decision: str
    reasons: list[str]
    checks: list[dict[str, Any]] = field(default_factory=list)
    baseline_id: str = ""
    candidate_id: str = ""
    rating_basis: str = "judge score (the critic's levels and the plan linter), no person's rating"

    @property
    def accepted(self) -> bool:
        return self.decision == "accept"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["accepted"] = self.accepted
        return d


def relative_drop(baseline: float, candidate: float) -> float:
    """``(baseline - candidate) / baseline``: positive when the candidate is lower. A baseline of 0 cannot drop."""
    return (baseline - candidate) / baseline if baseline > 0 else 0.0


def variety_guard(baseline: Mapping[str, Any] | None, candidate: Mapping[str, Any], *, drop_max: float | None = None,
                  quality_drop_max: float | None = None) -> GuardDecision:
    """Compare a candidate run with the baseline run on the same inputs.

    REJECTED when any of these holds (the numbers are ``calib.variety_drop_max`` = 0.05 and ``calib.quality_drop_max`` = 0.0):

    1. the mean pairwise picture distance (``image_distance``) fell by MORE than 5% (relative to the baseline);
    2. the ``variety_index`` fell by MORE than 5%;
    3. the quality score fell by more than 0 (any drop at all);
    4. a candidate model snapshot was named but never served during the candidate run.

    A candidate that raises the pass rate or the rating but fails 1 or 2 is rejected: that is the whole point of the guard. Without a baseline,
    or on different inputs, the answer is ``needs_baseline`` / ``incomparable``: never an accept."""
    cid = str(candidate.get("id", ""))
    if baseline is None:
        return GuardDecision("needs_baseline", ["There is no baseline run yet: run the regression test once without a candidate first."], candidate_id=cid)
    bid = str(baseline.get("id", ""))
    why = comparable(baseline, candidate)
    if why:
        return GuardDecision("incomparable", ["The two runs cannot be compared: " + "; ".join(why) + "."], baseline_id=bid, candidate_id=cid)
    vmax = float(drop_max if drop_max is not None else TH.get("calib.variety_drop_max"))
    qmax = float(quality_drop_max if quality_drop_max is not None else TH.get("calib.quality_drop_max"))
    bm, cm = baseline.get("metrics") or {}, candidate.get("metrics") or {}
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    def add(name: str, label: str, b: float, c: float, limit: float, *, gating: bool = True) -> None:
        drop = relative_drop(b, c)
        ok = drop <= limit + 1e-9
        checks.append({"name": name, "label": label, "baseline": b, "candidate": c, "change": -drop, "limit": limit, "ok": ok, "gating": gating})
        if gating and not ok:
            reasons.append(f"{label} fell {drop:.1%} ({b:.3f} to {c:.3f}); the limit is {limit:.0%}.")

    if candidate.get("image_mode") != "none":
        add("image_distance", "How different the concept pictures are from each other", float(bm.get("image_distance", 0.0)), float(cm.get("image_distance", 0.0)), vmax)
    add("variety_index", "The variety index (kits, structures, palettes, accessories, faces)", float(bm.get("variety_index", 0.0)),
        float(cm.get("variety_index", 0.0)), vmax)
    for name, label in (("category_entropy", "Evenness of the kit and style choices"), ("category_spread", "Spread (no choice dominates)"),
                        ("distinct_ratio", "Distinct pair styles, hair styles and palettes"), ("spec_distance", "Mean difference between two specs"),
                        ("grammar_spread", "Spread of face combinations")):
        if name in bm and name in cm:
            add(name, label, float(bm[name]), float(cm[name]), vmax, gating=False)
    bq, cq = float(baseline.get("quality", 0.0)), float(candidate.get("quality", 0.0))
    q_ok = bq - cq <= qmax + 1e-9
    checks.append({"name": "quality", "label": "The quality score (critic levels and plan linter)", "baseline": bq, "candidate": cq, "change": cq - bq,
                   "limit": qmax, "ok": q_ok, "gating": True})
    if not q_ok:
        reasons.append(f"The quality score fell ({bq:.3f} to {cq:.3f}); it may not fall at all." if qmax == 0.0 else
                       f"The quality score fell by {bq - cq:.3f} ({bq:.3f} to {cq:.3f}); the limit is {qmax:g}.")
    served = set(candidate.get("served_models") or [])
    for role, version in sorted((candidate.get("candidate_versions") or {}).get("models", {}).items()):
        ok = version in served
        checks.append({"name": f"served:{role}", "label": f"The candidate {role} model was used", "baseline": 0.0, "candidate": 1.0 if ok else 0.0,
                       "change": 0.0, "limit": 0.0, "ok": ok, "gating": True})
        if not ok:
            reasons.append(f"The candidate {role} version {version} was never used in this run, so it has not been tested.")
    if candidate.get("failed_briefs") and len(candidate["failed_briefs"]) > len(baseline.get("failed_briefs") or []):
        reasons.append(f"{len(candidate['failed_briefs'])} brief(s) made no plans in the candidate run "
                       f"(baseline: {len(baseline.get('failed_briefs') or [])}); they count as quality 0.")
    if not reasons and any(c["gating"] and not c["ok"] for c in checks):
        reasons.append("A guard check failed.")
    decision = "reject" if any(c["gating"] and not c["ok"] for c in checks) else "accept"
    if decision == "accept":
        reasons = ["Variety and quality held on the same briefs: the version may be promoted."]
    return GuardDecision(decision, reasons, checks, bid, cid)


# ======================================================================================================================
# the estimate
# ======================================================================================================================
def _live_providers(rt: Runtime, stage: str) -> bool:
    """Do the providers this stage uses cost real money? (Mock and disabled providers cost nothing.)"""
    from duoskin.pipeline import common

    names = ("anthropic", "openai") if stage == "plan" else ("openai", "recraft", "tripo")
    return any(common.provider_mode(rt, p) == "real" for p in names)


def estimate(rt: Runtime, stage: str = "plan", *, briefs: int | None = None, specs: int | None = None, templates: int = 1, arms: int = 1) -> dict[str, Any]:
    """The cost quoted before a regression job starts: low, high and the figure the budget gate judges (the high one), per arm and in all."""
    live = _live_providers(rt, stage)
    if stage == "plan":
        n = int(briefs if briefs is not None else TH.get("calib.regression_briefs"))
        lo, hi = BRIEF_USD[0] * n, BRIEF_USD[1] * n
        what = f"{n} briefs through the plan loop and the Gate 1 previews"
    else:
        n = int(specs if specs is not None else 0)
        lo, hi = PART_USD[0] * n * templates, PART_USD[1] * n * templates
        what = f"{n} frozen specs on {templates} part template(s)"
    lo, hi = (lo * arms, hi * arms) if live else (0.0, 0.0)
    ask = float(rt.effective_settings().budgets.regression_ask_usd)
    return {"stage": stage, "live": live, "arms": arms, "usd_low": round(lo, 2), "usd_high": round(hi, 2), "usd": round(hi, 2), "what": what,
            "ask_above_usd": ask, "needs_confirmation": bool(live and hi > ask),
            "text": ("Mock providers: nothing is charged." if not live else f"About ${lo:,.0f} to ${hi:,.0f} for {what}"
                     + (f" ({arms} runs: the current versions and the candidate)" if arms > 1 else "") + ".")}


# ======================================================================================================================
# plan stage: the job
# ======================================================================================================================
class BeginParams(Strict):
    run_id: str


class BriefParams(Strict):
    run_id: str
    arm: str
    index: int
    est_usd: float = 0.0


class FinishParams(Strict):
    run_id: str


def _mode(rt: Runtime) -> str:
    return "real" if _live_providers(rt, "plan") else "mock"


def brief_key(run_id: str, index: int) -> str:
    return f"{BRIEF_PREFIX}{run_id}:{index}"


def _pins_for(rt: Runtime, models: Mapping[str, str]) -> Any:
    from duoskin.pipeline import plan as PL

    pins = PL.make_pins(rt)
    return pins.model_copy(update={"models": {**pins.models, **models}}) if models else pins


def hidden_project(rt: Runtime, run: Mapping[str, Any], brief: RegressionBrief) -> Project:
    """The archived ``[regression] <id>`` project of one brief. With ``regression_reuse_plan_cache`` (the default) its id is the same in every run,
    so an unchanged plan loop is served from the content cache (the planner's kit order is seeded by the project id); without it every run gets
    a project of its own."""
    reuse = bool(rt.effective_settings().regression_reuse_plan_cache)
    pid = f"prj_regr_{brief.id}" if reuse else new_id("prj")
    now = utcnow()
    caps = ProjectSettings(budget_usd=BRIEF_CAP_USD, ask_above_usd=BRIEF_CAP_USD)
    pins = _pins_for(rt, (run.get("candidate_versions") or {}).get("models", {}) if run.get("role") == "candidate" else {})
    existing = rt.repo.find_project(pid)
    if existing is None:
        name = f"{cal.REGRESSION_PREFIX}{brief.id}"
        proj = Project(id=pid, name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo=brief.combo, brief=brief.brief,
                       structure_request=brief.structure_request, must_include=list(brief.must_include), stage=Stage.BRIEF, archived=True,
                       settings=caps, pins=pins)
        return rt.repo.create_project(proj)

    def reset(p: Project) -> None:
        p.combo, p.brief, p.structure_request, p.must_include = brief.combo, brief.brief, brief.structure_request, list(brief.must_include)
        p.stage, p.archived, p.paused, p.pins, p.settings = Stage.BRIEF, True, False, pins, caps
        p.plan_job_id = p.approved_spec_id = p.current_spec_id = None

    return rt.repo.mutate_project(pid, reset, bump=False)


def _planner_set(rt: Runtime, job_id: str) -> str:
    step = next((s for s in rt.repo.list_steps(job_id=job_id) if s.kind == "plan.planner"), None)
    return str(step.params.get("plan_set_id", "")) if step else ""


def _measure(rt: Runtime, project_id: str, job_id: str, gate: Any, started_at: str) -> dict[str, Any]:
    """What a finished Gate 1 says: the shown plans (rank order) with their critic levels and lint summary, and the concept sheet of each."""
    from duoskin.pipeline import plan as PL

    psid = _planner_set(rt, job_id)
    recs = {r.id: r for r in rt.repo.list_specs(project_id) if r.plan_set_id == psid}
    plans: list[dict[str, Any]] = []
    for t in sorted(gate.tiles, key=lambda t: (t.facts.get("rank") is None, t.facts.get("rank", 0))):
        rec = recs.get(str(t.facts.get("spec_id", "")))
        if rec is None:
            continue
        plans.append({"spec_id": rec.id, "rank": t.facts.get("rank"), "wildcard": bool(rec.spec.get("is_wildcard")), "spec": rec.spec,
                      "critic_levels": dict(rec.critic_levels), "lint": lint_summary(rec.lint), "sheet": t.assets.get("sheet"),
                      "failed": bool(t.facts.get("failed")) or bool(t.facts.get("hard_failures"))})
    env = PL.get_envelope(rt, psid) if psid else {}
    models = sorted({r["m"] for r in rt.db.conn().execute(
        "SELECT DISTINCT json_extract(json,'$.model') AS m FROM cost_ledger WHERE project_id=? AND ts >= ? AND provider IN ('anthropic','mock')",
        (project_id, started_at)).fetchall() if r["m"]})
    spent = rt.db.conn().execute("SELECT COALESCE(SUM(usd),0) AS u FROM cost_ledger WHERE project_id=? AND ts >= ? AND state IN ('committed','orphan')",
                                 (project_id, started_at)).fetchone()["u"]
    return {"plans": plans, "plan_set_id": psid, "envelope": {"brief_constraints": env.get("brief_constraints", []),
                                                               "how_they_differ": env.get("how_they_differ", "")},
            "served_models": models, "cost_usd": float(spent), "notice": str(env.get("notice", ""))}


def _retire(rt: Runtime, project_id: str, job_id: str) -> None:
    """After the measurement: no gate, no job and no taste evidence is left behind, and every asset is tagged ``regression``."""
    for g in rt.repo.list_gates(project_id, "open"):
        with contextlib.suppress(Exception):
            rt.gates.close_gate(g.id, "superseded")
    with contextlib.suppress(Exception):
        rt.scheduler.cancel_job(job_id)
    with rt.db.tx() as c:
        c.execute("UPDATE asset_links SET stream='regression', json=json_set(json,'$.provenance.stream','regression') WHERE project_id=?", (project_id,))
        for row in c.execute("SELECT id, json FROM specs WHERE project_id=?", (project_id,)).fetchall():
            doc = json.loads(row["json"])
            if doc.get("rank") is not None:                # a regression plan is never "shown to the person and not chosen" (taste evidence)
                doc["rank"] = None
                c.execute("UPDATE specs SET json=? WHERE id=?", (json.dumps(doc, ensure_ascii=False), row["id"]))
    rt.repo.mutate_project(project_id, lambda p: setattr(p, "archived", True), bump=False)


def run_begin(ctx: StepContext, p: BeginParams, inputs: list[Any]) -> StepResult:
    """Start of an arm: a candidate arm that names model snapshots switches them on (in memory only) for the whole app until ``regression.finish``."""
    rt = ctx.rt
    run = load_run(rt, p.run_id)
    if run is None:
        raise StepFailure(f"unknown regression run {p.run_id}", kind="bad_request", billed="no")
    models = (run.get("candidate_versions") or {}).get("models", {}) if run.get("role") == "candidate" else {}
    if models:
        rt.set_model_override(models)
    run.update({"state": "running", "started_at": iso_utc(utcnow())})
    save_run(rt, run)
    return StepResult(result={"run_id": p.run_id, "models": models}, message="regression started")


def run_brief(ctx: StepContext, p: BriefParams, inputs: list[Any]) -> StepResult | Pending:
    """One brief: make (or refresh) its hidden project, start a PLAN job on it and poll until Gate 1 is open."""
    rt = ctx.rt
    run = load_run(rt, p.run_id) or {}
    brief = RegressionBrief.model_validate(run["briefs"][p.index])
    key = brief_key(p.run_id, p.index)
    state = rt.repo.kv_get(key) or {}
    if state.get("done"):
        return StepResult(result={"index": p.index, "brief": brief.id, "ok": bool(state.get("ok"))}, message="measured")
    if not state.get("job_id"):
        project = hidden_project(rt, run, brief)
        job = rt.scheduler.submit_job(JobKind.PLAN, project.id, {"regression": p.run_id, "brief": brief.id})
        rt.repo.mutate_project(project.id, lambda pr: setattr(pr, "plan_job_id", job.id), bump=False)
        state = {"project_id": project.id, "job_id": job.id, "started_at": iso_utc(utcnow()), "brief": brief.id}
        rt.repo.kv_set(key, state)
    ctx.set_remote_ref(state["job_id"])
    return Pending(delay_s=POLL_S[run.get("mode", "mock")], message=f"planning {brief.id}", progress=0.1)


def poll_brief(ctx: StepContext, p: BriefParams, remote_ref: str) -> StepResult | Pending:
    from duoskin.models.gate import GateKind, TileState

    rt = ctx.rt
    run = load_run(rt, p.run_id) or {}
    key = brief_key(p.run_id, p.index)
    state = dict(rt.repo.kv_get(key) or {})
    if state.get("done"):
        return StepResult(result={"index": p.index, "ok": bool(state.get("ok"))}, message="measured")
    pid, job_id = state["project_id"], state["job_id"]
    gate = next((g for g in rt.repo.list_gates(pid, "open") if g.kind == GateKind.CONCEPT and g.job_id == job_id), None)
    job = rt.repo.find_job(job_id)
    waited = time.time() - parse_iso(state["started_at"]).timestamp()
    limit = BRIEF_TIMEOUT_S[run.get("mode", "mock")]
    if gate is not None and not any(t.state == TileState.GENERATING for t in gate.tiles):
        result = {"ok": True, **_measure(rt, pid, job_id, gate, state["started_at"])}
        result["ok"] = bool(result["plans"])
        if not result["ok"]:
            result["error"] = result.get("notice") or "Gate 1 showed no plans"
    elif job is None or job.state in (JobState.FAILED, JobState.CANCELLED) or waited > limit:
        reason = "the plan job failed" if job is not None and job.state == JobState.FAILED else \
            "the plan job was cancelled" if job is not None and job.state == JobState.CANCELLED else f"no Gate 1 after {waited:.0f} s"
        result = {"ok": False, "error": reason, "plans": [], "served_models": [], "cost_usd": 0.0}
    else:
        ctx.progress(min(0.9, 0.1 + waited / max(limit, 1.0)), f"planning {state.get('brief', '')}")
        return Pending(delay_s=POLL_S[run.get("mode", "mock")], message=f"planning {state.get('brief', '')}")
    _retire(rt, pid, job_id)
    rt.repo.kv_set(key, {**state, "done": True, **result, "index": p.index})
    return StepResult(result={"index": p.index, "brief": state.get("brief"), "ok": result["ok"]}, message="measured" if result["ok"] else f"failed: {result.get('error')}")


def collect_images(rt: Runtime, results: Sequence[Mapping[str, Any]]) -> list[Any]:
    from duoskin.pipeline import common

    out = []
    for r in results:
        sheet = ((r.get("plans") or [{}])[0]).get("sheet")
        if r.get("ok") and sheet:
            try:
                out.append(common.open_image(rt.cas.get(sheet)))
            except Exception:
                log.exception("concept sheet %s could not be read", str(sheet)[:12])
    return out


def memory_sha(rt: Runtime) -> str:
    """What the planner remembers of the person's approved duos (the hints of ``<recently_used>`` and ``<recent_cards>``): runs are only
    exactly comparable while it is unchanged."""
    from duoskin.pipeline import brief as BR

    try:
        return sha256_of({"used": BR.recently_used(rt), "cards": [c.model_dump(mode="json") for c in BR.recent_cards(rt)]})[:16]
    except Exception:    # noqa: BLE001
        return ""


def build_plan_run(rt: Runtime, run: dict[str, Any]) -> dict[str, Any]:
    """Turn the per-brief records of a finished arm into the run's numbers: variety metrics, quality, cost, the models that were served."""
    results = [dict(rt.repo.kv_get(brief_key(run["id"], i)) or {"ok": False, "error": "not run", "plans": []}) for i in range(len(run["briefs"]))]
    ok = [r for r in results if r.get("ok")]
    top_specs = [r["plans"][0]["spec"] for r in ok]
    model = cal._dreamsim_model(rt)
    images = collect_images(rt, ok)
    dist, mode = image_distance(images, model) if len(images) >= 2 else (0.0, "dreamsim" if model is not None else "degraded")
    metrics = spec_variety(top_specs)
    metrics["image_distance"] = dist
    metrics["images"] = len(images)
    quality = float(np.mean([brief_quality(r) for r in results])) if results else 0.0
    served = sorted({m for r in results for m in r.get("served_models", [])})
    run.update({"state": "done", "finished_at": iso_utc(utcnow()), "image_mode": mode, "metrics": metrics, "quality": quality,
                "ok_briefs": len(ok), "failed_briefs": [{"id": run["briefs"][i]["id"], "error": r.get("error", "")} for i, r in enumerate(results) if not r.get("ok")],
                "cost_usd": float(sum(r.get("cost_usd", 0.0) for r in results)), "served_models": served, "memory_sha": memory_sha(rt),
                "per_brief": [{"id": run["briefs"][i]["id"], "ok": bool(r.get("ok")), "quality": brief_quality(r),
                               "structure": ((r.get("plans") or [{}])[0].get("spec") or {}).get("world", {}).get("pair_structure"),
                               "error": r.get("error", "")} for i, r in enumerate(results)]})
    return run


def run_finish(ctx: StepContext, p: FinishParams, inputs: list[Any]) -> StepResult:
    """End of an arm: compute the run, switch a candidate model override off, set the baseline pointer or run the guard."""
    rt = ctx.rt
    run = load_run(rt, p.run_id)
    if run is None:
        raise StepFailure(f"unknown regression run {p.run_id}", kind="bad_request", billed="no")
    try:
        if run["stage"] == "plan":
            run = build_plan_run(rt, run)
        else:
            run = build_parts_run(rt, run)
    finally:
        if run.get("role") == "candidate" and (run.get("candidate_versions") or {}).get("models"):
            rt.set_model_override(None)
    guard = None
    if run["role"] == "baseline":
        rt.repo.kv_set(BASELINE_PREFIX + run["stage"], run["id"])
    else:
        guard = variety_guard(baseline_of(rt, run["stage"]) if not run.get("baseline_id") else load_run(rt, run["baseline_id"]), run).as_dict()
    run["guard"] = guard
    save_run(rt, run)
    rt.bus.emit("toast", {"message": "The regression test finished." if guard is None else
                          ("The new version passed the variety guard." if guard["accepted"] else "The new version did not pass the variety guard."),
                          "level": "ok" if guard is None or guard["accepted"] else "warn"}, None)
    return StepResult(result={"run_id": run["id"], "quality": run["quality"], "guard": (guard or {}).get("decision")}, message="regression finished")


# ======================================================================================================================
# parts stage
# ======================================================================================================================
class PartParams(Strict):
    run_id: str
    index: int
    est_usd: float = 0.0


def parts_set_dir(rt: Runtime) -> Path:
    return rt.paths.regression_dir / "parts_set"


def freeze_parts_set(rt: Runtime, *, limit: int = 20) -> list[dict[str, Any]]:
    """Freeze the specs of the newest approved duos as the parts set (10 to 20 of them, each with the plan outputs it was approved with). The set
    is a file per spec, so it never changes under a running test; a set that already exists is kept (delete the folder to refresh it)."""
    from duoskin.pipeline import brief as BR

    folder = parts_set_dir(rt)
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "index.json"
    if index.exists():
        return list(json.loads(index.read_text(encoding="utf-8")).get("specs", []))
    rows: list[dict[str, Any]] = []
    for item in BR.approved_specs(rt, limit=limit):
        sid = f"ps{len(rows) + 1:02d}"
        spec = item["spec"]
        (folder / f"{sid}.json").write_text(json.dumps({"id": sid, "source_spec": item["spec_id"], "spec": spec}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        rows.append({"id": sid, "combo": spec.get("combo"), "sha256": sha256_of(spec)})
    index.write_text(json.dumps({"version": 1, "specs": rows}, indent=1) + "\n", encoding="utf-8")
    return rows


def load_parts_set(rt: Runtime) -> list[dict[str, Any]]:
    folder = parts_set_dir(rt)
    if not (folder / "index.json").exists():
        return []
    return [json.loads((folder / f"{r['id']}.json").read_text(encoding="utf-8")) for r in json.loads((folder / "index.json").read_text(encoding="utf-8"))["specs"]]


def kinds_for(templates: Iterable[str]) -> list[str]:
    """Part kinds for ``--template`` values: a bible template id (``I2``) or a kind name (``print``)."""
    out: list[str] = []
    for t in templates:
        k = TEMPLATE_KINDS.get(str(t).upper()) or (str(t).lower() if str(t).lower() in PART_KINDS else None)
        if k is None:
            raise RegressionError(f"unknown part template '{t}': use one of {', '.join(sorted(TEMPLATE_KINDS))} or a part kind ({', '.join(PART_KINDS)})", "unknown_template")
        if k not in out:
            out.append(k)
    return out


def run_part(ctx: StepContext, p: PartParams, inputs: list[Any]) -> StepResult | Pending:
    """One frozen spec: an approved hidden project, the affected parts only, until their tiles have settled on the part board."""
    from duoskin.pipeline import parts

    rt = ctx.rt
    run = load_run(rt, p.run_id) or {}
    key = brief_key(p.run_id, p.index)
    state = rt.repo.kv_get(key) or {}
    if state.get("done"):
        return StepResult(result={"index": p.index, "ok": bool(state.get("ok"))}, message="measured")
    if not state.get("job_id"):
        entry = run["specs"][p.index]
        from duoskin.models.spec_record import SpecRecord

        now = utcnow()
        pid = new_id("prj")
        name = f"{cal.REGRESSION_PREFIX}{entry['id']} {new_id('r')[-6:]}"
        rt.repo.create_project(Project(id=pid, name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo=entry["spec"]["combo"],
                                                 brief="", stage=Stage.PARTS, archived=True,
                                                 settings=ProjectSettings(budget_usd=BRIEF_CAP_USD, ask_above_usd=BRIEF_CAP_USD, mesh_mode="api"),
                                                 pins=_pins_for(rt, {})))
        rec = SpecRecord(id=new_id("spc"), project_id=pid, plan_set_id="pls_regression", plan_index=0, spec=entry["spec"], status="approved",
                         palette_source="concept_extracted", sha256=sha256_of(entry["spec"]))      # type: ignore[arg-type]
        rt.repo.add_spec(rec)
        rt.repo.mutate_project(pid, lambda pr: (setattr(pr, "approved_spec_id", rec.id), setattr(pr, "current_spec_id", rec.id)), bump=False)
        planned = parts.ensure_parts(rt, pid, entry["spec"])
        only = [x.id for x in planned if x.kind.value in run["template_kinds"]]
        job = parts.start_parts_job(rt, pid, only=only or None, reason="regression")
        state = {"project_id": pid, "job_id": job.id, "only": only, "started_at": iso_utc(utcnow()), "spec": entry["id"]}
        rt.repo.kv_set(key, state)
    ctx.set_remote_ref(state["job_id"])
    return Pending(delay_s=POLL_S[run.get("mode", "mock")], message=f"making parts for {state['spec']}", progress=0.1)


def poll_part(ctx: StepContext, p: PartParams, remote_ref: str) -> StepResult | Pending:
    """Wait until the affected parts have settled (ready, needs a person, or failed), then take what they made. The part board gate is not used:
    a PARTS job on a few parts does not open it, and nothing here is ever approved."""
    from duoskin.models.part import PartState

    rt = ctx.rt
    run = load_run(rt, p.run_id) or {}
    key = brief_key(p.run_id, p.index)
    state = dict(rt.repo.kv_get(key) or {})
    if state.get("done"):
        return StepResult(result={"index": p.index, "ok": bool(state.get("ok"))}, message="measured")
    pid, job_id, only = state["project_id"], state["job_id"], set(state.get("only") or [])
    parts_now = [x for x in rt.repo.list_parts(pid) if not only or x.id in only]
    job = rt.repo.find_job(job_id)
    waited = time.time() - parse_iso(state["started_at"]).timestamp()
    limit = BRIEF_TIMEOUT_S[run.get("mode", "mock")]
    settled = (PartState.READY, PartState.NEEDS_HUMAN, PartState.FAILED, PartState.WAITING_MANUAL, PartState.APPROVED)
    job_over = job is None or job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
    if parts_now and all(x.state in settled for x in parts_now) and job_over:
        checks = [r for _, r in rt.repo.list_checks(project_id=pid)]
        outputs, passed = [], 0
        for part in parts_now:
            hard = part.state in (PartState.FAILED, PartState.NEEDS_HUMAN)
            passed += 0 if hard else 1
            sha = next((part.board_assets[r] for r in ("final", "neutral", "front", "flat", "canvas") if r in part.board_assets),
                       next(iter(part.board_assets.values()), None))
            ran = [r for r in checks if r.ran and r.subject_sha and r.subject_sha in part.board_assets.values()]
            outputs.append({"part_id": part.id, "sha": sha, "hard": hard, "lint": {"total": len(ran), "passed": sum(1 for r in ran if r.passed)}})
        spent = rt.db.conn().execute("SELECT COALESCE(SUM(usd),0) AS u FROM cost_ledger WHERE project_id=? AND state IN ('committed','orphan')", (pid,)).fetchone()["u"]
        result = {"ok": passed > 0, "outputs": outputs, "passed": passed, "total": len(parts_now), "cost_usd": float(spent), "served_models": []}
    elif (job is None or job.state in (JobState.FAILED, JobState.CANCELLED)) or waited > limit:
        result = {"ok": False, "error": "the parts job did not finish", "outputs": [], "passed": 0, "total": len(only), "cost_usd": 0.0, "served_models": []}
    else:
        return Pending(delay_s=POLL_S[run.get("mode", "mock")], message=f"making parts for {state['spec']}")
    for g in rt.repo.list_gates(pid, "open"):
        with contextlib.suppress(Exception):
            rt.gates.close_gate(g.id, "superseded")
    if job is not None and job.state not in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        with contextlib.suppress(Exception):
            rt.scheduler.cancel_job(job_id)
    with rt.db.tx() as c:
        c.execute("UPDATE asset_links SET stream='regression', json=json_set(json,'$.provenance.stream','regression') WHERE project_id=?", (pid,))
    rt.repo.kv_set(key, {**state, "done": True, **result, "index": p.index})
    return StepResult(result={"index": p.index, "ok": result["ok"]}, message="measured")


def build_parts_run(rt: Runtime, run: dict[str, Any]) -> dict[str, Any]:
    """The numbers of a finished parts-stage arm: pass rate (quality), the pairwise distance between the template outputs (variety) and cost."""
    from duoskin.pipeline import common

    results = [dict(rt.repo.kv_get(brief_key(run["id"], i)) or {"ok": False, "outputs": []}) for i in range(len(run["specs"]))]
    outputs = [o for r in results for o in r.get("outputs", [])]
    total = sum(r.get("total", 0) for r in results)
    passed = sum(r.get("passed", 0) for r in results)
    images = []
    for o in outputs:
        if o.get("sha"):
            with contextlib.suppress(Exception):
                images.append(common.open_image(rt.cas.get(o["sha"])))
    model = cal._dreamsim_model(rt)
    dist, mode = image_distance(images, model) if len(images) >= 2 else (0.0, "dreamsim" if model is not None else "degraded")
    chk = [o["lint"] for o in outputs]
    check_share = (sum(c["passed"] for c in chk) / sum(c["total"] for c in chk)) if sum(c["total"] for c in chk) else None
    pass_rate = passed / total if total else 0.0
    metrics = {"image_distance": dist, "images": len(images), "variety_index": dist, "pass_rate": pass_rate, "parts": total, "check_pass_share": check_share}
    run.update({"state": "done", "finished_at": iso_utc(utcnow()), "image_mode": mode if len(images) >= 2 else "none", "metrics": metrics,
                "quality": float(np.mean([x for x in (pass_rate, check_share) if x is not None])), "ok_briefs": sum(1 for r in results if r.get("ok")),
                "failed_briefs": [{"id": run["specs"][i]["id"], "error": r.get("error", "")} for i, r in enumerate(results) if not r.get("ok")],
                "cost_usd": float(sum(r.get("cost_usd", 0.0) for r in results)), "served_models": [], "memory_sha": ""})
    return run


# ======================================================================================================================
# starting a run, candidate thresholds, promotion
# ======================================================================================================================
ACTIVE_STATES = (JobState.RUNNING, JobState.WAITING_USER)
BUSY_KINDS = (JobKind.PLAN, JobKind.PARTS, JobKind.BUILD, JobKind.DUO, JobKind.EXPORT, JobKind.MANUAL_MESH)


def other_jobs_running(rt: Runtime) -> list[Job]:
    """Jobs of the person's own work that a candidate model override would leak into (regression projects' jobs do not count)."""
    out = []
    for j in rt.repo.list_jobs(limit=500):
        if j.kind not in BUSY_KINDS or j.state not in ACTIVE_STATES:
            continue
        proj = rt.repo.find_project(j.project_id) if j.project_id else None
        if proj is not None and cal.is_regression_name(proj.name):
            continue
        out.append(j)
    return out


def sweep_override(rt: Runtime) -> bool:
    """Switch a candidate model override off when no regression job is active any more (a cancelled job never reaches ``regression.finish``)."""
    if not rt.model_override:
        return False
    if any(j.kind == JobKind.REGRESSION and j.state in ACTIVE_STATES for j in rt.repo.list_jobs(limit=50)):
        return False
    rt.set_model_override(None)
    return True


def _arm_steps(rt: Runtime, job: Job, run: dict[str, Any], *, after: str | None, per_item_usd: float) -> list[Step]:
    """begin -> items (at most ``CONCURRENCY`` at a time) -> finish, as steps of the job."""
    items = run["briefs"] if run["stage"] == "plan" else run["specs"]
    kind = "regression.brief" if run["stage"] == "plan" else "regression.part"
    begin = rt.ops.new_step("regression.begin", job_id=job.id, params=BeginParams(run_id=run["id"]).model_dump(), deps=[after] if after else [])
    steps = [begin]
    item_steps: list[Step] = []
    for i in range(len(items)):
        deps = [begin.id] + ([item_steps[i - CONCURRENCY].id] if i >= CONCURRENCY else [])
        params = (BriefParams(run_id=run["id"], arm=run["role"], index=i, est_usd=per_item_usd) if run["stage"] == "plan"
                  else PartParams(run_id=run["id"], index=i, est_usd=per_item_usd))
        item_steps.append(rt.ops.new_step(kind, job_id=job.id, params=params.model_dump(), deps=deps, priority=60))
    finish = rt.ops.new_step("regression.finish", job_id=job.id, params=FinishParams(run_id=run["id"]).model_dump(), deps=[s.id for s in item_steps] or [begin.id])
    return [*steps, *item_steps, finish]


def need_baseline_for(existing: Mapping[str, Any], probe: Mapping[str, Any]) -> bool:
    """Is the stored baseline on other inputs than the run about to start? (Then a candidate has nothing to be compared with.)"""
    return (any(existing.get(k) != probe[k] for k in ("stage", "brief_set_sha", "n_briefs"))
            or sorted(existing.get("template_kinds") or []) != sorted(probe["template_kinds"]))


@dataclass
class Prepared:
    """What a regression job would do, worked out before anything is created (the estimate, the arms, the inputs)."""

    stage: str
    cand: dict[str, str]
    roles: list[str]
    base_run: dict[str, Any]
    n_items: int
    estimate: dict[str, Any]
    mode: str
    label: str
    sample: int | None
    compare: bool

    def preview(self) -> dict[str, Any]:
        return {"stage": self.stage, "arms": self.roles, "items": self.n_items, "candidate_versions": self.cand, "estimate": self.estimate, "mode": self.mode,
                "template_kinds": self.base_run["template_kinds"], "brief_set_edited": self.base_run.get("brief_set_edited", False)}


def prepare_regression(rt: Runtime, *, stage: str = "plan", sample: int | None = None, templates: Sequence[str] = (),
                       candidate_versions: Mapping[str, str] | None = None, label: str = "", compare: bool = False) -> Prepared:
    """Validate the request and quote it (no job, no run record, no project is created). Raises ``RegressionError``."""
    if stage not in STAGES:
        raise RegressionError(f"stage must be one of {', '.join(STAGES)}", "bad_stage")
    cand = {k: str(v) for k, v in (candidate_versions or {}).items() if v}
    bad = [k for k in cand if k not in MODEL_ROLES]
    if bad:
        raise RegressionError(f"not a model role: {', '.join(bad)}", "bad_role")
    if cand and other_jobs_running(rt):
        raise RegressionError("Finish or pause your other duos first: a candidate model is switched on for the whole app while the test runs.", "busy")
    sweep_override(rt)
    if any(j.kind == JobKind.REGRESSION and j.state in ACTIVE_STATES for j in rt.repo.list_jobs(limit=50)):
        raise RegressionError("A regression test is already running.", "busy")
    mode = _mode(rt)
    if stage == "plan":
        bset = ensure_briefs(rt)
        chosen = sample_briefs(bset.briefs, sample)
        base_run = {"briefs": [b.model_dump(mode="json") for b in chosen], "brief_set_sha": bset.sha256, "brief_set_edited": bset.edited,
                    "n_briefs": len(chosen), "template_kinds": []}
        n_items = len(chosen)
    else:
        kinds = kinds_for(templates or ["print"])
        freeze_parts_set(rt)
        specs = load_parts_set(rt)
        floor = int(TH.get("calib.drill_min_duos")) * 2
        if len(specs) < floor:
            raise RegressionError(f"The parts test needs {floor} to 20 approved duos to freeze; there are {len(specs)}.", "not_enough_specs")
        n = min(len(specs), int(sample)) if sample else len(specs)
        specs = specs[:n]
        base_run = {"specs": specs, "brief_set_sha": sha256_of([s["spec"] for s in specs]), "n_briefs": len(specs), "template_kinds": kinds}
        n_items = len(specs)
    existing = baseline_of(rt, stage)
    probe = {"stage": stage, "brief_set_sha": base_run["brief_set_sha"], "n_briefs": base_run["n_briefs"], "template_kinds": base_run["template_kinds"]}
    need_baseline = bool(cand) and (existing is None or need_baseline_for(existing, probe))
    if compare and not cand:
        if existing is None or need_baseline_for(existing, probe):
            raise RegressionError("There is no baseline to compare with: run the regression test once BEFORE you change anything.", "no_baseline")
        roles = ["candidate"]
    else:
        roles = ["baseline", "candidate"] if need_baseline else (["candidate"] if cand else ["baseline"])
    est = estimate(rt, stage, briefs=n_items, specs=n_items, templates=max(1, len(base_run["template_kinds"])), arms=len(roles))
    return Prepared(stage, cand, roles, base_run, n_items, est, mode, label, sample, compare)


def preview_regression(rt: Runtime, **kw: Any) -> dict[str, Any]:
    """``prepare_regression`` as plain data: what the CLI and the dialog show before anything is started."""
    return prepare_regression(rt, **kw).preview()


def start_regression(rt: Runtime, *, stage: str = "plan", sample: int | None = None, templates: Sequence[str] = (),
                     candidate_versions: Mapping[str, str] | None = None, label: str = "", compare: bool = False) -> Job:
    """Create the REGRESSION job (APP_SPEC §3.9, §13 ``POST /api/regression/run``).

    * ``stage="plan"``: the fixed briefs (or a deterministic ``sample`` of them) through the plan loop to Gate 1. ``stage="parts"``: the frozen
      parts set on ``templates`` only.
    * ``candidate_versions`` ``{role: snapshot}`` (model roles): a candidate arm, run with those snapshots switched on in memory. When no baseline
      exists for the same inputs, the job runs one first (the current versions) so the guard has something to compare with.
    * ``compare=True`` without candidate versions: a change that is already installed (a template, the house style, the kit manifest, a prompt
      version) is measured now and judged against the stored baseline. Run the baseline BEFORE the change.
    * The job carries its estimate; above ``budgets.regression_ask_usd`` the scheduler opens one BUDGET gate before anything is spent."""
    pre = prepare_regression(rt, stage=stage, sample=sample, templates=templates, candidate_versions=candidate_versions, label=label, compare=compare)
    per_item = (pre.estimate["usd"] / (pre.n_items * len(pre.roles))) if pre.n_items else 0.0
    runs = []
    for role in pre.roles:
        rid = new_id("reg")
        runs.append({"id": rid, "stage": stage, "role": role, "label": label or ("candidate" if role == "candidate" else "baseline"), "state": "queued",
                     "mode": pre.mode, "created_at": iso_utc(utcnow()),
                     "candidate_versions": ({"models": pre.cand} if pre.cand else {"note": label or "installed change"}) if role == "candidate" else {},
                     "versions": _pins_for(rt, pre.cand if role == "candidate" else {}).model_dump(mode="json"), "sample": sample, **pre.base_run})
    if len(runs) == 2:
        runs[1]["baseline_id"] = runs[0]["id"]
    job = rt.scheduler.submit_job(JobKind.REGRESSION, None, {"stage": stage, "run_ids": [r["id"] for r in runs], "estimate_usd": pre.estimate["usd"],
                                                             "estimate": pre.estimate, "sample": sample, "templates": list(templates)}, steps=[])
    for r in runs:
        r["job_id"] = job.id
        save_run(rt, r)
    steps: list[Step] = []
    prev_finish: str | None = None
    for r in runs:
        arm = _arm_steps(rt, job, r, after=prev_finish, per_item_usd=per_item)
        steps += arm
        prev_finish = arm[-1].id
    rt.scheduler.spawn(job.id, steps)
    return rt.repo.get_job(job.id)


def evaluate_threshold_candidate(rt: Runtime, values: Mapping[str, float]) -> GuardDecision:
    """The guard for a threshold set. A threshold changes what the plan linter accepts, not what the planner draws, so the candidate is the
    baseline plan run's own plans linted again with the candidate values (``thresholds.overrides``); no model is called and nothing is spent.
    Variety cannot fall (the same plans, the same pictures); quality falls when a tighter bound fails plans the baseline accepted."""
    from duoskin.pipeline import lint as LI

    base = baseline_of(rt, "plan")
    if base is None:
        return GuardDecision("needs_baseline", ["Run the regression test once first: a threshold set is judged on the plans of that run."])
    results = [dict(rt.repo.kv_get(brief_key(base["id"], i)) or {}) for i in range(len(base["briefs"]))]
    if not any(r.get("ok") for r in results):
        return GuardDecision("needs_baseline", ["The baseline run holds no plans to lint again."], baseline_id=base["id"])

    def requality(over: Mapping[str, float]) -> float:
        scores = []
        with TH.overrides(dict(over)):
            for brief, r in zip(base["briefs"], results, strict=True):
                if not r.get("ok"):
                    scores.append(0.0)
                    continue
                cands = [(p["spec_id"], p["spec"]) for p in r["plans"]]
                env = r.get("envelope") or {}
                from duoskin.models.spec import BriefConstraint

                cons = [BriefConstraint.model_validate(c) for c in env.get("brief_constraints", [])]
                ctx = LI.lint_context(rt, None, structure_request=brief.get("structure_request", "auto"), must_include=brief.get("must_include", []))
                bundle = LI.lint_candidates(cands, ctx, brief_constraints=cons, how_they_differ=env.get("how_they_differ", ""))
                per = []
                for p in r["plans"]:
                    ls = lint_summary(bundle.results(p["spec_id"]))
                    parts = [x for x in (critic_score(p.get("critic_levels") or {}), lint_score(ls)) if x is not None]
                    per.append(float(np.mean(parts)) if parts else 0.0)
                scores.append(float(np.mean(per)) if per else 0.0)
        return float(np.mean(scores)) if scores else 0.0

    b_run = {**{k: base.get(k) for k in ("id", "stage", "brief_set_sha", "n_briefs", "image_mode", "metrics", "template_kinds", "failed_briefs")},
             "quality": requality({})}
    c_run = {**b_run, "id": new_id("thr"), "quality": requality(values), "candidate_versions": {}}
    return variety_guard(b_run, c_run)


def adopt_run(rt: Runtime, run_id: str) -> dict[str, Any]:
    """A change that is already installed (a template, the house style, kits, prompts) passed the guard: its run becomes the baseline of the next
    comparison. Refused unless the run's own guard accepted it."""
    run = load_run(rt, run_id)
    if run is None:
        raise RegressionError("Unknown regression run.", "unknown_run")
    if run.get("role") != "candidate" or run.get("state") != "done":
        raise RegressionError("Only a finished candidate run can be adopted.", "bad_run")
    guard = run.get("guard") or {}
    if not guard.get("accepted"):
        raise RegressionError("The variety guard did not accept this run. " + " ".join(guard.get("reasons", [])), "guard")
    rt.repo.kv_set(BASELINE_PREFIX + run["stage"], run["id"])
    return {"run_id": run_id, "stage": run["stage"], "guard": guard}


def run_summary(run: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """A run for the Learning page: the numbers, the versions and the guard's answer (``detail`` adds the per-brief rows and the variety breakdown)."""
    m = run.get("metrics") or {}
    out = {k: run.get(k) for k in ("id", "stage", "role", "label", "state", "mode", "created_at", "finished_at", "n_briefs", "ok_briefs", "failed_briefs",
                                   "quality", "cost_usd", "candidate_versions", "brief_set_sha", "brief_set_edited", "image_mode", "template_kinds", "baseline_id",
                                   "served_models", "sample")}
    out.update({"image_distance": m.get("image_distance"), "variety_index": m.get("variety_index"), "guard": run.get("guard")})
    if detail:
        out.update({"metrics": m, "per_brief": run.get("per_brief", [])})
    return out


def versions_view(rt: Runtime) -> dict[str, Any]:
    """Defaults and candidates of the model roles, each candidate with the latest run that tested it and what the guard said."""
    models = rt.settings.models
    runs = [r for r in list_runs(rt, "plan") if r.get("role") == "candidate"]
    rows = []
    for role in MODEL_ROLES:
        cand = models.candidates.get(role)
        latest = next((r for r in runs if cand and (r.get("candidate_versions") or {}).get("models", {}).get(role) == cand), None)
        rows.append({"role": role, "default": getattr(models, role), "candidate": cand, "run": run_summary(latest) if latest else None,
                     "can_promote": bool(latest and (latest.get("guard") or {}).get("accepted") and latest.get("state") == "done")})
    base = baseline_of(rt, "plan")
    return {"roles": rows, "baseline": run_summary(base) if base else None}


def promote(rt: Runtime, role: str, version: str) -> dict[str, Any]:
    """``POST /api/versions/promote``: make a candidate snapshot the default. Refused unless the latest plan-stage run that tested exactly this
    candidate passed the variety guard (APP_SPEC §3.9, §13)."""
    if role not in MODEL_ROLES:
        raise RegressionError(f"not a model role: {role}", "bad_role")
    cands = rt.settings.models.candidates
    if cands.get(role) != version:
        raise RegressionError(f"{version} is not a candidate for {role}: add it in Settings first.", "not_a_candidate")
    run = next((r for r in list_runs(rt, "plan") if r.get("role") == "candidate" and (r.get("candidate_versions") or {}).get("models", {}).get(role) == version
                and r.get("state") == "done"), None)
    if run is None:
        raise RegressionError("No regression test has been run with this version yet.", "no_regression")
    guard = run.get("guard") or {}
    if not guard.get("accepted"):
        why = " ".join(guard.get("reasons", [])) or "The guard has not accepted it."
        raise RegressionError(f"The latest regression test did not pass the variety guard. {why}", "guard")
    rt.update_settings({"models": {role: version, "candidates": {role: None}}})
    rt.repo.kv_set(BASELINE_PREFIX + "plan", run["id"])
    return {"role": role, "version": version, "run_id": run["id"], "guard": guard}


# ======================================================================================================================
# registration
# ======================================================================================================================
def register_handlers() -> None:
    """The regression steps. They poll hidden jobs, so they hold no worker while they wait (``Pending``)."""
    eng.register_handler("regression.begin", run_begin, version=1, pool="cpu", paid=False, Params=BeginParams, cacheable=False)
    eng.register_handler("regression.brief", run_brief, version=1, pool="cpu", paid=True, provider="anthropic", Params=BriefParams, cacheable=False,
                         estimate=lambda p: p.est_usd, poll=poll_brief)
    eng.register_handler("regression.part", run_part, version=1, pool="cpu", paid=True, provider="openai", Params=PartParams, cacheable=False,
                         estimate=lambda p: p.est_usd, poll=poll_part)
    eng.register_handler("regression.finish", run_finish, version=1, pool="cpu", paid=False, Params=FinishParams, cacheable=False)


def register(rt: Runtime | None = None) -> None:
    register_handlers()
    sched.register_job_factory(JobKind.REGRESSION, lambda _rt, _job, _project: [])      # steps are made by ``start_regression``
    if rt is not None:
        try:
            cal.load_calibrated(rt)                      # accepted threshold values are active from the first call on
        except Exception:
            log.exception("calibrated thresholds could not be loaded")
        with contextlib.suppress(Exception):
            sweep_override(rt)

