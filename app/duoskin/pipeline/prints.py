"""The print lane: prints, shoe motifs and (with ``kind="badge"``) badge artwork, through the asset loop (APP_SPEC §10.4).

A print part (``a.print.top.0``) is generated alone: I2 (GPT, the default; I2.print_frame while no concept crop exists, I2.shoe_decal for a
shoe motif) with R2 (Recraft vector) as the technique-ladder rung. The loop's code checks run at the **placed** size (the compositor's slot
scale), the tile shows the graphic alone plus its 100 px readability preview, and the approved final is what the compositor places.

Board assets: ``final`` (RGBA print), ``preview100``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from duoskin.models.part import Part, PartKind
from duoskin.pipeline import assetloop as AL
from duoskin.pipeline import common, kits, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.prints")

TALL_REGIONS = ("torso_l", "torso_r", "rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "llimb_f", "llimb_b", "llimb_l", "llimb_r")
SCALE_FRACTION = {"small": 0.40, "medium": 0.68, "large": 1.0}


def print_source(spec: dict[str, Any], part_id: str) -> dict[str, Any]:
    """What the spec says about one print part: its slot, motif, region, scale and colour refs."""
    ref = common.split_part_id(part_id)
    ch = spec[ref.character]
    if ref.slot == "shoes":
        sh = ch["bottom"]["shoes"]
        return {"slot": "shoes", "motif": sh.get("motif", ""), "region": "rlimb_r", "scale": "small",
                "refs": [sh.get("accent_ref"), sh.get("base_ref")], "print": "shoes"}
    group = "top" if ref.slot == "top" else "bottom"
    p = ch[group]["prints"][ref.index or 0]
    return {"slot": group, "motif": p["motif"], "region": p["region"], "scale": p["scale"], "refs": list(p["colour_refs"]),
            "print": f"{group}.{ref.index or 0}"}


def placed_scale(region: str, scale: str, pants: bool) -> float:
    """Placed width of the print in template pixels over its working width (1024): the A_STROKE scale (APP_SPEC §10.4)."""
    try:
        from duoskin.imaging import print_place as PP

        x0, _, x1, _ = PP.default_slot(region, pants)
        return max(0.01, min(1.0, (x1 - x0) * SCALE_FRACTION.get(scale, 0.68) / 1024.0))
    except Exception:  # noqa: BLE001
        return 0.1


def palette_for_print(spec: dict[str, Any], refs: list[str | None]) -> list[str]:
    pal = common.palette_map(spec)
    own = [pal[r] for r in refs if r in pal]
    extra = [c["hex"].lower() for c in spec.get("palette", []) if c.get("role") in ("line", "neutral_dark", "neutral_light")]
    return list(dict.fromkeys(own + extra))


def loop_checks(template_id: str) -> tuple[list[str], list[str], list[str]]:
    """``(gate_a ids, gate B hard rules, gate B soft rules)`` from the template front matter."""
    from duoskin.checks import gate_b
    from duoskin.prompts import registry as preg

    checks = preg.get(template_id).meta.checks
    rules = list(checks.get("gate_b", []))
    hard = [r for r in rules if gate_b.is_hard(r)]
    soft = [r for r in rules if not gate_b.is_hard(r)]
    return list(checks.get("gate_a", [])), hard, soft


class PrintLane(parts.Lane):
    kind = PartKind.PRINT

    def waits_for(self, part: Part, all_parts: dict[str, Part]) -> list[str]:
        return []

    def start(self, ctx: StepContext, part: Part, spec: dict[str, Any], *, nonce: str, target: str | None) -> None:
        rt = ctx.rt
        project_id = ctx.step.project_id or ""
        src = print_source(spec, part.id)
        refs = common.concept_refs(rt, project_id, spec, part.id)
        shoes = src["slot"] == "shoes"
        tall = src["region"] in TALL_REGIONS
        aspect = "tall" if tall else "square"
        placeholder = refs.placeholder_crop
        if shoes:
            template, ladder = "I2.shoe_decal", "shoe_print"
            images = [refs.crop_sha, refs.style_sha]
            slots = {"print": "shoes"}
            mask = None
        elif placeholder:
            from duoskin.imaging import guides

            frame = guides.guide_frame(aspect)                                    # type: ignore[arg-type]
            frame_sha = rt.cas.put(frame.image_png(), "png", prov=common.prov("code", params={"guide": frame.guide_id})).sha256
            mask = rt.cas.put(frame.mask_png(), "png", prov=common.prov("code", params={"guide": frame.guide_id, "mask": True})).sha256
            template, ladder = "I2.print_frame", "print_frame"
            images = [frame_sha, refs.style_sha]
            slots = {"print": src["print"], "aspect": aspect}
        else:
            template, ladder = "I2.print", "print"
            images = [refs.crop_sha, refs.style_sha]
            slots = {"print": src["print"], "aspect": aspect}
            mask = None
        gate_a, hard, soft = loop_checks(template)
        pants = src["slot"] in ("bottom", "shoes")
        loop = AL.AssetLoopSpec(
            part_id=part.id, character=part.character, role="print", template_id=template, slots=slots,   # type: ignore[arg-type]
            images=[i for i in images if i], mask=mask, n_drafts=4, finalize=True, gate_a=gate_a, gate_b_hard=hard, gate_b_soft=soft,
            technique_ladder=ladder, then="print", palette_hex=palette_for_print(spec, src["refs"]),
            expected={"components": [1, 4], "placed_scale": placed_scale(src["region"], src["scale"], pants), "margin": 0.04,
                      "symmetric": False},
            gate_b_slots={"pr_motif_matches": {"motif": src["motif"]}}, background="transparent",
            reference_shas=[refs.crop_sha] if refs.crop_sha and not placeholder else [], nonce=nonce,
            kit_subset_sha=kits.load_context(rt).subset_sha(print=src["print"]),
            available=sorted(common.available_providers(rt)))
        if refs.flags:
            parts.set_part_state(rt, project_id, part.id, part.state, flags_add=refs.flags)
        AL.start_loop(rt, ctx.step.job_id, project_id, spec, loop, nonce=nonce, priority=ctx.step.priority)


def print_done(ctx: StepContext, loop: AL.AssetLoopSpec, res: AL.LoopResult) -> None:
    """The loop of one print finished: board assets, registry check, tile."""
    from duoskin.imaging import guides

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    if res.needs_human or not res.final_sha:
        parts.mark_needs_human(ctx, project_id, loop.part_id, report=res.report, best=res.best_sha,
                               assets={"final": res.best_sha} if res.best_sha else None, results=res.results)
        return
    im = common.open_image(ctx.read_asset(res.final_sha))
    prev = guides.readability_preview(im, long_edge=100)
    prov = common.prov("code", params={"preview": "readability100"}, input_shas=[ctx.get_asset(res.final_sha).pixel_sha or res.final_sha])
    prev_sha = common.put_png(ctx, prev, role="preview100", part_id=loop.part_id, provenance=prov).sha256
    results = list(res.results)
    results += registry_results(rt, project_id, "print", im)
    ids = common.store_checks(ctx, [r for r in results if r.check_id == "A_REGISTRY"])
    _ = ids
    blocking = [r for r in results if common.hard_failures([r])]
    if blocking and any(r.check_id == "A_REGISTRY" for r in blocking):
        parts.mark_needs_human(ctx, project_id, loop.part_id, report="this print is already registered (exact reuse)", best=res.final_sha,
                               assets={"final": res.final_sha, "preview100": prev_sha}, results=results)
        return
    alts = [{"final": a} for a in res.alternatives]
    rt.repo.mutate_part(project_id, loop.part_id, lambda p: setattr(p, "alternatives", alts))
    for role, sha in (("final", res.final_sha),):
        common.link_asset(rt, project_id, loop.part_id, sha, f"board.{role}", "final",
                          common.prov("code", params={"role": role}), step_id=ctx.step.id)
    parts.mark_ready(ctx, project_id, loop.part_id, assets={"final": res.final_sha, "preview100": prev_sha}, results=results,
                     flags_add=res.flags, facts={"technique": res.technique})


def registry_results(rt: Runtime, project_id: str, kind: str, im: Any) -> list[Any]:
    """A_REGISTRY: exact reuse is blocked forever; near-duplicates within the window warn (APP_SPEC §3.7). Empty registry: passes."""
    try:
        from duoskin.imaging import similarity
        from duoskin.pipeline import registries

        rows = registries.rows(rt, kind, exclude_project=project_id)
        return [similarity.registry_check(kind, im, rows, current_duo_seq=registries.current_seq(rt))]
    except Exception as exc:  # noqa: BLE001 - fail closed
        from duoskin.checks.runner import fail_closed

        return [fail_closed("A_REGISTRY", f"{type(exc).__name__}: {exc}")]


def register(rt: Runtime | None = None) -> None:
    parts.register_lane(PrintLane())
    AL.register_callback("print", print_done)
