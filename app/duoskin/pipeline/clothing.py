"""The shirt and pants lanes: code only, from the spec, the approved prints, the fabric tile and the fold set (APP_SPEC §10.5).

``clothing.compose`` runs the compositor (``imaging.compositor``: a pure function, so the same inputs give byte-identical PNGs and an
unchanged ``approval_hash``) and the template validators (CHK-B01…B08, B11), and puts the board assets on the part:

* ``template``: the 585x559 RGBA 8-bit classic template (what the upload kit ships as ``shirt.png`` / ``pants.png``);
* ``flat_front`` / ``flat_back``: the Gate 2 flat renders; ``preview_boxes``: the 3D box preview; ``label_map``: the garment labels.

A missing fabric tile or fold set falls back to the procedural ones and the tile says so (``procedural_folds``; APP_SPEC S23).
``template.finalize`` (BUILD) re-opens the final PNG and validates it again, so a file damaged between the board and the build is caught.
"""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from duoskin.engine import registry
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict
from duoskin.models.part import Part, PartKind, PartState
from duoskin.pipeline import common, kits, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.clothing")


class ComposeParams(Strict):
    part_id: str


def print_assets(rt: Runtime, project_id: str, part: Part) -> dict[str, str]:
    """``{print part id: final sha}`` of the prints this garment places (their approved or ready board finals)."""
    out = {}
    for pid in part.part_deps:
        pp = rt.repo.find_part(project_id, pid)
        if pp is not None and "final" in pp.board_assets:
            out[pid] = pp.board_assets["final"]
    return out


def skin_rgb(spec: dict[str, Any], character: str, inv: Any) -> tuple[int, int, int]:
    try:
        return common.hex_to_rgb(inv.skin_hex(spec[character]["body"]["skin_tone"]))
    except Exception:  # noqa: BLE001
        return (226, 178, 140)


def compose_part(rt: Runtime, project_id: str, part: Part, spec: dict[str, Any], *, with_layers: bool = False) -> tuple[Any, dict[str, Any]]:
    """Run the compositor for one garment. Returns ``(ComposeResult, info)`` (``info``: prints used, fabric and fold ids, kit subset sha)."""
    from duoskin.imaging import compositor as CP
    from duoskin.imaging import print_place as PP

    kit = kits.load_context(rt)
    kind = "shirt" if part.kind == PartKind.SHIRT else "pants"
    prints = {pid: rt.cas.get(sha) for pid, sha in print_assets(rt, project_id, part).items()}
    ch = spec[part.character]
    req = CP.request_from_character(kind, ch, spec["palette"], prints, None, None, kit.compose_kits(), skin=skin_rgb(spec, part.character, kit.inventory))
    result = CP.compose(req, with_layers=with_layers)
    info = {"prints": sorted(prints), "recipe": req.recipe.recipe_id, "fabric_sha": req.fabric.sha256 if req.fabric else None,
            "fold_sha": req.folds.sha256 if req.folds else None, "fold_origin": req.folds.origin if req.folds else None,
            "kit_subset_sha": kit.subset_sha(recipe=req.recipe.sha256, fabric=req.fabric.sha256 if req.fabric else None,
                                             folds=req.folds.sha256 if req.folds else None),
            "layer_stack_hash": result.layer_stack_hash}
    _ = PP
    return result, info


def label_map_npz(labels: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, labels=labels.astype(np.uint8))
    return buf.getvalue()


def mock_lineage(rt: Runtime, shas: list[str]) -> bool:
    """Does any input come from a mock provider (directly or through its own lineage)? Such outputs are never exportable (CHK-E01)."""
    for sha in shas:
        a = rt.cas.find_asset(sha)
        if a is None:
            continue
        pv = a.first_provenance
        if pv.source == "mock" or "mock_lineage" in pv.notes:
            return True
    return False


def run_compose(ctx: StepContext, p: ComposeParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    ctx.progress(0.1, "compositing")
    result, info = compose_part(rt, project_id, part, spec)
    in_shas = list(print_assets(rt, project_id, part).values())
    in_pix = [rt.cas.get_asset(s).pixel_sha or s for s in in_shas]
    notes = ["mock_lineage"] if mock_lineage(rt, in_shas) else []
    pv = common.prov("code", step_kind="clothing.compose", input_shas=in_pix, notes=notes,
                     params={"recipe": info["recipe"], "fabric": info["fabric_sha"], "folds": info["fold_sha"], "fold_origin": info["fold_origin"],
                             "layer_stack_hash": info["layer_stack_hash"], "kit_subset_sha": info["kit_subset_sha"]})
    roles = {"template": result.png, "flat_front": result.flat_front, "flat_back": result.flat_back, "preview_boxes": result.preview_boxes}
    assets: dict[str, str] = {}
    for role, data in roles.items():
        assets[role] = common.put_bytes(ctx, data, "png", role=role, part_id=part.id, provenance=pv, status="final").sha256
    assets["label_map"] = common.put_bytes(ctx, label_map_npz(result.label_map), "npz", role="label_map", part_id=part.id,
                                           provenance=pv, status="final").sha256
    results = list(result.checks)
    common.store_checks(ctx, results)
    flags = []
    if "procedural folds" in result.warnings or info["fold_origin"] == "procedural":
        flags.append("procedural_folds")
    if notes:
        flags.append("mock")
    blocking = common.hard_failures(results)
    parts.set_tile_facts(rt, project_id, part.id, recipe=info["recipe"], prints=info["prints"], fold_origin=info["fold_origin"])
    if blocking:
        parts.mark_needs_human(ctx, project_id, part.id, report="; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]),
                               assets=assets, results=results)
    else:
        parts.mark_ready(ctx, project_id, part.id, assets=assets, results=results, flags_add=flags)
    return StepResult(outputs=list(assets.values()), result={"template": assets["template"], "hard_failures": [r.check_id for r in blocking],
                                                             "layer_stack_hash": info["layer_stack_hash"]},
                      message="composed")


class ClothingLane(parts.Lane):
    def __init__(self, kind: PartKind) -> None:
        self.kind = kind

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:
        st = ctx.rt.ops.new_step("clothing.compose", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=part.id,
                                 params={"part_id": part.id}, priority=ctx.step.priority)
        ctx.spawn([st])


# ---------------------------------------------------------------------------------------------------- BUILD: template.finalize
def run_template_finalize(ctx: StepContext, p: ComposeParams, inputs: list[Any]) -> StepResult:
    """BUILD: re-open the final Shirt/Pants PNG from the content store and validate it again (CHK-B01…B08, B11; the file the user uploads)."""
    from duoskin.roblox import validators as V

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    sha = part.board_assets.get("template")
    if not sha:
        raise StepFailure("the part has no template file", kind="bad_request", billed="no")
    data = ctx.read_asset(sha)
    labels = None
    if "label_map" in part.board_assets:
        labels = np.load(io.BytesIO(ctx.read_asset(part.board_assets["label_map"])))["labels"]
    kind = "shirt" if part.kind == PartKind.SHIRT else "pants"
    results = V.validate_template(data, kind, labels, None)
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    return StepResult(outputs=[], result={"template": sha, "ok": not blocking, "hard_failures": [r.check_id for r in blocking]},
                      message="template re-validated" if not blocking else "template failed the file gate")


def register(rt: Runtime | None = None) -> None:
    parts.register_lane(ClothingLane(PartKind.SHIRT))
    parts.register_lane(ClothingLane(PartKind.PANTS))
    registry.register_handler("clothing.compose", run_compose, version=1, pool="cpu", paid=False, Params=ComposeParams, cacheable=False)
    registry.register_handler("template.finalize", run_template_finalize, version=1, pool="cpu", paid=False, Params=ComposeParams,
                              cacheable=False)


_ = PartState
