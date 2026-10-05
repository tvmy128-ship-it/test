"""The library: kits, fabric and fold tiles, the head-base build (APP_SPEC §10.1, §10.6.1, §5.3, §13).

* ``add_kit(rt, folder, kind, origin, license)`` validates a folder the user made (the **Add kit** check of §10.7.1 for hair: at most 3000 triangles, one
  mesh, one material, one UV set, the fixed band values 64/128/192 in ``bands.png``; fabrics: a square greyscale tile) and copies it into ``DATA\\kits\\``
  with its ``origin`` and ``license``; the kit manifest is rebuilt and the spec's kit enums follow it. A kit asset without an allowed ``origin`` is
  refused (POL-08).
* ``library.fabric`` / ``library.fold`` (LIBRARY job, paid): I7 draws a neutral greyscale fabric tile, I8 a shading panel, through the same provider
  routing as every image call; the tile goes into the user kits (``origin = app_generated``) after a seamless-tile check.
* ``kit.build_head``: the head-base build needs Blender and the kit-build script (APP_SPEC §10.6.1); without them the step fails with that reason and
  an existing prebuilt head-base folder can still be added with ``add_kit``.
* ``summary(rt)``: what ``GET /api/library`` shows: the manifest, the availability flags, the registries.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image
from pydantic import Field

from duoskin import winplat
from duoskin.engine import registry
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict
from duoskin.pipeline import common, kits, llmcall

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.library")

KINDS = ("hair", "hair_module", "fabric", "folds", "head_base", "body_base")
LICENSES = ("n/a", "unknown", "user_made", "cc0", "cc_by")
HAIR_TRIS_MAX = 3000
BAND_VALUES = (64, 128, 192)


class KitError(ValueError):
    """The folder is not a usable kit; the message says what to fix."""


def _read_json(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))   # hand-edited kit files may start with a BOM
    except (OSError, ValueError) as exc:
        raise KitError(f"{p.name} cannot be read: {exc}") from exc


def validate_hair(folder: Path, *, module: bool) -> dict[str, Any]:
    """The hair-kit validator: contract files, triangle budget (<= 3000), one mesh and material, bands.png with the fixed band values."""
    from duoskin.mesh import load
    from duoskin.mesh import repair as rep

    meta = _read_json(folder / "style.json")
    if not (folder / "mesh.glb").exists():
        raise KitError("mesh.glb is missing")
    loaded = load.load_gltf(str(folder / "mesh.glb"))
    topo = rep.topology(loaded.mesh)
    if topo["tris"] > HAIR_TRIS_MAX:
        raise KitError(f"mesh.glb has {topo['tris']} triangles; a kit hair style may have at most {HAIR_TRIS_MAX} (so any blend plus modules stays under 3600)")
    bands = folder / "bands.png"
    if bands.exists():
        arr = np.asarray(Image.open(bands).convert("L"))
        found = sorted(int(v) for v in np.unique(arr))
        if found != list(BAND_VALUES):
            raise KitError(f"bands.png must hold exactly the grey values {BAND_VALUES}; it holds {found}")
    elif not module:
        raise KitError("bands.png (the band texture) is missing")
    if not module:
        for need in ("id", "prompt_phrase", "length_class", "silhouette_class", "parting", "supported_adjustments"):
            if need not in meta:
                raise KitError(f"style.json lacks '{need}'")
    return {"id": meta.get("id") or folder.name, "tris": topo["tris"]}


def validate_fabric(folder: Path) -> dict[str, Any]:
    if not (folder / "tile.png").exists():
        raise KitError("tile.png is missing")
    im = Image.open(folder / "tile.png")
    if im.width != im.height:
        raise KitError("the fabric tile must be square")
    meta = _read_json(folder / "fabric.json") if (folder / "fabric.json").exists() else {}
    return {"id": meta.get("id") or folder.name, "size": im.width}


KIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}")
MAX_KIT_FILES = 400
MAX_KIT_BYTES = 200 * 1024 * 1024


def _kit_id(raw: Any) -> str:
    """A kit id becomes a folder name under ``DATA\\kits`` and an enum value: letters, digits, ``-`` and ``_`` only. The id comes from a file in
    the kit (``style.json``, ``fabric.json``), i.e. from whoever made the kit: ``..\\..\\x`` or ``C:\\x`` must never reach a path."""
    text = str(raw or "")
    if not KIT_ID_RE.fullmatch(text) or text.split(".")[0].lower() in winplat._RESERVED_NAMES:
        raise KitError(f"the kit id {text[:40]!r} is not allowed: use letters, digits, - and _ only (at most 61 characters)")
    return text


def _copy_kit_tree(src: Path, dest: Path) -> None:
    """Copy a kit folder into DATA. Only plain files and folders are copied: a link (symlink or junction) would make this copy whatever it points
    at, and a kit may come from anywhere. The number of files and the total size are capped."""
    files = 0
    total = 0
    plan: list[tuple[Path, Path]] = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise KitError(f"{rel.as_posix()} is a link: a kit folder may only hold plain files")
        if winplat.lint_path(rel):
            raise KitError(f"{rel.as_posix()}: " + "; ".join(winplat.lint_path(rel)))
        if path.is_file():
            files += 1
            total += path.stat().st_size
            if files > MAX_KIT_FILES or total > MAX_KIT_BYTES:
                raise KitError(f"the kit is too big (more than {MAX_KIT_FILES} files or {MAX_KIT_BYTES // (1024 * 1024)} MB)")
            plan.append((path, dest / rel))
        elif not path.is_dir():
            raise KitError(f"{rel.as_posix()} is not a plain file or folder")
    dest.mkdir(parents=True, exist_ok=False)
    try:
        for from_, to in plan:
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(from_, to)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def add_kit(rt: Runtime, folder_path: str, kind: str, origin: str, license_: str) -> dict[str, Any]:
    """``POST /api/library/kits``: validate, copy into the user kits with its origin and licence, rebuild the manifest. Returns the manifest summary."""
    from duoskin.security import is_network_path

    if is_network_path(folder_path):
        raise KitError("a kit folder must be on this PC: copy it from the network share first")
    src = Path(folder_path)
    if kind not in KINDS:
        raise KitError(f"kind must be one of {', '.join(KINDS)}")
    if origin not in kits.ORIGINS:
        raise KitError(f"origin must be one of {', '.join(kits.ORIGINS)} (POL-08)")
    if license_ not in LICENSES:
        raise KitError(f"license must be one of {', '.join(LICENSES)}")
    if not src.is_dir():
        raise KitError("the folder does not exist")
    if kind in ("hair", "hair_module"):
        info = validate_hair(src, module=kind == "hair_module")
        info["id"] = _kit_id(info["id"])
        dest = rt.paths.kits_dir / "hair" / ("modules" if kind == "hair_module" else "") / info["id"]
        meta_file = "style.json"
    elif kind == "fabric":
        info = validate_fabric(src)
        info["id"] = _kit_id(info["id"])
        dest = rt.paths.kits_dir / "fabrics" / info["id"]
        meta_file = "fabric.json"
    elif kind == "folds":
        if not (src / "fold.json").exists():
            raise KitError("fold.json is missing")
        info = {"id": _kit_id(src.name)}
        dest = rt.paths.kits_dir / "folds" / info["id"]
        meta_file = "fold.json"
    else:                                                          # head_base / body_base: a prebuilt folder; the manifest checks its generated files
        info = {"id": _kit_id(src.name)}
        dest = rt.paths.kits_dir / ("head_base" if kind == "head_base" else "body_base") / info["id"]
        meta_file = ""
    root = rt.paths.kits_dir.resolve()
    if not dest.resolve().is_relative_to(root):                    # belt and braces: the id check above already makes this impossible
        raise KitError("the kit would be written outside the kits folder")
    if dest.exists():
        raise KitError(f"a kit named {info['id']} already exists")
    _copy_kit_tree(src, dest)
    if meta_file:
        mp = dest / meta_file
        meta = _read_json(mp) if mp.exists() else {"id": info["id"]}
        meta.update({"origin": origin, "license": license_})
        mp.write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
    manifest = kits.write_manifest(rt)
    kits.install_inventory(rt)
    return {"added": info["id"], "kind": kind, "manifest_sha": manifest["sha256"], "flags": manifest["flags"]}


def rebuild_manifest(rt: Runtime) -> dict[str, Any]:
    """``POST /api/library/rebuild-manifest``: rebuild, install the enums, smoke-test that the spec schema still builds with them."""
    manifest = kits.write_manifest(rt)
    ctx = kits.install_inventory(rt)
    ok, why = True, ""
    try:
        from duoskin.models.spec import DuoSpec

        DuoSpec.model_json_schema()
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"{type(exc).__name__}: {exc}"
    return {"manifest_sha": manifest["sha256"], "schema_smoke_test": {"ok": ok, "error": why}, "flags": ctx.flags}


def summary(rt: Runtime) -> dict[str, Any]:
    ctx = kits.load_context(rt)
    m = ctx.manifest
    rows = {t: int(rt.db.conn().execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in ("registry_face", "registry_print")}
    return {"manifest_sha": m["sha256"], "flags": ctx.flags, "demo": ctx.demo, "hair": sorted(m.get("hair", {})),
            "hair_modules": {k: sorted(v) for k, v in m.get("hair_modules", {}).items()}, "fabrics": sorted(m.get("fabrics", {})), "fold_sets": m.get("fold_sets", []),
            "eye_shape_variants": m.get("eye_shape_variants", []), "head_base_built_not_validated": m.get("head_base_built_not_validated", []),
            "rejected": m.get("rejected", []), "registries": rows,
            "labels": {"head_base": "2D preview - no head base" if not ctx.flags.get("head_base_present") else "head base",
                       "hair": "custom hair: no kit" if ctx.flags.get("hair_kit_empty") else "kit hair",
                       "blender": "FBX not produced" if not ctx.flags.get("blender_present") else "Blender found",
                       "dreamsim": "clone check degraded" if not ctx.flags.get("dreamsim_present") else "DreamSim found"}}


# ---------------------------------------------------------------------------------------------------- library steps
class FabricParams(Strict):
    fabric_id: str
    nonce: str = ""


def run_fabric(ctx: StepContext, p: FabricParams, inputs: list[Any]) -> StepResult:
    """``library.fabric`` (I7) and ``library.fold`` (I8) are not part of this build: the prompt compiler needs a locked spec even for these DNA-free
    templates, and the procedural fabrics and folds (APP_SPEC S23) cover every use. The step fails with that reason instead of pretending."""
    raise StepFailure("Drawing a fabric or fold library tile with AI is not available in this build. The procedural fabrics and folds are used instead.",
                      kind="bad_request", billed="no", retryable=False, user_hint="The procedural fabrics and folds are used instead (labelled 'procedural folds').")


def run_head_build(ctx: StepContext, p: Any, inputs: list[Any]) -> StepResult:
    if not kits.blender_present(ctx.rt):
        raise StepFailure("Building a head base needs Blender. Install Blender 4.2 or newer, or add a prebuilt head-base folder with Add kit.", kind="bad_request",
                          billed="no", retryable=False, user_hint="Building a head base needs Blender (APP_SPEC 10.9.1).")
    raise StepFailure("The head-base build script (kit_build.py) is not part of this build.", kind="bad_request", billed="no", retryable=False)


class HeadParams(Strict):
    source_path: str
    variant: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("library.fabric", run_fabric, version=1, pool="api", paid=False, Params=FabricParams, cacheable=False)
    registry.register_handler("library.fold", run_fabric, version=1, pool="api", paid=False, Params=FabricParams, cacheable=False)
    registry.register_handler("kit.build_head", run_head_build, version=1, pool="proc", paid=False, Params=HeadParams, cacheable=False)


_ = (common, llmcall)
