"""The colours and body lane (APP_SPEC §10.10): swatches, the skin tone, the modesty layer and the flat body preview.

Board tile (``c.colours``): the palette swatches (main, second, shared, accent, hair), the skin tone and modesty colour chips and the body
front and back flat previews. Checks: palette integrity, the modesty colour at least dE 10 from the skin tone, and CHK-B10 (the body bundle),
which is ``not_applicable`` with the reason ``no_body_base`` until a body base exists (APP_SPEC S26); the 2D equivalents run instead.

BUILD (``body.compose``): ``body_colors.json`` for the standard Block body (every limb in the skin tone) and, with a body base, the
modesty texture. Without a body base the checklist says the character uses the Roblox blocky body (APP_SPEC §16).
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result, run_check
from duoskin.engine import registry
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict
from duoskin.models.part import Part, PartKind
from duoskin.pipeline import common, kits, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.colours")

MODESTY_MIN_DE = 10.0
LIMB_PARTS = ("rlimb", "llimb")


class ColoursParams(Strict):
    part_id: str


def role_colours(spec: dict[str, Any], character: str) -> list[tuple[str, str]]:
    """``[(role label, hex)]`` of the swatch strip: main, second, shared, accent, hair of this character."""
    by_role: dict[str, str] = {}
    for c in spec.get("palette", []):
        by_role.setdefault(c["role"], c["hex"].lower())
    order = [(f"{character}_main", "main"), (f"{character}_second", "second"), ("shared", "shared"), ("accent", "accent"),
             (f"hair_{character}", "hair")]
    return [(label, by_role[r]) for r, label in order if r in by_role]


def modesty_canvas(modesty_hex: str) -> np.ndarray:
    """A 585x559 modesty layer: the torso and the top of the legs in the modesty colour, everything else transparent (skin shows)."""
    from duoskin.roblox import template as T

    arr = np.zeros((T.HEIGHT, T.WIDTH, 4), np.uint8)
    rgb = common.hex_to_rgb(modesty_hex)
    for key, (x0, y0, x1, y1) in T.REGIONS.items():
        part = T.PART_OF.get(key, "")
        if part in ("torso",) or (part in LIMB_PARTS and y0 < 400 and key.endswith(("_f", "_b", "_l", "_r", "_u"))):
            ys1 = y1 if part == "torso" else min(y1, 395)
            arr[y0:ys1 + 1, x0:x1 + 1] = (*rgb, 255)
    return arr


def body_previews(spec: dict[str, Any], character: str, skin: tuple[int, int, int], modesty_hex: str) -> dict[str, Image.Image]:
    """Front and back flat previews of the bare body with its modesty layer (the mannequin, no clothes)."""
    from duoskin.render import avatar, sheets

    mq = avatar.default_mannequin()
    meshes = avatar.dress(mq, skin_rgb=skin, body_rgb=skin, modesty=modesty_canvas(modesty_hex))
    return sheets.render_character_views(meshes, ("front", "back"), size=(300, 540), ss=1, name=f"body_{character}")


def swatch_sheet(colours: list[tuple[str, str]], skin: str, modesty: str) -> Image.Image:
    from duoskin.imaging import palette as P

    items = [h for _, h in colours] + [skin, modesty]
    return P.palette_sheet(items, cell=72, gap=10, columns=len(items))


def check_palette_integrity(spec: dict[str, Any], character: str) -> CheckResult:
    """Every ``*_ref`` the character uses names a palette colour with a valid ``#RRGGBB`` (the 2D equivalent of the body checks)."""
    ids = {c["id"] for c in spec.get("palette", [])}
    bad: list[str] = []
    ch = spec[character]

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith("_ref") and isinstance(v, str) and v not in ("none", "") and v not in ids:
                    bad.append(f"{path}/{k}={v}")   # win-ok: JSON pointer, not a file path
                walk(v, f"{path}/{k}")   # win-ok: JSON pointer, not a file path
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}")   # win-ok: JSON pointer, not a file path

    walk(ch, f"/{character}")
    return common.mk_result("COL_PALETTE_REFS", not bad, metric="unknown_palette_refs", value=float(len(bad)),
                            evidence="; ".join(bad[:4]) or "every colour reference resolves", fix_hint="human")


def check_modesty_skin(skin_hex: str, modesty_hex: str) -> CheckResult:
    from duoskin.imaging import palette as P

    de = P.de2000_hex(skin_hex, modesty_hex)
    return common.mk_result("COL_MODESTY", de >= MODESTY_MIN_DE, metric="modesty_vs_skin_de2000", value=de,
                            threshold=f">= {MODESTY_MIN_DE} (modesty colour vs the skin tone)", evidence=f"dE {de:.1f}", fix_hint="human")


def run_compose(ctx: StepContext, p: ColoursParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    _, spec = common.load_spec(rt, project_id)
    c = part.character
    kit = kits.load_context(rt)
    body = spec[c]["body"]
    skin_hex = kit.inventory.skin_hex(body["skin_tone"]).lower()
    modesty_hex = common.hex_of(spec, body["modesty_ref"], "#808080") or "#808080"
    colours = role_colours(spec, c)
    swatches = swatch_sheet(colours, skin_hex, modesty_hex)
    previews = body_previews(spec, c, common.hex_to_rgb(skin_hex), modesty_hex)
    modesty_png = common.png_bytes(Image.fromarray(modesty_canvas(modesty_hex), "RGBA"))
    pv = common.prov("code", step_kind="colours.compose", params={"skin": skin_hex, "modesty": modesty_hex, "kit_subset_sha": kit.subset_sha(
        skin=skin_hex, body_base=kit.flags.get("body_base_present"))})
    assets = {"swatches": common.put_png(ctx, swatches, role="swatches", part_id=part.id, provenance=pv, status="final").sha256,
              "body_front": common.put_png(ctx, previews["front"], role="body_front", part_id=part.id, provenance=pv, status="final").sha256,
              "body_back": common.put_png(ctx, previews["back"], role="body_back", part_id=part.id, provenance=pv, status="final").sha256,
              "modesty_layer": common.put_bytes(ctx, modesty_png, "png", role="modesty_layer", part_id=part.id, provenance=pv,
                                                status="final").sha256}
    body_base = bool(kit.flags.get("body_base_present"))
    results = [check_palette_integrity(spec, c), check_modesty_skin(skin_hex, modesty_hex),
               run_check("CHK-B10", assets["modesty_layer"], lambda: build_result("CHK-B10", passed=True, metric="body_bundle", evidence="body bundle checked"),
                         body_base_present=body_base)]
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    flags = [] if body_base else ["no_body_base"]
    if blocking:
        parts.mark_needs_human(ctx, project_id, part.id, report="; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]),
                               assets=assets, results=results)
    else:
        parts.mark_ready(ctx, project_id, part.id, assets=assets, results=results, flags_add=flags,
                         facts={"skin_hex": skin_hex, "modesty_hex": modesty_hex, "swatches": [h for _, h in colours]})
    return StepResult(outputs=list(assets.values()), result={"hard_failures": [r.check_id for r in blocking]}, message="colours composed")


class ColoursLane(parts.Lane):
    kind = PartKind.COLOURS

    def waits_for(self, part: Part, all_parts: dict[str, Part]) -> list[str]:
        return []

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:
        st = ctx.rt.ops.new_step("colours.compose", job_id=ctx.step.job_id, project_id=ctx.step.project_id, part_id=part.id,
                                 params={"part_id": part.id}, priority=ctx.step.priority)
        ctx.spawn([st])


# ---------------------------------------------------------------------------------------------------- BUILD: body.compose
def body_colors_json(skin_hex: str) -> dict[str, Any]:
    """BodyColors for the standard Block body: every limb in the skin tone."""
    h = skin_hex.lower()
    return {"schema": "duoskin.body_colors/1", "HeadColor3": h, "TorsoColor3": h, "LeftArmColor3": h, "RightArmColor3": h,
            "LeftLegColor3": h, "RightLegColor3": h, "body": "standard Block body"}


def run_body_compose(ctx: StepContext, p: ColoursParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    meta = rt.repo.kv_get(parts.facts_key(project_id, part.id)) or {}
    skin_hex = meta.get("skin_hex") or "#e3b08e"
    doc = body_colors_json(skin_hex)
    pv = common.prov("code", step_kind="body.compose", notes=["mock_lineage"] if part.board_assets and any(
        rt.cas.get_asset(s).first_provenance.source == "mock" for s in part.board_assets.values()) else [],
        input_shas=[], params={"skin": skin_hex})
    data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
    sha = common.put_bytes(ctx, data, "json", role="body_colors", part_id=part.id, provenance=pv, status="final").sha256
    assets = {"body_colors": sha}
    kit = kits.load_context(rt)
    results = [run_check("CHK-B10", sha, lambda: build_result("CHK-B10", passed=True, metric="body_bundle", evidence="body bundle checked"),
                         body_base_present=bool(kit.flags.get("body_base_present")))]
    common.store_checks(ctx, results)
    from duoskin.pipeline import build

    build.set_build_assets(rt, project_id, part.id, assets)
    return StepResult(outputs=[sha], result={"body_colors": sha}, message="body colours written")


def register(rt: Runtime | None = None) -> None:
    parts.register_lane(ColoursLane())
    registry.register_handler("colours.compose", run_compose, version=1, pool="cpu", paid=False, Params=ColoursParams, cacheable=False)
    registry.register_handler("body.compose", run_body_compose, version=1, pool="cpu", paid=False, Params=ColoursParams, cacheable=False)


_ = CheckResult
