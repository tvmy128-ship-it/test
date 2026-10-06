"""Kit inventory and the kit enums of the LLM-facing ``DuoSpec`` (APP_SPEC §6.2, bible §3.1, §3.4, §8.1.4).

The spec's kit fields (``HairKit``, ``FringeKit``, ``BackKit``, ``TopRecipeKit``, ``InnerTopKit``, ``BottomRecipeKit``,
``FabricKit``, ``ShoeKit``, ``SkinToneKit``, ``EyeShapeKit``, ``MouthKit``) come from the kit manifest, which is the merge of
``duoskin/builtin_kits`` and the user's kits (``DATA\\kits\\manifest.json``). This module owns that inventory:

* :class:`KitInventory` is an immutable snapshot with the human-written fields the slot maps need (``prompt_phrase``,
  ``clump_k``, ``parting``, ``default_fringe``, ``weave_k``, ``pattern_phrase``, ``fold_k``, ``material``) and the
  availability flags. :func:`builtin_inventory` builds the **demo defaults** (the kit hair is empty, so ``HairKit`` is only
  ``hair_custom``); :func:`inventory_from_manifest` merges a generated manifest on top of them.
* The enum types are *annotations* (``K.HairKit`` and friends): a lower-cased string that is checked against the **current**
  inventory at validation time, and whose JSON schema lists the current ids (sorted, sentinels included). The classes in
  ``models/spec.py`` therefore never have to be rebuilt when a kit is added; ``set_default_inventory`` is enough. A new
  :func:`kit_manifest_sha` is the signal to re-run the schema smoke test (CHK-S10) and re-render ``<kit_inventory>``.
* ``schema_mode("placeholder")`` emits ``{"type": "string", "x-kit": "HairKit"}`` instead of the id list, which is what
  ``SCHEMAS.lock`` hashes, so the lock does not change when the user adds a hair style. ``schema_mode("string")`` is the
  bible's "Schema is too complex" fallback (§3.1): kit ids stay validated strings and the linter rejects unknown ids.

Manifest shape consumed by :func:`inventory_from_manifest` (every key is optional; what is missing falls back to the demo
defaults)::

    {"hair": {"<style_id>": {"prompt_phrase", "length_class", "silhouette_class", "clump_k", "parting", "default_fringe",
                             "symmetric", "supported_adjustments"}},          # a list of {"id": ..., ...} is accepted too
     "hair_modules": {"fringe": {"<id>": {"prompt_phrase"}}, "back": {"<id>": {"prompt_phrase"}}},
     "hair_pair_iou": {"<a>|<b>": {"front": 0.4, "side": 0.5}},
     "recipes": {"<id>": {"template", "family", "prompt_phrase", "cut", "requires_bottom", "fold_k", "inner_ok"}},
     "fabrics": {"<id>": {"material", "prompt_phrase", "pattern_phrase", "weave_k"}},
     "shoes": {"<id>": {"prompt_phrase", "top_row"}},
     "skin_tones": [{"id", "name", "hex"}],
     "eye_shapes": {"<id>": {"prompt_phrase"}},  "mouth_styles": ["<id>", ...],
     "flags": {"hair_kit_empty", "head_base_present", "body_base_present", "house_style_present", "makeup": "unavailable"}}
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from pydantic_core import core_schema

from duoskin.models.common import sha256_of

_DATA = Path(__file__).resolve().parent.parent / "data"
_BUILTIN = Path(__file__).resolve().parent.parent / "builtin_kits"

KIT_KINDS: tuple[str, ...] = ("HairKit", "FringeKit", "BackKit", "TopRecipeKit", "InnerTopKit", "BottomRecipeKit", "FabricKit",
                              "ShoeKit", "SkinToneKit", "EyeShapeKit", "MouthKit")
SENTINELS: dict[str, tuple[str, ...]] = {
    "HairKit": ("hair_custom",),
    "FringeKit": ("kit_default", "none"),
    "BackKit": ("kit_default",),
    "InnerTopKit": ("none",),
}
INNER_FAMILIES = ("tee", "vest", "raglan", "crop_top")     # shirt families that may be the layer visible inside an open front
PARTINGS = ("left", "right", "centre", "none")


class KitError(ValueError):
    """The kit inventory is unusable (a missing human-written phrase, an unknown id)."""


# ------------------------------------------------------------------------------------------------ entries
@dataclass(frozen=True)
class HairStyle:
    id: str
    prompt_phrase: str
    length_class: str = ""
    silhouette_class: str = ""
    clump_k: int = 6
    parting: str = "none"
    default_fringe: str = "none"
    symmetric: bool = False
    supported_adjustments: tuple[str, ...] = ()


@dataclass(frozen=True)
class HairModule:
    id: str
    prompt_phrase: str = ""


@dataclass(frozen=True)
class Recipe:
    id: str
    template: str                       # "shirt" | "pants"
    family: str                         # tee, raglan, hoodie, jacket, crop_top, vest, skirt, jeans, shorts, cargos
    prompt_phrase: str
    cut: Mapping[str, tuple[str, ...]] = field(default_factory=dict)            # allowed values per attribute (empty = unrestricted)
    requires_bottom: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # crop_top: {"waist": ("high",)}
    fold_k: int = 3
    inner_ok: bool = False


@dataclass(frozen=True)
class Fabric:
    id: str
    material: str                       # material_family
    fabric_phrase: str
    pattern_phrase: str = "plain weave"
    weave_k: int = 16


@dataclass(frozen=True)
class Shoe:
    id: str
    prompt_phrase: str
    top_row: int = 0


@dataclass(frozen=True)
class SkinTone:
    id: str
    name: str
    hex: str


@dataclass(frozen=True)
class EyeShape:
    id: str
    prompt_phrase: str


@dataclass(frozen=True)
class KitFlags:
    hair_kit_empty: bool = True
    head_base_present: bool = False
    body_base_present: bool = False
    house_style_present: bool = False
    makeup_available: bool = False     # the manifest says "makeup: unavailable" in v1


@dataclass(frozen=True)
class KitInventory:
    """An immutable snapshot of what the kits can build. ``ids(kind)`` is what the spec enums accept."""

    hair: Mapping[str, HairStyle] = field(default_factory=dict)
    fringes: Mapping[str, HairModule] = field(default_factory=dict)
    backs: Mapping[str, HairModule] = field(default_factory=dict)
    recipes: Mapping[str, Recipe] = field(default_factory=dict)
    fabrics: Mapping[str, Fabric] = field(default_factory=dict)
    shoes: Mapping[str, Shoe] = field(default_factory=dict)
    skin_tones: Mapping[str, SkinTone] = field(default_factory=dict)
    eye_shapes: Mapping[str, EyeShape] = field(default_factory=dict)
    mouths: tuple[str, ...] = ()
    hair_pair_iou: Mapping[str, float] = field(default_factory=dict)   # "a|b" (sorted) -> max(front, side) silhouette IoU
    flags: KitFlags = field(default_factory=KitFlags)

    # ---- enum values -------------------------------------------------------------------------
    def ids(self, kind: str) -> tuple[str, ...]:
        """Sorted ids of one kit kind, including the sentinels the bible lists."""
        if kind == "HairKit":
            base = set(self.hair)
        elif kind == "FringeKit":
            base = set(self.fringes)
        elif kind == "BackKit":
            base = set(self.backs)
        elif kind == "TopRecipeKit":
            base = {r.id for r in self.recipes.values() if r.template == "shirt"}
        elif kind == "InnerTopKit":
            base = {r.id for r in self.recipes.values() if r.template == "shirt" and r.inner_ok}
        elif kind == "BottomRecipeKit":
            base = {r.id for r in self.recipes.values() if r.template == "pants"}
        elif kind == "FabricKit":
            base = set(self.fabrics)
        elif kind == "ShoeKit":
            base = set(self.shoes)
        elif kind == "SkinToneKit":
            base = set(self.skin_tones)
        elif kind == "EyeShapeKit":
            base = set(self.eye_shapes)
        elif kind == "MouthKit":
            base = set(self.mouths)
        else:
            raise KitError(f"unknown kit kind {kind!r}")
        return tuple(sorted(base | set(SENTINELS.get(kind, ()))))

    # ---- lookups -----------------------------------------------------------------------------
    def recipe(self, recipe_id: str) -> Recipe:
        try:
            return self.recipes[recipe_id]
        except KeyError:
            raise KitError(f"unknown recipe {recipe_id!r}") from None

    def fabric(self, fabric_id: str) -> Fabric:
        try:
            return self.fabrics[fabric_id]
        except KeyError:
            raise KitError(f"unknown fabric {fabric_id!r}") from None

    def shoe(self, shoe_id: str) -> Shoe:
        try:
            return self.shoes[shoe_id]
        except KeyError:
            raise KitError(f"unknown shoe style {shoe_id!r}") from None

    def hair_style(self, style_id: str) -> HairStyle | None:
        """The kit style, or ``None`` for ``hair_custom`` (and for an id that is not in the kit)."""
        return self.hair.get(style_id)

    def skin_hex(self, tone_id: str) -> str:
        try:
            return self.skin_tones[tone_id].hex
        except KeyError:
            raise KitError(f"unknown skin tone {tone_id!r}") from None

    def hair_iou(self, a: str, b: str) -> float | None:
        """Precomputed front/side silhouette IoU of two kit hair styles (the larger of the two views), or ``None``."""
        key = "|".join(sorted((a, b)))
        return self.hair_pair_iou.get(key)

    def kit_phrase_problems(self) -> list[str]:
        """Entries without the human-written phrase the compiler needs (reported by the doctor and by the tests)."""
        bad = [f"recipe {r.id}" for r in self.recipes.values() if not r.prompt_phrase.strip()]
        bad += [f"shoe {s.id}" for s in self.shoes.values() if not s.prompt_phrase.strip()]
        bad += [f"fabric {f.id}" for f in self.fabrics.values() if not f.fabric_phrase.strip()]
        bad += [f"hair {h.id}" for h in self.hair.values() if not h.prompt_phrase.strip()]
        return sorted(bad)

    # ---- the <kit_inventory> block -----------------------------------------------------------
    def to_manifest_view(self) -> dict[str, Any]:
        """The JSON the cached ``<kit_inventory>`` system block shows (bible §8.1.4): ids, phrases, compatibility, flags."""
        recipes = {}
        for r in sorted(self.recipes.values(), key=lambda x: x.id):
            row: dict[str, Any] = {"template": r.template, "family": r.family, "prompt_phrase": r.prompt_phrase,
                                   "fold_k": r.fold_k}
            if r.cut:
                row["cut"] = {k: list(v) for k, v in sorted(r.cut.items())}
            if r.requires_bottom:
                row["requires_bottom"] = {k: list(v) for k, v in sorted(r.requires_bottom.items())}
            if r.inner_ok:
                row["inner_ok"] = True
            recipes[r.id] = row
        compat = ["inner_recipe_id is set only when front is open or layered, else none",
                  "mouth_style must be one of mouth_styles (the mouth rig)", "a hair_custom hair never uses fringe_id kit_default",
                  "makeup.kind is none unless availability.makeup is available"]
        for r in sorted(self.recipes.values(), key=lambda x: x.id):
            for k, v in sorted(r.requires_bottom.items()):
                compat.append(f"{r.id} requires bottom.{k} = {'|'.join(v)}")
        return {
            "availability": {"hair_kit_empty": not bool(self.hair), "head_base_present": self.flags.head_base_present,
                             "body_base_present": self.flags.body_base_present,
                             "house_style_present": self.flags.house_style_present,
                             "makeup": "available" if self.flags.makeup_available else "unavailable"},
            "compatibility": compat,
            "recipes": recipes,
            "hair": {h.id: {"prompt_phrase": h.prompt_phrase, "length_class": h.length_class,
                            "silhouette_class": h.silhouette_class, "clump_k": h.clump_k, "parting": h.parting,
                            "default_fringe": h.default_fringe, "supported_adjustments": list(h.supported_adjustments)}
                     for h in sorted(self.hair.values(), key=lambda x: x.id)},
            "hair_custom": "use only when no kit style fits, or when availability.hair_kit_empty is true",
            "hair_pair_iou": {k: self.hair_pair_iou[k] for k in sorted(self.hair_pair_iou)},
            "fringe_modules": {m.id: m.prompt_phrase for m in sorted(self.fringes.values(), key=lambda x: x.id)},
            "back_modules": {m.id: m.prompt_phrase for m in sorted(self.backs.values(), key=lambda x: x.id)},
            "fabrics": {f.id: {"material": f.material, "fabric_phrase": f.fabric_phrase, "pattern_phrase": f.pattern_phrase,
                               "weave_k": f.weave_k} for f in sorted(self.fabrics.values(), key=lambda x: x.id)},
            "shoes": {s.id: {"prompt_phrase": s.prompt_phrase, "top_row": s.top_row}
                      for s in sorted(self.shoes.values(), key=lambda x: x.id)},
            "skin_tones": [s.id for s in sorted(self.skin_tones.values(), key=lambda x: x.id)],
            "eye_shapes": [e.id for e in sorted(self.eye_shapes.values(), key=lambda x: x.id)],
            "mouth_styles": sorted(self.mouths),
            "enums": {kind: list(self.ids(kind)) for kind in KIT_KINDS},
        }

    @property
    def sha256(self) -> str:
        """``kit_manifest_sha``: changes whenever an id, a phrase or a flag changes."""
        return sha256_of(self.to_manifest_view())


def kit_manifest_sha(inv: KitInventory | None = None) -> str:
    """The ``kit_manifest_sha`` of ``inv`` (default: the current inventory)."""
    return (inv or current_inventory()).sha256


# ------------------------------------------------------------------------------------------------ building
def _phrases() -> dict[str, Any]:
    return _load_json("phrases.json")


@lru_cache(maxsize=8)
def _load_json_cached(name: str) -> str:
    return (_DATA / name).read_text(encoding="utf-8")


def _load_json(name: str) -> dict[str, Any]:
    return json.loads(_load_json_cached(name))


def _tuple(v: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in (v or ()))


def _recipe_from_builtin(recipe_id: str, phrases: Mapping[str, str]) -> Recipe:
    from duoskin.imaging.recipes import load_recipe  # lazy: pulls in numpy and Pillow

    r = load_recipe(recipe_id)
    phrase = phrases.get(recipe_id, "")
    return Recipe(id=r.recipe_id, template=r.template, family=r.family, prompt_phrase=phrase,
                  cut={k: tuple(v) for k, v in r.cut.items()},
                  requires_bottom={k: tuple(v) for k, v in r.requires_bottom.items()},
                  fold_k=int(_phrases()["defaults"]["fold_k"]), inner_ok=(r.template == "shirt" and r.family in INNER_FAMILIES))


@lru_cache(maxsize=1)
def builtin_inventory() -> KitInventory:
    """The demo defaults: built-in recipes, shoes, fabrics, skin tones and the default 2D face canvas, with an **empty hair kit**
    (so ``HairKit`` is just ``hair_custom``, bible D24) and ``makeup: unavailable``."""
    from duoskin.imaging import face_canvas as fc  # lazy

    ph = _phrases()
    recipe_phr = ph["kit_defaults"]["recipe_phrase"]
    from duoskin.imaging.recipes import list_recipe_ids

    recipes = {rid: _recipe_from_builtin(rid, recipe_phr) for rid in list_recipe_ids()}
    shoes_raw = json.loads((_BUILTIN / "shoes.json").read_text(encoding="utf-8"))["styles"]
    shoe_phr = ph["kit_defaults"]["shoe_phrase"]
    shoes = {sid: Shoe(id=sid, prompt_phrase=shoe_phr.get(sid, ""), top_row=int(v.get("top_row", 0))) for sid, v in shoes_raw.items()}
    fabrics_raw = json.loads((_BUILTIN / "fabrics.json").read_text(encoding="utf-8"))["fabrics"]
    fabrics = {fid: Fabric(id=fid, material=str(v.get("material_family", "")), fabric_phrase=str(v.get("prompt_phrase", "")),
                           pattern_phrase=str(v.get("pattern_phrase", ph["defaults"]["pattern_phrase"])),
                           weave_k=int(v.get("weave_k", ph["defaults"]["weave_k"]))) for fid, v in fabrics_raw.items()}
    tones = {t["id"]: SkinTone(id=t["id"], name=t["name"], hex=t["hex"]) for t in _load_json("skin_tones.json")["tones"]}
    eyes = {e: EyeShape(id=e, prompt_phrase=ph["face"]["eye_phrase"].get(e, "")) for e in fc.eye_shape_kit()}
    return KitInventory(
        recipes=recipes, fabrics=fabrics, shoes=shoes, skin_tones=tones, eye_shapes=eyes, mouths=tuple(sorted(fc.mouth_kit())),
        fringes={}, backs={}, hair={}, flags=KitFlags(hair_kit_empty=True))


def _hair_rows(raw: Any) -> dict[str, HairStyle]:
    rows = raw.items() if isinstance(raw, Mapping) else ((x["id"], x) for x in (raw or ()))
    out: dict[str, HairStyle] = {}
    for hid, v in rows:
        parting = str(v.get("parting", "none"))
        if parting not in PARTINGS:
            raise KitError(f"hair {hid}: parting {parting!r} is not one of {PARTINGS}")
        out[str(hid)] = HairStyle(
            id=str(hid), prompt_phrase=str(v.get("prompt_phrase", "")), length_class=str(v.get("length_class", "")),
            silhouette_class=str(v.get("silhouette_class", "")), clump_k=int(v.get("clump_k", _phrases()["hair"]["default_clump_k"])),
            parting=parting, default_fringe=str(v.get("default_fringe", "none")), symmetric=bool(v.get("symmetric", False)),
            supported_adjustments=_tuple(v.get("supported_adjustments")))
    return out


def _modules(raw: Any) -> dict[str, HairModule]:
    rows = raw.items() if isinstance(raw, Mapping) else ((x["id"], x) for x in (raw or ()))
    return {str(k): HairModule(id=str(k), prompt_phrase=str((v or {}).get("prompt_phrase", "")) if isinstance(v, Mapping) else str(v))
            for k, v in rows}


def inventory_from_manifest(manifest: Mapping[str, Any] | None, base: KitInventory | None = None) -> KitInventory:
    """Merge a generated ``kits/manifest.json`` (see the module docstring) over ``base`` (default: :func:`builtin_inventory`).

    An empty or ``None`` manifest returns the demo defaults. Entries in the manifest replace built-in entries with the same id.
    """
    base = base or builtin_inventory()
    if not manifest:
        return base
    ph = _phrases()
    hair = dict(base.hair)
    hair.update(_hair_rows(manifest.get("hair")))
    fringes = dict(base.fringes)
    backs = dict(base.backs)
    mods = manifest.get("hair_modules") or {}
    fringes.update(_modules(mods.get("fringe")))
    backs.update(_modules(mods.get("back")))
    recipes = dict(base.recipes)
    for rid, v in (manifest.get("recipes") or {}).items():
        if rid in recipes:
            old = recipes[rid]
            recipes[rid] = Recipe(
                id=rid, template=str(v.get("template", old.template)), family=str(v.get("family", old.family)),
                prompt_phrase=str(v.get("prompt_phrase", old.prompt_phrase)),
                cut={k: tuple(x) for k, x in (v.get("cut") or old.cut).items()},
                requires_bottom={k: tuple(x) for k, x in (v.get("requires_bottom") or old.requires_bottom).items()},
                fold_k=int(v.get("fold_k", old.fold_k)), inner_ok=bool(v.get("inner_ok", old.inner_ok)))
        else:
            tpl = str(v["template"])
            fam = str(v.get("family", rid))
            recipes[rid] = Recipe(id=rid, template=tpl, family=fam, prompt_phrase=str(v.get("prompt_phrase", "")),
                                  cut={k: tuple(x) for k, x in (v.get("cut") or {}).items()},
                                  requires_bottom={k: tuple(x) for k, x in (v.get("requires_bottom") or {}).items()},
                                  fold_k=int(v.get("fold_k", ph["defaults"]["fold_k"])),
                                  inner_ok=bool(v.get("inner_ok", tpl == "shirt" and fam in INNER_FAMILIES)))
    fabrics = dict(base.fabrics)
    for fid, v in (manifest.get("fabrics") or {}).items():
        fabrics[fid] = Fabric(id=fid, material=str(v.get("material", v.get("material_family", ""))),
                              fabric_phrase=str(v.get("fabric_phrase", v.get("prompt_phrase", ""))),
                              pattern_phrase=str(v.get("pattern_phrase", ph["defaults"]["pattern_phrase"])),
                              weave_k=int(v.get("weave_k", ph["defaults"]["weave_k"])))
    shoes = dict(base.shoes)
    for sid, v in (manifest.get("shoes") or {}).items():
        shoes[sid] = Shoe(id=sid, prompt_phrase=str(v.get("prompt_phrase", "")), top_row=int(v.get("top_row", 0)))
    tones = dict(base.skin_tones)
    for t in manifest.get("skin_tones") or ():
        tones[t["id"]] = SkinTone(id=t["id"], name=str(t.get("name", t["id"])), hex=str(t["hex"]))
    eyes = dict(base.eye_shapes)
    raw_eyes = manifest.get("eye_shapes") or manifest.get("eye_shape_variants")
    if raw_eyes:
        rows = raw_eyes.items() if isinstance(raw_eyes, Mapping) else ((e, {}) if isinstance(e, str) else (e["id"], e) for e in raw_eyes)
        for eid, v in rows:
            eyes[str(eid)] = EyeShape(id=str(eid), prompt_phrase=str(v.get("prompt_phrase", ph["face"]["eye_phrase"].get(eid, ""))))
    mouths = tuple(sorted(set(base.mouths) | set(manifest.get("mouth_styles") or ()))) if manifest.get("mouth_styles") else base.mouths
    iou = dict(base.hair_pair_iou)
    for key, v in (manifest.get("hair_pair_iou") or {}).items():
        a, _, b = str(key).partition("|")
        iou["|".join(sorted((a, b)))] = float(max(v.values())) if isinstance(v, Mapping) else float(v)
    fl = {**(manifest.get("flags") or {}), **{k: manifest[k] for k in ("hair_kit_empty", "head_base_present", "body_base_present",
                                                                       "house_style_present", "makeup") if k in manifest}}
    flags = KitFlags(
        hair_kit_empty=not bool(hair),
        head_base_present=bool(fl.get("head_base_present", base.flags.head_base_present)),
        body_base_present=bool(fl.get("body_base_present", base.flags.body_base_present)),
        house_style_present=bool(fl.get("house_style_present", base.flags.house_style_present)),
        makeup_available=(fl.get("makeup", "unavailable") not in ("unavailable", False, None)))
    inv = KitInventory(hair=hair, fringes=fringes, backs=backs, recipes=recipes, fabrics=fabrics, shoes=shoes, skin_tones=tones,
                       eye_shapes=eyes, mouths=mouths, hair_pair_iou=iou, flags=flags)
    return inv


def load_inventory(manifest_path: str | Path | None = None) -> KitInventory:
    """``inventory_from_manifest`` of the JSON file at ``manifest_path`` (default: ``DATA\\kits\\manifest.json`` when it exists)."""
    path = Path(manifest_path) if manifest_path else None
    if path is None:
        try:
            from duoskin import config

            path = config.paths().kits_dir / "manifest.json"
        except Exception:    # noqa: BLE001 - no data folder (tests, tools): the demo defaults apply
            path = None
    if path is None or not path.exists():
        return builtin_inventory()
    return inventory_from_manifest(json.loads(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------------------------------------------ the current inventory
_LOCK = threading.Lock()
_DEFAULT: KitInventory | None = None
_CURRENT: contextvars.ContextVar[KitInventory | None] = contextvars.ContextVar("duoskin_kit_inventory", default=None)
_MODE: contextvars.ContextVar[str] = contextvars.ContextVar("duoskin_kit_schema_mode", default="live")


def current_inventory() -> KitInventory:
    """The inventory the spec enums validate against: a ``use_inventory`` override, else the process default (demo defaults)."""
    global _DEFAULT
    cur = _CURRENT.get()
    if cur is not None:
        return cur
    if _DEFAULT is None:
        with _LOCK:
            if _DEFAULT is None:
                _DEFAULT = builtin_inventory()
    return _DEFAULT


def set_default_inventory(inv: KitInventory | None) -> None:
    """Install the process-wide inventory (startup, after ``build-kit-manifest``); ``None`` resets to the demo defaults."""
    global _DEFAULT
    with _LOCK:
        _DEFAULT = inv


@contextlib.contextmanager
def use_inventory(inv: KitInventory) -> Iterator[KitInventory]:
    """Validate and emit schemas against ``inv`` inside the block (thread- and task-local)."""
    tok = _CURRENT.set(inv)
    try:
        yield inv
    finally:
        _CURRENT.reset(tok)


@contextlib.contextmanager
def schema_mode(mode: Literal["live", "placeholder", "string"]) -> Iterator[None]:
    """``live``: kit ids as a JSON-schema enum; ``placeholder``: ``x-kit`` markers (what SCHEMAS.lock hashes); ``string``: plain strings."""
    if mode not in ("live", "placeholder", "string"):
        raise ValueError(mode)
    tok = _MODE.set(mode)
    try:
        yield
    finally:
        _MODE.reset(tok)


# ------------------------------------------------------------------------------------------------ the annotation
def _lower(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


class KitId:
    """Annotation marker: a lower-cased string that must be one of ``current_inventory().ids(kind)``."""

    def __init__(self, kind: str):
        if kind not in KIT_KINDS:
            raise KitError(f"unknown kit kind {kind!r}")
        self.kind = kind

    def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> core_schema.CoreSchema:
        kind = self.kind

        def check(v: str) -> str:
            valid = current_inventory().ids(kind)
            if v not in valid:
                shown = ", ".join(valid[:40]) + (", ..." if len(valid) > 40 else "")
                raise ValueError(f"unknown {kind} id {v!r}; valid ids: {shown}")
            return v

        inner = core_schema.no_info_before_validator_function(_lower, handler(str))
        return core_schema.no_info_after_validator_function(check, inner)

    def __get_pydantic_json_schema__(self, cs: core_schema.CoreSchema, handler: Any) -> dict[str, Any]:
        js = dict(handler(core_schema.str_schema()))
        mode = _MODE.get()
        if mode == "live":
            js["enum"] = list(current_inventory().ids(self.kind))
        elif mode == "placeholder":
            js["x-kit"] = self.kind
        return js


def kit_type(kind: str) -> Any:
    return Annotated[str, KitId(kind)]


HairKit = kit_type("HairKit")
FringeKit = kit_type("FringeKit")
BackKit = kit_type("BackKit")
TopRecipeKit = kit_type("TopRecipeKit")
InnerTopKit = kit_type("InnerTopKit")
BottomRecipeKit = kit_type("BottomRecipeKit")
FabricKit = kit_type("FabricKit")
ShoeKit = kit_type("ShoeKit")
SkinToneKit = kit_type("SkinToneKit")
EyeShapeKit = kit_type("EyeShapeKit")
MouthKit = kit_type("MouthKit")

K = SimpleNamespace(HairKit=HairKit, FringeKit=FringeKit, BackKit=BackKit, TopRecipeKit=TopRecipeKit, InnerTopKit=InnerTopKit,
                    BottomRecipeKit=BottomRecipeKit, FabricKit=FabricKit, ShoeKit=ShoeKit, SkinToneKit=SkinToneKit,
                    EyeShapeKit=EyeShapeKit, MouthKit=MouthKit)


def kit_literal(kind: str, inv: KitInventory | None = None) -> Any:
    """The sorted ``Literal[...]`` of one kit kind for callers that want a closed type (API validation, tests)."""
    values = (inv or current_inventory()).ids(kind)
    return Literal.__getitem__(values)


def enum_values(inv: KitInventory | None = None) -> dict[str, list[str]]:
    """All kit enums as sorted lists (what the startup smoke test and ``<kit_inventory>`` use)."""
    inv = inv or current_inventory()
    return {kind: list(inv.ids(kind)) for kind in KIT_KINDS}


def validate_ids(values: Sequence[tuple[str, str]], inv: KitInventory | None = None) -> list[str]:
    """Problems for ``(kind, id)`` pairs against ``inv`` (the linter's unknown-id check)."""
    inv = inv or current_inventory()
    return [f"{kind}: unknown id {v!r}" for kind, v in values if v not in inv.ids(kind)]
