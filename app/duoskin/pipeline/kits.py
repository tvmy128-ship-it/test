"""Kits: the manifest, the availability flags and where the kit files are (APP_SPEC §5.3, §10.1, §16).

Three sources are merged, in this order of precedence: the user's kits in ``DATA\\kits\\`` (they win), the demo kits in
``duoskin/builtin_kits/demo/`` (only in demo mode or when asked for: stand-ins that are labelled ``origin=code_generated``) and the
built-in kits defined by code (``duoskin/builtin_kits``: recipes, shoes, procedural fabrics and folds, the default 2D face
canvas). ``build_manifest`` writes the merged ``DATA\\kits\\manifest.json`` and ``install_inventory`` makes the kit enums of
``DuoSpec`` follow it.

The availability flags (``head_base_present``, ``hair_kit_empty``, ``body_base_present``, ``house_style_present``,
``blender_present``, ``dreamsim_present``) only select the labelled reduced modes of §16; none of them blocks anything (§2 S25).
``head_base_present`` is true only when a variant folder holds every generated file **and** ``studio_validated.json`` (§5.3).

The loader refuses any kit asset without an ``origin`` in ``ORIGINS`` and a sha256 (POL-08): such an entry is listed in the
manifest under ``rejected`` and is never offered to the planner.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duoskin.models import kitenums
from duoskin.models.common import sha256_of

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.kits")

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_kits"
DEMO_DIR = BUILTIN_DIR / "demo"
ORIGINS = ("user_made", "code_generated", "app_generated", "roblox_reference")
HEAD_BASE_FILES = ("face_canvas.json", "uv_lut.npz", "zones.json", "stretch.npz", "head.fbx", "head_mesh.sha256", "studio_validated.json")
_lock = threading.Lock()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def folder_sha(folder: Path, *, skip: tuple[str, ...] = ()) -> str:
    """One hash over every file of a kit folder (relative names and contents), for ``kit_subset_sha`` and the manifest."""
    h = hashlib.sha256()
    for p in sorted(x for x in folder.rglob("*") if x.is_file() and x.name not in skip):
        h.update(p.relative_to(folder).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(file_sha(p).encode("ascii"))
    return h.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _origin_problem(meta: dict[str, Any]) -> str | None:
    if meta.get("origin") not in ORIGINS:
        return f"origin must be one of {', '.join(ORIGINS)} (got {meta.get('origin')!r})"
    return None


@dataclass
class KitContext:
    """Everything a lane needs to know about the kits of this run."""

    inventory: kitenums.KitInventory
    manifest: dict[str, Any]
    flags: dict[str, bool]
    user_dir: Path
    demo: bool = False
    roots: list[Path] = field(default_factory=list)        # kit roots in precedence order (user, demo)

    # ---- hair
    def hair_style_dir(self, style_id: str) -> Path | None:
        for r in self.roots:
            p = r / "hair" / style_id
            if (p / "style.json").exists() and (p / "mesh.glb").exists():
                return p
        return None

    def hair_module_dir(self, kind: str, module_id: str) -> Path | None:
        for r in self.roots:
            p = r / "hair" / "modules" / module_id
            if (p / "mesh.glb").exists():
                return p
        return None

    def hair_style_json(self, style_id: str) -> dict[str, Any]:
        d = self.hair_style_dir(style_id)
        return _read_json(d / "style.json") if d else {}

    # ---- clothing
    def fabric_dirs(self) -> list[Path]:
        return [r / "fabrics" for r in self.roots if (r / "fabrics").is_dir()]

    def fold_dirs(self) -> list[Path]:
        return [r / "folds" for r in self.roots if (r / "folds").is_dir()]

    def compose_kits(self) -> dict[str, Any]:
        """The ``kits`` argument of ``compositor.compose_template``."""
        return {"fabric_dirs": [str(p) for p in self.fabric_dirs()], "fold_dirs": [str(p) for p in self.fold_dirs()],
                "recipe_dirs": [], "kit_dirs": [str(p) for p in self.roots]}

    # ---- face
    def head_base_dir(self, eye_shape: str | None = None) -> Path | None:
        """A validated head-base variant folder (the one for ``eye_shape`` when it exists, else the first), or None."""
        base = self.user_dir / "head_base"
        if not base.is_dir():
            return None
        variants = [d for d in sorted(base.iterdir()) if d.is_dir() and all((d / f).exists() for f in HEAD_BASE_FILES)]
        if not variants:
            return None
        for d in variants:
            if eye_shape and d.name == eye_shape:
                return d
        return variants[0]

    @property
    def head_base_present(self) -> bool:
        return bool(self.flags.get("head_base_present"))

    @property
    def hair_kit_empty(self) -> bool:
        return bool(self.flags.get("hair_kit_empty", True))

    @property
    def kit_manifest_sha(self) -> str:
        return self.inventory.sha256

    def subset_sha(self, **entries: Any) -> str:
        """The sha of only the kit entries a step reads (APP_SPEC §8.5 ``kit_subset_sha``)."""
        return sha256_of(entries)


# ---------------------------------------------------------------------------------------------------- scanning
def _scan_hair(root: Path, rejected: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    styles: dict[str, Any] = {}
    modules: dict[str, dict[str, Any]] = {"fringe": {}, "back": {}}
    hair = root / "hair"
    if not hair.is_dir():
        return styles, modules
    for d in sorted(p for p in hair.iterdir() if p.is_dir() and p.name != "modules"):
        sj = d / "style.json"
        if not sj.exists() or not (d / "mesh.glb").exists():
            continue
        meta = _read_json(sj)
        bad = _origin_problem(meta)
        if bad:
            rejected.append({"kind": "hair", "id": d.name, "reason": bad})
            continue
        styles[d.name] = {**{k: meta[k] for k in ("prompt_phrase", "length_class", "silhouette_class", "clump_k", "parting", "default_fringe",
                                                  "symmetric", "supported_adjustments", "attach_frame", "tris", "license") if k in meta},
                          "origin": meta["origin"], "sha256": folder_sha(d)}
    mods = hair / "modules"
    if mods.is_dir():
        for d in sorted(p for p in mods.iterdir() if p.is_dir()):
            sj = d / "style.json"
            if not sj.exists() or not (d / "mesh.glb").exists():
                continue
            meta = _read_json(sj)
            bad = _origin_problem(meta)
            if bad:
                rejected.append({"kind": "hair_module", "id": d.name, "reason": bad})
                continue
            kind = "back" if meta.get("kind") == "back" else "fringe"
            modules[kind][d.name] = {"prompt_phrase": meta.get("prompt_phrase", ""), "origin": meta["origin"], "sha256": folder_sha(d)}
    return styles, modules


def _scan_fabrics(root: Path, rejected: list[dict[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fab = root / "fabrics"
    if not fab.is_dir():
        return out
    for d in sorted(p for p in fab.iterdir() if p.is_dir()):
        fj = d / "fabric.json"
        if not fj.exists() or not (d / "tile.png").exists():
            continue
        meta = _read_json(fj)
        bad = _origin_problem(meta)
        if bad:
            rejected.append({"kind": "fabric", "id": d.name, "reason": bad})
            continue
        out[d.name] = {"material": meta.get("material_family", "jersey"), "prompt_phrase": meta.get("prompt_phrase", d.name),
                       "pattern_phrase": meta.get("pattern_phrase", "plain weave"), "weave_k": int(meta.get("weave_k", 16)),
                       "origin": meta["origin"], "sha256": folder_sha(d)}
    return out


def _scan_folds(root: Path) -> list[str]:
    fold = root / "folds"
    return sorted(d.name for d in fold.iterdir() if d.is_dir() and (d / "fold.json").exists()) if fold.is_dir() else []


def _head_base_variants(user_dir: Path) -> tuple[list[str], list[str]]:
    """``(validated variants, built-but-not-validated variants)``."""
    base = user_dir / "head_base"
    ok, pending = [], []
    if base.is_dir():
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            gen = [f for f in HEAD_BASE_FILES if f != "studio_validated.json"]
            if all((d / f).exists() for f in gen):
                (ok if (d / "studio_validated.json").exists() else pending).append(d.name)
    return ok, pending


def blender_present(rt: Runtime | None = None, explicit: str = "") -> bool:
    try:
        from duoskin.mesh import blender

        if not explicit and rt is not None:
            explicit = rt.effective_settings().three_d.blender_path or ""
        return blender.find_blender(explicit) is not None
    except Exception:  # noqa: BLE001
        return False


def dreamsim_path(rt: Runtime | None) -> Path | None:
    if rt is None:
        return None
    p = rt.paths.models_dir / "dreamsim.onnx"
    return p if p.exists() else None


def dreamsim_present(rt: Runtime | None) -> bool:
    p = dreamsim_path(rt)
    if p is None:
        return False
    try:
        from duoskin.imaging import similarity

        return similarity.load_dreamsim(p) is not None
    except Exception:  # noqa: BLE001
        return False


def build_manifest(user_dir: Path, *, include_demo: bool = False, blender: bool = False, dreamsim: bool = False,
                   demo_dir: Path = DEMO_DIR) -> dict[str, Any]:
    """Merge the user kits (they win) and, when asked, the demo kits into the manifest dict that
    ``kitenums.inventory_from_manifest`` reads, with the availability flags of §5.3."""
    rejected: list[dict[str, str]] = []
    roots = [user_dir] + ([demo_dir] if include_demo else [])
    hair: dict[str, Any] = {}
    modules: dict[str, dict[str, Any]] = {"fringe": {}, "back": {}}
    fabrics: dict[str, Any] = {}
    folds: list[str] = []
    for root in reversed(roots):                                   # later (lower precedence) first, so earlier roots override
        h, m = _scan_hair(root, rejected)
        hair.update(h)
        for k in ("fringe", "back"):
            modules[k].update(m[k])
        fabrics.update(_scan_fabrics(root, rejected))
        folds = sorted(set(folds) | set(_scan_folds(root)))
    ok, pending = _head_base_variants(user_dir)
    style_dir = user_dir / "style"
    house = sorted(style_dir.glob("house_style_v*.png")) if style_dir.is_dir() else []
    body = (user_dir / "body_base" / "body.fbx").exists() and (user_dir / "body_base" / "studio_validated.json").exists()
    manifest: dict[str, Any] = {
        "version": 1, "schema": "duoskin.kit_manifest/1",
        "hair": hair, "hair_modules": modules, "fabrics": fabrics, "fold_sets": folds,
        "eye_shape_variants": ok, "head_base_built_not_validated": pending,
        "flags": {"hair_kit_empty": not bool(hair), "head_base_present": bool(ok), "body_base_present": bool(body),
                  "house_style_present": bool(house), "makeup": "unavailable", "blender_present": bool(blender),
                  "dreamsim_present": bool(dreamsim)},
        "include_demo": bool(include_demo), "rejected": rejected,
        "fabric_ids": sorted(fabrics), "recraft_face_style_id": None,
    }
    if (user_dir / "style" / "recraft_styles.json").exists():
        try:
            manifest["recraft_face_style_id"] = _read_json(user_dir / "style" / "recraft_styles.json").get("vector_face")
        except (OSError, ValueError):
            pass
    manifest["sha256"] = sha256_of({k: v for k, v in manifest.items() if k != "sha256"})
    return manifest


#: tests switch the demo kits on or off (the empty-kit e2e scenarios run with ``False``); ``None`` follows the demo mode
DEMO_OVERRIDE: bool | None = None


def want_demo(rt: Runtime) -> bool:
    """Demo kits stand in for missing user kits in demo mode (``Settings.demo_mode``) and under all-mock providers."""
    if DEMO_OVERRIDE is not None:
        return DEMO_OVERRIDE
    try:
        return bool(rt.demo)
    except Exception:  # noqa: BLE001
        return False


def write_manifest(rt: Runtime, *, include_demo: bool | None = None) -> dict[str, Any]:
    """Rebuild ``DATA\\kits\\manifest.json`` (what ``python -m duoskin build-kit-manifest`` and the Library page call)."""
    demo = want_demo(rt) if include_demo is None else include_demo
    rt.paths.kits_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(rt.paths.kits_dir, include_demo=demo, blender=blender_present(rt), dreamsim=dreamsim_present(rt))
    from duoskin import winplat

    winplat.atomic_write(rt.paths.kits_dir / "manifest.json", (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return manifest


def load_context(rt: Runtime, *, include_demo: bool | None = None, refresh: bool = True) -> KitContext:
    """The kit context of this run, built fresh from the folders (cheap: a few small files)."""
    demo = want_demo(rt) if include_demo is None else include_demo
    manifest = build_manifest(rt.paths.kits_dir, include_demo=demo, blender=blender_present(rt), dreamsim=dreamsim_present(rt))
    inv = kitenums.inventory_from_manifest(manifest)
    roots = [rt.paths.kits_dir] + ([DEMO_DIR] if demo else [])
    return KitContext(inventory=inv, manifest=manifest, flags=dict(manifest["flags"]), user_dir=rt.paths.kits_dir, demo=demo, roots=roots)


def install_inventory(rt: Runtime, *, include_demo: bool | None = None) -> KitContext:
    """Make the spec's kit enums follow the kits (startup, after a kit was added). Returns the context."""
    ctx = load_context(rt, include_demo=include_demo)
    kitenums.set_default_inventory(ctx.inventory)
    return ctx


def flags_for(rt: Runtime) -> dict[str, bool]:
    """The availability flags (cheap: reads the folders)."""
    return load_context(rt).flags
