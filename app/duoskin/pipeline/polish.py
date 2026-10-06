"""The hair polish pack (APP_SPEC §10.7 "Polish pack", §11.4): the fitted kit hair, handed to the user for hand polishing in Blender.

``EXPORTS\\PolishPacks\\<duo slug>\\<part id>\\`` holds ``hair_fitted.glb`` (studs, +Z front, Y up; ``hair_fitted.fbx`` and ``hair_fitted.blend`` only with
Blender), the four approved views, ``head_guide.glb`` (a reference only: delete it before saving; ``hair.register`` removes a leftover), ``POLISH.txt`` (the
triangle target, one mesh and one material, do not move the origin, save as ``return\\hair.glb``) and the empty ``return\\`` drop folder. Polishing is
optional: "Skip polish" is the default (the fitted kit hair is the build asset after the same mesh gate), so nothing waits for it. A returned file is
imported like any user-made mesh (``manual_mesh.assign_import`` with the plan "not_tripo": licence ``user_made``).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duoskin.engine import registry
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict, iso_utc, utcnow
from duoskin.models.part import PartKind
from duoskin.pipeline import common, itemspec, kits, manual_mesh

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.polish")

POLISH_TXT = """DuoSkin hair polish pack
========================

What this is: the kit hair that DuoSkin fitted to your character, ready to polish by hand. Polishing is optional.

Rules for the file you send back
  - at most {tris} triangles, ONE mesh and ONE material;
  - do not move the origin (the hair is placed at the head's HairAttachment);
  - keep studs as the unit, +Z front, Y up;
  - delete head_guide.glb (it is only there to show where the head is) before saving;
  - save as return\\hair.glb (or hair.fbx when Blender is installed).

DuoSkin picks the file up from the return folder, repairs, checks and judges it like any other model.
"""


class PolishParams(Strict):
    part_id: str


def pack_dir(rt: Runtime, project_slug: str, part_id: str) -> Path:
    d = manual_mesh.exports_root(rt) / "PolishPacks" / project_slug / common.part_dir_name(part_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_polish_pack(rt: Runtime, project_id: str, part_id: str) -> dict[str, Any]:
    """Write the pack of a built kit-hair tile. Returns the pack state (folder, return dir, files)."""
    from duoskin.mesh import export as MX
    from duoskin.render import avatar

    project = rt.repo.get_project(project_id)
    part = rt.repo.get_part(project_id, part_id)
    if part.kind != PartKind.HAIR or not part.build_assets.get("glb_archive"):
        raise ValueError("a polish pack needs hair that has been built")
    out = pack_dir(rt, project.slug, part_id)
    files: list[str] = []
    (out / "hair_fitted.glb").write_bytes(rt.cas.get(part.build_assets["glb_archive"]))
    files.append("hair_fitted.glb")
    if part.build_assets.get("fbx") and kits.blender_present(rt):
        (out / "hair_fitted.fbx").write_bytes(rt.cas.get(part.build_assets["fbx"]))
        files.append("hair_fitted.fbx")
    for v in ("front", "left", "back", "right"):
        sha = part.board_assets.get(f"view.{v}")
        if sha:
            (out / f"view_{v}.png").write_bytes(rt.cas.get(sha))
            files.append(f"view_{v}.png")
    MX.write_glb(avatar.head_guide_meshdata(), out / "head_guide.glb")
    files.append("head_guide.glb")
    tris = itemspec.TRIS_HAIR
    (out / "POLISH.txt").write_text(POLISH_TXT.format(tris=tris), encoding="ascii")
    files.append("POLISH.txt")
    ret = out / "return"
    ret.mkdir(exist_ok=True)
    state = {"folder": str(out), "return_dir": str(ret), "files": files, "created_at": iso_utc(utcnow()), "project_id": project_id, "part_id": part_id,
             "pack_id": f"DS-{project.slug}-{part_id.replace('.', '-')}-polish", "settings_text": POLISH_TXT.format(tris=tris), "kind": "polish"}
    rt.repo.kv_set(f"polish:{project_id}:{part_id}", state)
    (out / "pack.json").write_text(json.dumps({k: v for k, v in state.items() if k != "settings_text"}, indent=1), encoding="utf-8")
    return state


def run_pack(ctx: StepContext, p: PolishParams, inputs: list[Any]) -> StepResult:
    """``polish.pack``: write the pack and open a MANUAL_IMPORT gate for it (the user may cancel: polishing is optional)."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    state = build_polish_pack(rt, project_id, p.part_id)
    rt.repo.kv_set(f"pack:{project_id}:{p.part_id}", {**state, "gate_id": None, "step_id": None})
    manual_mesh.open_import_gate(ctx, part, state, reason="optional polish of the fitted kit hair")
    return StepResult(result={"folder": state["folder"]}, message="polish pack written")


def start_polish(rt: Runtime, project_id: str, part_id: str) -> None:
    from duoskin.models.job import JobKind

    step = rt.ops.new_step("polish.pack", job_id="", project_id=project_id, part_id=part_id, params=PolishParams(part_id=part_id).model_dump(mode="json"))
    rt.scheduler.submit_job(JobKind.MANUAL_MESH, project_id, {"part_id": part_id, "polish": True}, steps=[step])


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("polish.pack", run_pack, version=1, pool="cpu", paid=False, Params=PolishParams, cacheable=False)
