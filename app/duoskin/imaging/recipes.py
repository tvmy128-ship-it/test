"""Garment recipes: the JSON schema, loader, variable/expression evaluation and shape rasteriser (bible §6.2).

A recipe (``builtin_kits/recipes/<recipe_id>.json``) is **data**, not code: a list of paint layers that the compositor
(``imaging/compositor.py``) executes in the fixed stage order (CLO-15). All coordinates are template pixels (the 585x559
sheet, inclusive pixel boxes) so a recipe reads like bible §6.2 ("torso rows 74-201", "hem band 399-404").

Recipe file shape::

    {"recipe_id": "tee", "template": "shirt", "family": "tee", "title": "...", "version": 1,
     "extends": "<other recipe id>",                      # optional: start from another recipe, override keys
     "fold_set": "tee_soft",                              # fold library set (procedural fallback if missing)
     "cut": {"sleeve": ["short", ...], ...},              # allowed values of each cut attribute (garment cut lint)
     "defaults": {"sleeve": "short", ...},                # attribute values used when the spec gives none
     "params": {"crop_hem": 152},                         # numeric knobs ($crop_hem in expressions)
     "vars": {"sleeve_end": {"attr": "sleeve", "map": {"short": 404, "long": 466}}, "x": "$sleeve_end - 6"},
     "layers": [ {"id": ..., "stage": "blocks" | "details", "op": "paint" | "shade", ...}, ... ],
     "print_slots": [ {"region": "torso_f", "role": "hero", "box": [x0, y0, x1, y1]}, ... ],
     "requires_bottom": {"waist": ["high"]}}              # optional plan-level requirement (crop tops)

Layer fields: ``id``; ``stage``; ``op`` = ``paint`` (``coverage``: ``add`` sets colour and coverage, ``within`` recolours only
covered pixels, ``under`` paints only where nothing covers yet (legwear under pants), ``erase`` makes pixels transparent skin) or ``shade`` (lightness shift ``dL`` on covered pixels, optional
``ramp``); ``colour`` (role name, ``#rrggbb`` or ``{"role": .., "dL": ..}``); ``label`` (1 fabric, 2 secondary block, 4 trim, ...);
``shapes`` / ``minus`` (see below); ``regions`` (fnmatch globs); ``frame`` (``abs`` | ``local_x`` | ``local`` | ``strip``: x is the perimeter coordinate of the
part's side-face strip, so a stitch or rib pattern keeps its phase across the seams); ``when`` /
``unless`` (cut attribute conditions); ``fabric`` (apply the fabric tile, default true).

Shapes (union of): ``{"rect": [x0, y0, x1, y1]}`` inclusive pixel box, ``{"ellipse": [cx, cy, rx, ry]}``, ``{"poly": [[x, y], ..]}``,
``{"arc": [cx, cy, rx, ry, thickness, a0, a1]}`` (ring sector, degrees, 0 = +x, clockwise on the sheet), ``{"line": [[x, y], ..],
"w": 1, "dash": [on, off]}`` (points are pixel indices: the stroke runs through pixel centres), ``{"stripes": {"axis": "x"|"y", "from": a, "to": b, "step": s, "w": w, "span": [c0, c1]}}``,
``{"ref": "<layer id>"}`` (the mask of an earlier layer). Numbers may be expressions: ``"$sleeve_end - 6"``.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from pydantic import ConfigDict, field_validator, model_validator

from duoskin.models.common import Strict, sha256_of
from duoskin.roblox import template as T

RECIPE_DIR = Path(__file__).resolve().parent.parent / "builtin_kits" / "recipes"
STAGES = ("blocks", "details", "kit")        # "kit" is for painted kit pieces (shoes, legwear, bracelets) only
ASSERT_RULES = ("", "band", "shoe_top")
OPS = ("paint", "shade")
COVERAGE = ("add", "within", "under", "erase")
FRAMES = ("abs", "local_x", "local", "strip")
SHAPE_KEYS = ("rect", "ellipse", "poly", "arc", "line", "stripes", "ref")
SCALE = 4
LABEL_NAMES = {0: "skin", 1: "fabric", 2: "secondary", 3: "print", 4: "trim", 5: "bracelet_glove", 6: "shoes", 7: "legwear"}


class RecipeError(ValueError):
    """A recipe file is malformed or an expression/shape cannot be evaluated."""


# --------------------------------------------------------------------------------------------------------------------
# safe numeric expressions
# --------------------------------------------------------------------------------------------------------------------
_FUNCS = {"min": min, "max": max, "round": round, "abs": abs}


@lru_cache(maxsize=512)
def _parse(expr: str) -> ast.Expression:
    src = re.sub(r"\$([A-Za-z_]\w*)", r"v_\1", expr.strip())
    return ast.parse(src, mode="eval")


def _ev(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id.startswith("v_") and node.id[2:] in env:
            return float(env[node.id[2:]])
        raise RecipeError(f"unknown variable ${node.id[2:]}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _ev(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, env), _ev(node.right, env)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            return a / b
        if isinstance(node.op, ast.FloorDiv):
            return float(a // b)
        if isinstance(node.op, ast.Mod):
            return a % b
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return float(_FUNCS[node.func.id](*[_ev(a, env) for a in node.args]))
    raise RecipeError(f"unsupported expression element {type(node).__name__}")


def eval_expr(value: Any, env: dict[str, float]) -> float:
    """Evaluate a number or an expression string such as ``"$sleeve_end - 6"`` against ``env``."""
    if isinstance(value, bool):
        raise RecipeError("boolean where a number was expected")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return _ev(_parse(value).body, env)
        except (SyntaxError, ZeroDivisionError) as e:
            raise RecipeError(f"bad expression {value!r}: {e}") from e
    raise RecipeError(f"expected a number or expression, got {value!r}")


def _num_tree(obj: Any, env: dict[str, float]) -> Any:
    """Evaluate every expression string inside a shape description (lists, dicts) to plain numbers."""
    if isinstance(obj, list):
        return [_num_tree(o, env) for o in obj]
    if isinstance(obj, dict):
        return {k: (_num_tree(v, env) if k not in ("axis", "ref", "profile") else v) for k, v in obj.items()}
    if isinstance(obj, str):
        return eval_expr(obj, env)
    return obj


# --------------------------------------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------------------------------------
class PrintSlot(Strict):
    region: str
    role: str                      # hero | small | back | sleeve | ...
    box: list[int]                 # absolute template px (x0, y0, x1, y1), inclusive
    avoid_rows: list[list[int]] = []   # extra row ranges to keep clear (inclusive)

    @field_validator("region")
    @classmethod
    def _region(cls, v: str) -> str:
        if v not in T.REGIONS:
            raise ValueError(f"unknown region {v!r}")
        return v

    @field_validator("box")
    @classmethod
    def _box(cls, v: list[int]) -> list[int]:
        if len(v) != 4 or v[2] < v[0] or v[3] < v[1]:
            raise ValueError("box must be [x0, y0, x1, y1] with x1>=x0, y1>=y0")
        return v

    @model_validator(mode="after")
    def _inside(self) -> PrintSlot:
        x0, y0, x1, y1 = T.REGIONS[self.region]
        bx0, by0, bx1, by1 = self.box
        inset = T.BEVEL_INSET_PX
        if not (bx0 >= x0 + inset and by0 >= y0 + inset and bx1 <= x1 - inset and by1 <= y1 - inset):
            raise ValueError(f"slot {self.box} is not >= {inset}px inside {self.region} {T.REGIONS[self.region]}")
        return self


class Layer(Strict):
    model_config = ConfigDict(extra="forbid")
    id: str
    stage: str = "blocks"
    op: str = "paint"
    coverage: str = "add"
    colour: Any = None
    label: int | None = None
    shapes: list[dict[str, Any]] = []
    minus: list[dict[str, Any]] = []
    regions: list[str] = []
    frame: str = "abs"
    when: dict[str, Any] = {}
    unless: dict[str, Any] = {}
    fabric: bool = True
    dL: Any = 0.0                        # shade: lightness shift (number or expression)
    ramp: dict[str, Any] | None = None    # shade: {"axis": "y", "from": a, "to": b, "profile": "bell"}
    assert_rule: str = ""                # kit pieces: "band" (inside one limb band) | "shoe_top" (top row in 446-465), CLO-07
    note: str = ""

    @field_validator("stage")
    @classmethod
    def _stage(cls, v: str) -> str:
        if v not in STAGES:
            raise ValueError(f"stage must be one of {STAGES}")
        return v

    @field_validator("assert_rule")
    @classmethod
    def _assert_rule(cls, v: str) -> str:
        if v not in ASSERT_RULES:
            raise ValueError(f"assert_rule must be one of {ASSERT_RULES}")
        return v

    @field_validator("op")
    @classmethod
    def _op(cls, v: str) -> str:
        if v not in OPS:
            raise ValueError(f"op must be one of {OPS}")
        return v

    @field_validator("coverage")
    @classmethod
    def _cov(cls, v: str) -> str:
        if v not in COVERAGE:
            raise ValueError(f"coverage must be one of {COVERAGE}")
        return v

    @field_validator("frame")
    @classmethod
    def _frame(cls, v: str) -> str:
        if v not in FRAMES:
            raise ValueError(f"frame must be one of {FRAMES}")
        return v

    @model_validator(mode="after")
    def _shape_keys(self) -> Layer:
        for s in [*self.shapes, *self.minus]:
            keys = [k for k in s if k in SHAPE_KEYS]
            if len(keys) != 1:
                raise ValueError(f"layer {self.id}: a shape needs exactly one of {SHAPE_KEYS}, got {sorted(s)}")
        if self.op == "paint" and self.coverage != "erase" and self.colour is None:
            raise ValueError(f"layer {self.id}: a paint layer needs a colour")
        for pat in self.regions:
            if not fnmatch.filter(T.REGION_ORDER, pat):
                raise ValueError(f"layer {self.id}: region pattern {pat!r} matches nothing")
        return self


class Recipe(Strict):
    recipe_id: str
    template: str
    family: str
    title: str = ""
    version: int = 1
    fold_set: str = "default"
    cut: dict[str, list[str]] = {}
    defaults: dict[str, str] = {}
    params: dict[str, float] = {}
    vars: dict[str, Any] = {}
    layers: list[Layer] = []
    print_slots: list[PrintSlot] = []
    requires_bottom: dict[str, list[str]] = {}
    param_limits: dict[str, list[float]] = {}     # numeric params must lie in [lo, hi] (e.g. crop_hem 140-166: >= 2 px above row 170)
    notes: str = ""

    @field_validator("template")
    @classmethod
    def _tpl(cls, v: str) -> str:
        if v not in ("shirt", "pants"):
            raise ValueError("template must be shirt or pants")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Recipe:
        ids = [lay.id for lay in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.recipe_id}: duplicate layer ids")
        if any(lay.stage == "kit" for lay in self.layers):
            raise ValueError(f"{self.recipe_id}: stage 'kit' is reserved for painted kit pieces")
        for k, v in self.defaults.items():
            if k in self.cut and v not in self.cut[k]:
                raise ValueError(f"{self.recipe_id}: default {k}={v!r} is not in its allowed values {self.cut[k]}")
        # a `ref` must point at an EARLIER layer
        seen: set[str] = set()
        for lay in self.layers:
            for s in [*lay.shapes, *lay.minus]:
                if "ref" in s and s["ref"] not in seen:
                    raise ValueError(f"{self.recipe_id}/{lay.id}: ref {s['ref']!r} must name an earlier layer")
            seen.add(lay.id)
        return self

    @property
    def sha256(self) -> str:
        return sha256_of(self.model_dump(mode="json"))

    def attrs_with_defaults(self, attrs: dict[str, str] | None = None) -> dict[str, str]:
        out = dict(self.defaults)
        for k, v in (attrs or {}).items():
            if v is not None and v != "":
                out[k] = str(v)
        return out

    def validate_attrs(self, attrs: dict[str, str]) -> list[str]:
        """Messages for attribute values the recipe cannot draw (empty list = fine)."""
        bad = []
        for k, allowed in self.cut.items():
            if k in attrs and attrs[k] not in allowed:
                bad.append(f"{self.recipe_id}: {k}={attrs[k]!r} not in {allowed}")
        return bad


# --------------------------------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------------------------------
def _merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(parent))
    for k, v in child.items():
        if k == "extends":
            continue
        if k in ("cut", "defaults", "params", "vars") and isinstance(v, dict):
            out.setdefault(k, {}).update(v)
        elif k == "layers_extra":
            out.setdefault("layers", []).extend(v)
        elif k == "layers_remove":
            out["layers"] = [lay for lay in out.get("layers", []) if lay["id"] not in set(v)]
        elif k == "layers_replace":
            by = {lay["id"]: lay for lay in v}
            out["layers"] = [by.get(lay["id"], lay) for lay in out.get("layers", [])]
        else:
            out[k] = v
    return out


def _read_raw(recipe_id: str, dirs: tuple[Path, ...], depth: int = 0) -> dict[str, Any]:
    if depth > 4:
        raise RecipeError(f"{recipe_id}: extends chain too deep")
    for d in dirs:
        p = d / f"{recipe_id}.json"
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if raw.get("extends"):
                return _merge(_read_raw(raw["extends"], dirs, depth + 1), raw)
            return raw
    raise RecipeError(f"no recipe {recipe_id!r} in {[str(d) for d in dirs]}")


@lru_cache(maxsize=64)
def _load_cached(recipe_id: str, dirs: tuple[str, ...]) -> Recipe:
    raw = _read_raw(recipe_id, tuple(Path(d) for d in dirs))
    raw.pop("extends", None)
    raw.pop("_notes", None)
    try:
        return Recipe.model_validate(raw)
    except Exception as e:  # pydantic ValidationError -> RecipeError with the recipe id
        raise RecipeError(f"{recipe_id}: {e}") from e


def load_recipe(recipe_id: str, extra_dirs: list[Path] | None = None) -> Recipe:
    """Load and validate ``recipe_id`` (user dirs first, then the built-in kit). Cached; the result is immutable data."""
    dirs = tuple(str(Path(d)) for d in (extra_dirs or [])) + (str(RECIPE_DIR),)
    return _load_cached(recipe_id, dirs)


def list_recipe_ids(extra_dirs: list[Path] | None = None) -> list[str]:
    """Sorted ids of every recipe file (files starting with ``_`` are shared snippets and are skipped)."""
    ids: set[str] = set()
    for d in [*(extra_dirs or []), RECIPE_DIR]:
        ids.update(p.stem for p in Path(d).glob("*.json") if not p.name.startswith("_"))
    return sorted(ids)


# --------------------------------------------------------------------------------------------------------------------
# resolution: variables, conditions, resolved layers
# --------------------------------------------------------------------------------------------------------------------
def matches_when(cond: dict[str, Any], attrs: dict[str, str]) -> bool:
    """True when every attribute in ``cond`` equals (or is in) the given value(s)."""
    for k, want in cond.items():
        have = attrs.get(k)
        if isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def resolve_vars(recipe: Recipe, attrs: dict[str, str], params: dict[str, float] | None = None) -> dict[str, float]:
    """Numeric environment: recipe params (overridable), then ``vars`` in file order (tables keyed by a cut attribute)."""
    env: dict[str, float] = {k: float(v) for k, v in recipe.params.items()}
    for k, v in (params or {}).items():
        env[k] = float(v)
    for k, (lo, hi) in recipe.param_limits.items():
        if k in env and not (lo <= env[k] <= hi):
            raise RecipeError(f"{recipe.recipe_id}: {k}={env[k]} is outside its allowed range {lo}-{hi}")
    for name, spec in recipe.vars.items():
        if isinstance(spec, dict):
            attr = spec.get("attr")
            table = spec.get("map", {})
            key = attrs.get(attr, "") if attr else ""
            if key in table:
                val = table[key]
            elif "default" in spec:
                val = spec["default"]
            else:
                raise RecipeError(f"{recipe.recipe_id}: var {name}: no entry for {attr}={key!r} and no default")
            env[name] = eval_expr(val, env)
        else:
            env[name] = eval_expr(spec, env)
    return env


@dataclass(frozen=True)
class ResolvedLayer:
    """A layer whose conditions passed and whose numbers are plain floats (its descriptor is what the stack hash covers)."""

    id: str
    stage: str
    op: str
    coverage: str
    colour: Any
    label: int | None
    shapes: tuple[dict[str, Any], ...]
    minus: tuple[dict[str, Any], ...]
    regions: tuple[str, ...]
    frame: str
    fabric: bool
    dL: float
    ramp: dict[str, Any] | None
    assert_rule: str = ""

    def descriptor(self) -> dict[str, Any]:
        return {"id": self.id, "stage": self.stage, "op": self.op, "coverage": self.coverage, "colour": self.colour,
                "label": self.label, "shapes": list(self.shapes), "minus": list(self.minus), "regions": list(self.regions),
                "frame": self.frame, "fabric": self.fabric, "dL": self.dL, "ramp": self.ramp,
                "assert_rule": self.assert_rule}


def resolve_layers(recipe: Recipe, attrs: dict[str, str], env: dict[str, float]) -> list[ResolvedLayer]:
    """The recipe's layers whose ``when``/``unless`` pass, with every expression evaluated."""
    return resolve_layer_list(recipe.layers, attrs, env)


def resolve_layer_list(layers: list[Layer], attrs: dict[str, str], env: dict[str, float]) -> list[ResolvedLayer]:
    out: list[ResolvedLayer] = []
    for lay in layers:
        if lay.when and not matches_when(lay.when, attrs):
            continue
        if lay.unless and matches_when(lay.unless, attrs):
            continue
        out.append(ResolvedLayer(
            lay.id, lay.stage, lay.op, lay.coverage, lay.colour, lay.label,
            tuple(_num_tree(s, env) for s in lay.shapes), tuple(_num_tree(s, env) for s in lay.minus),
            tuple(lay.regions), lay.frame, lay.fabric, eval_expr(lay.dL, env),
            _num_tree(lay.ramp, env) if lay.ramp else None, lay.assert_rule))
    return out


def select_regions(patterns: tuple[str, ...] | list[str]) -> list[str]:
    """Region names matching any fnmatch pattern (all regions when ``patterns`` is empty), in template order."""
    if not patterns:
        return list(T.REGION_ORDER)
    return [k for k in T.REGION_ORDER if any(fnmatch.fnmatch(k, p) for p in patterns)]


# --------------------------------------------------------------------------------------------------------------------
# rasteriser: shapes -> boolean masks at 4x
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class Mask:
    """A boolean mask on the 4x canvas: ``arr`` covers canvas rows ``y0:y0+h`` and columns ``x0:x0+w``."""

    arr: np.ndarray
    x0: int
    y0: int

    @property
    def x1(self) -> int:
        return self.x0 + self.arr.shape[1]

    @property
    def y1(self) -> int:
        return self.y0 + self.arr.shape[0]

    def any(self) -> bool:
        return bool(self.arr.any())

    def sha(self) -> str:
        return sha256_of(np.packbits(self.arr).tobytes().hex() + f"|{self.x0}|{self.y0}|{self.arr.shape}")


CANVAS_W = T.WIDTH * SCALE
CANVAS_H = T.HEIGHT * SCALE


def _ring_points(cx: float, cy: float, rx: float, ry: float, a0: float, a1: float, n: int = 48) -> np.ndarray:
    t = np.radians(np.linspace(a0, a1, n))
    return np.stack([cx + rx * np.cos(t), cy + ry * np.sin(t)], axis=1)


def _dash_segments(pts: list[tuple[float, float]], on: float, off: float) -> list[list[tuple[float, float]]]:
    """Split a polyline into dashes of length ``on`` separated by ``off`` (template px)."""
    segs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    pos, drawing, left = 0.0, True, on
    for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
        length = math.hypot(bx - ax, by - ay)
        if length == 0:
            continue
        t = 0.0
        while t < length - 1e-9:
            step = min(left, length - t)
            p0 = (ax + (bx - ax) * t / length, ay + (by - ay) * t / length)
            p1 = (ax + (bx - ax) * (t + step) / length, ay + (by - ay) * (t + step) / length)
            if drawing:
                if not cur:
                    cur.append(p0)
                cur.append(p1)
            t += step
            left -= step
            pos += step
            if left <= 1e-9:
                if drawing and cur:
                    segs.append(cur)
                    cur = []
                drawing = not drawing
                left = on if drawing else off
    if cur:
        segs.append(cur)
    return segs


def shape_bbox(s: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Conservative (x0, y0, x1, y1) of a resolved shape in template px (exclusive max), None if empty."""
    if "rect" in s:
        x0, y0, x1, y1 = s["rect"]
        return (x0, y0, x1 + 1, y1 + 1) if x1 >= x0 and y1 >= y0 else None
    if "ellipse" in s:
        cx, cy, rx, ry = s["ellipse"]
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if "poly" in s:
        p = np.array(s["poly"], dtype=float)
        return (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()) if len(p) else None
    if "arc" in s:
        cx, cy, rx, ry, _th, _a0, _a1 = s["arc"]
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if "line" in s:
        p = np.array(s["line"], dtype=float)
        w = float(s.get("w", 1.0))
        return (p[:, 0].min() - w, p[:, 1].min() - w, p[:, 0].max() + w, p[:, 1].max() + w) if len(p) else None
    if "stripes" in s:
        st = s["stripes"]
        a, b, w = st["from"], st["to"], st["w"]
        c0, c1 = st["span"]
        if st["axis"] == "x":
            return (a, c0, b + w, c1 + 1)
        return (c0, a, c1 + 1, b + w)
    return None


def _draw_shape(d: ImageDraw.ImageDraw, s: dict[str, Any], ox: float, oy: float) -> None:
    """Draw one resolved shape into a PIL 'L' image whose origin is canvas pixel (ox, oy) (all values at 4x)."""
    k = SCALE
    if "rect" in s:
        x0, y0, x1, y1 = s["rect"]
        if x1 >= x0 and y1 >= y0:
            d.rectangle([x0 * k - ox, y0 * k - oy, (x1 + 1) * k - 1 - ox, (y1 + 1) * k - 1 - oy], fill=255)
    elif "ellipse" in s:
        cx, cy, rx, ry = s["ellipse"]
        d.ellipse([(cx - rx) * k - ox, (cy - ry) * k - oy, (cx + rx) * k - 1 - ox, (cy + ry) * k - 1 - oy], fill=255)
    elif "poly" in s:
        d.polygon([((x * k) - ox, (y * k) - oy) for x, y in s["poly"]], fill=255)
    elif "arc" in s:
        cx, cy, rx, ry, th, a0, a1 = s["arc"]
        outer = _ring_points(cx, cy, rx, ry, a0, a1)
        inner = _ring_points(cx, cy, max(rx - th, 0.01), max(ry - th, 0.01), a1, a0)
        pts = np.vstack([outer, inner]) * k - np.array([ox, oy])
        d.polygon([tuple(p) for p in pts], fill=255)
    elif "line" in s:
        w = max(1, int(round(float(s.get("w", 1.0)) * k)))
        # line points are PIXEL INDICES (the stroke runs through pixel centres), so a 1-px line at row 40 fills exactly row 40
        pts = [(float(x) + 0.5, float(y) + 0.5) for x, y in s["line"]]
        dash = s.get("dash")
        paths = _dash_segments(pts, float(dash[0]), float(dash[1])) if dash else [pts]
        for path in paths:
            xy = [(x * k - ox - 0.5, y * k - oy - 0.5) for x, y in path]        # PIL addresses pixel centres
            if len(xy) == 1:
                continue
            d.line(xy, fill=255, width=w, joint="curve")
            if w > 6:                                   # round-ish caps for thick strokes only
                r = w / 2.0
                for x, y in (xy[0], xy[-1]):
                    d.ellipse([x - r, y - r, x + r - 1, y + r - 1], fill=255)
    elif "stripes" in s:
        st = s["stripes"]
        a, b, step, w = st["from"], st["to"], st["step"], st["w"]
        c0, c1 = st["span"]
        if step <= 0:
            raise RecipeError("stripes step must be > 0")
        v = a
        while v <= b + 1e-9:
            if st["axis"] == "x":
                _draw_shape(d, {"rect": [v, c0, v + w - 1, c1]}, ox, oy)
            else:
                _draw_shape(d, {"rect": [c0, v, c1, v + w - 1]}, ox, oy)
            v += step


def _translate(s: dict[str, Any], dx: float, dy: float) -> dict[str, Any]:
    if dx == 0 and dy == 0:
        return s
    if "rect" in s:
        x0, y0, x1, y1 = s["rect"]
        return {"rect": [x0 + dx, y0 + dy, x1 + dx, y1 + dy]}
    if "ellipse" in s:
        cx, cy, rx, ry = s["ellipse"]
        return {"ellipse": [cx + dx, cy + dy, rx, ry]}
    if "poly" in s:
        return {"poly": [[x + dx, y + dy] for x, y in s["poly"]]}
    if "arc" in s:
        cx, cy, rx, ry, th, a0, a1 = s["arc"]
        return {"arc": [cx + dx, cy + dy, rx, ry, th, a0, a1]}
    if "line" in s:
        return {**s, "line": [[x + dx, y + dy] for x, y in s["line"]]}
    if "stripes" in s:
        st = dict(s["stripes"])
        if st["axis"] == "x":
            st["from"] += dx
            st["to"] += dx
            st["span"] = [st["span"][0] + dy, st["span"][1] + dy]
        else:
            st["from"] += dy
            st["to"] += dy
            st["span"] = [st["span"][0] + dx, st["span"][1] + dx]
        return {"stripes": st}
    return s


def rasterize(shapes: tuple[dict[str, Any], ...] | list[dict[str, Any]], *, dx: float = 0.0, dy: float = 0.0,
              named: dict[str, Mask] | None = None) -> Mask | None:
    """Union of ``shapes`` (template px, optionally translated by ``dx, dy``) as a 4x mask clipped to the canvas, or None
    when empty. ``named`` resolves ``{"ref": id}`` shapes to earlier masks."""
    drawn = [(_translate(s, dx, dy)) for s in shapes if "ref" not in s]
    refs = [s["ref"] for s in shapes if "ref" in s]
    boxes = [b for b in (shape_bbox(s) for s in drawn) if b is not None]
    parts: list[Mask] = []
    if boxes:
        k = SCALE
        x0 = max(0, int(math.floor(min(b[0] for b in boxes) * k)) - 2)
        y0 = max(0, int(math.floor(min(b[1] for b in boxes) * k)) - 2)
        x1 = min(CANVAS_W, int(math.ceil(max(b[2] for b in boxes) * k)) + 2)
        y1 = min(CANVAS_H, int(math.ceil(max(b[3] for b in boxes) * k)) + 2)
        if x1 > x0 and y1 > y0:
            img = Image.new("L", (x1 - x0, y1 - y0), 0)
            dr = ImageDraw.Draw(img)
            for s in drawn:
                _draw_shape(dr, s, x0, y0)
            parts.append(Mask(np.asarray(img, dtype=np.uint8) > 0, x0, y0))
    for r in refs:
        if named is None or r not in named:
            raise RecipeError(f"unknown mask ref {r!r}")
        m = named[r]
        parts.append(Mask(m.arr.copy(), m.x0, m.y0))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    x0 = min(p.x0 for p in parts)
    y0 = min(p.y0 for p in parts)
    x1 = max(p.x1 for p in parts)
    y1 = max(p.y1 for p in parts)
    arr = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    for p in parts:
        arr[p.y0 - y0:p.y1 - y0, p.x0 - x0:p.x1 - x0] |= p.arr
    return Mask(arr, x0, y0)


def region_mask_for(mask: Mask, region: str) -> Mask | None:
    """``mask`` clipped to the 4x box of ``region`` (cropped to the intersection), or None if they do not meet."""
    rs, cs = T.region_slices_scaled(region, SCALE)
    x0, x1 = max(mask.x0, cs.start), min(mask.x1, cs.stop)
    y0, y1 = max(mask.y0, rs.start), min(mask.y1, rs.stop)
    if x1 <= x0 or y1 <= y0:
        return None
    sub = mask.arr[y0 - mask.y0:y1 - mask.y0, x0 - mask.x0:x1 - mask.x0]
    return Mask(sub, x0, y0) if sub.any() else None


def mask_and_regions(mask: Mask, regions: list[str]) -> Mask | None:
    """``mask`` restricted to the union of the 4x boxes of ``regions`` (returns a mask on the original bbox)."""
    keep = np.zeros_like(mask.arr)
    for region in regions:
        rs, cs = T.region_slices_scaled(region, SCALE)
        x0, x1 = max(mask.x0, cs.start), min(mask.x1, cs.stop)
        y0, y1 = max(mask.y0, rs.start), min(mask.y1, rs.stop)
        if x1 > x0 and y1 > y0:
            keep[y0 - mask.y0:y1 - mask.y0, x0 - mask.x0:x1 - mask.x0] = True
    out = mask.arr & keep
    return Mask(out, mask.x0, mask.y0) if out.any() else None


def subtract(mask: Mask, other: Mask | None) -> Mask:
    """``mask`` minus ``other`` (same canvas coordinates)."""
    if other is None:
        return mask
    arr = mask.arr.copy()
    x0, x1 = max(mask.x0, other.x0), min(mask.x1, other.x1)
    y0, y1 = max(mask.y0, other.y0), min(mask.y1, other.y1)
    if x1 > x0 and y1 > y0:
        arr[y0 - mask.y0:y1 - mask.y0, x0 - mask.x0:x1 - mask.x0] &= ~other.arr[y0 - other.y0:y1 - other.y0,
                                                                                  x0 - other.x0:x1 - other.x0]
    return Mask(arr, mask.x0, mask.y0)
