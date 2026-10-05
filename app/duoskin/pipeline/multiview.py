"""Multiview images for hair and accessories (APP_SPEC §10.7, §10.8, §11; bible T1, T2, I10, A_VIEWS).

One approved front view becomes four views (front, left, back, right):

* **Tripo** (``tripo.multiview``, T1): the front is re-padded to 80-85% of 2048 px, uploaded just before the request (a free call), and
  ``image-to-multiview`` runs as a remote step (``set_remote_ref`` before ``Pending``, ``poll`` afterwards). The four views are downloaded
  at once (host allowlist, magic bytes) and kept as they are: Tripo's own views are never re-cropped.
* **GPT views** (I10, flagged ``views_from_gpt``, lower reliability): without a Tripo key, or as the last resort after one T2 edit, the back
  view and then the two side views are drawn with the asset loop from the approved front.

``mv.check`` runs A_VIEWS (heights, ground line, centring, margins), the direction rules of the Gate B (``mv_view_direction``, ``mv_same_object``,
``mv_back_plausible``) and, when a view fails them, **one** T2 edit per set (``tripo.edit_view``) before the I10 views take over.

A chain ends in a *board* step (``hair.board`` or ``acc.board``) that reads the checked views and makes the tile.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image

from duoskin.engine import registry
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import Pending, StepResult
from duoskin.models.common import Strict
from duoskin.pipeline import assetloop as AL
from duoskin.pipeline import common, kits

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.multiview")

VIEWS = ("front", "left", "back", "right")
SIDE_VIEWS = ("back", "left", "right")
PAD_PX = 2048
FILL = 0.825
MOCK_POLL_S = 0.05
FACES_TO = {"left": "left", "right": "right"}      # the subject's own left side shows its front pointing to the image's left edge (bible T1)


class MvParams(Strict):
    part_id: str
    target: str = "hair"                  # "hair" | "acc:<i>"
    front_sha: str
    nonce: str = ""
    board: str = ""                       # the step kind that makes the tile after the check
    mode: str = "tripo"                   # tripo | gpt


class EditParams(Strict):
    part_id: str
    target: str = "hair"
    front_sha: str
    mv_task_id: str
    view: str
    fix_sentence: str
    nonce: str = ""
    board: str = ""
    from_step: str = ""                   # the mv.check that asked for the edit


class CheckParams(Strict):
    part_id: str
    target: str = "hair"
    front_sha: str
    nonce: str = ""
    board: str = ""
    from_step: str = ""                   # tripo.multiview / tripo.edit_view step whose result holds the views (Tripo mode)
    mode: str = "tripo"
    t2_done: bool = False
    gpt_done: bool = False
    mv_task_id: str = ""


# ---------------------------------------------------------------------------------------------------- helpers
def tripo_available(rt: Runtime) -> bool:
    return common.provider_available(rt, "tripo")


def repad(png: bytes, size: int = PAD_PX, fill: float = FILL) -> bytes:
    """The Tripo input: the subject re-padded onto a transparent 2048 px canvas at 80-85% of its long side."""
    from duoskin.imaging import checks as C

    return common.png_bytes(C.canonical_frame(common.open_image(png), (size, size), fill=fill))


def subject_alpha(im: Image.Image, tol: int = 18) -> Image.Image:
    """The view with a subject alpha for the checks: its own alpha when it has one, else the flat backdrop (the border colour) is keyed out.
    Only the check sees this copy; the stored view is untouched (Tripo's own views are never re-cropped)."""
    from scipy import ndimage

    rgba = np.asarray(im.convert("RGBA")).copy()
    if rgba[..., 3].min() < 250:
        return Image.fromarray(rgba, "RGBA")
    border = np.concatenate([rgba[0, :, :3], rgba[-1, :, :3], rgba[:, 0, :3], rgba[:, -1, :3]])
    bg = np.median(border, axis=0)
    fg = np.abs(rgba[..., :3].astype(float) - bg).max(axis=2) > tol
    fg = ndimage.binary_fill_holes(ndimage.binary_opening(fg, iterations=1))
    lab, n = ndimage.label(fg)
    if n > 1:
        sizes = ndimage.sum(fg, lab, range(1, n + 1))
        fg = np.isin(lab, [i + 1 for i, sz in enumerate(sizes) if sz >= 0.05 * sizes.max()])
    rgba[..., 3] = np.where(fg, 255, 0)
    return Image.fromarray(rgba, "RGBA")


def upload_name(part_id: str, view: str = "front") -> str:
    return f"{common.part_dir_name(part_id)}_{view}.png"


def _delay(rt: Runtime) -> float | None:
    return MOCK_POLL_S if common.is_mock(rt, "tripo") else None


def estimate_t1(p: Any) -> float:
    from duoskin.providers import pricing

    return float(pricing.estimate_tripo("image_to_multiview").usd)


def estimate_t2(p: Any) -> float:
    from duoskin.providers import pricing

    return float(pricing.estimate_tripo("edit_multiview", views=1).usd)


def _submit(ctx: StepContext, adapter: Any, op: str, *args: Any, **kw: Any) -> str:
    """Create a Tripo task. ``ctx.set_remote_ref`` runs inside the adapter right after Tripo answers, so a crash cannot lead to a second submit.
    A ``submission_uncertain`` answer is reconciled against ``/account/usage`` before anything is resent (ENG-06)."""
    from duoskin.providers.base import ProviderError

    call = getattr(adapter, op)
    cc = ctx.call_ctx()
    import dataclasses

    cc = dataclasses.replace(cc, set_remote_ref=ctx.set_remote_ref, tag=kw.pop("tag", ""))
    try:
        return call(*args, ctx=cc, **kw)
    except ProviderError as exc:
        if exc.kind != "submission_uncertain":
            raise
        c = exc.context or {}
        found = adapter.reconcile_uncertain(endpoint=c.get("endpoint", ""), submitted_at=c.get("submitted_at"), body=c.get("body") or {}, op=op)
        if found:
            ctx.set_remote_ref(found)
            return found
        raise StepFailure("Tripo may not have received the request; it will be sent again", kind="provider", retryable=True, billed="unknown") from exc


def store_views(ctx: StepContext, adapter: Any, task_id: str, status: Any, part_id: str, *, step_kind: str, front_sha: str,
                nonce: str = "") -> dict[str, str]:
    """Download the finished task's views at once and keep them (``view.<name>`` assets of the part)."""
    keys = [k for k in status.output_urls if k.endswith("_view_url")]
    files = adapter.download_files(task_id, keys, ctx=ctx.call_ctx(), status=status) if hasattr(adapter, "download_files") else {}
    pv_base = common.prov("mock" if common.is_mock(ctx.rt, "tripo") else "tripo", provider="tripo", step_kind=step_kind, request_id=task_id,
                          input_shas=[ctx.get_asset(front_sha).pixel_sha or front_sha], nonce=nonce, params={"task_id": task_id})
    out: dict[str, str] = {}
    for k, f in files.items():
        view = k[: -len("_view_url")]
        if view not in VIEWS:
            continue
        out[view] = common.put_bytes(ctx, f.data, "png", role=f"view.{view}", part_id=part_id, provenance=pv_base.model_copy(update={
            "params": {**pv_base.params, "view": view}}), status="candidate").sha256
    return out


def remember_task(rt: Runtime, project_id: str, part_id: str, mv: dict[str, Any]) -> None:
    """The multiview task of a tile (T1 or the T2 edit of it): the BUILD step ``tripo.model`` reuses it as its input."""
    rt.repo.kv_set(f"mvtask:{project_id}:{part_id}", {"task_id": mv.get("task_id") or "", "source": mv.get("source") or ""})


def _record_task_cost(ctx: StepContext, adapter: Any, status: Any, op: str) -> None:
    try:
        common.record_cost(ctx, adapter.cost_for(status), op, fallback_provider="tripo")
    except Exception:  # noqa: BLE001 - a ledger row that cannot be built must not lose the views
        log.warning("could not record the cost of %s", op, exc_info=True)


# ---------------------------------------------------------------------------------------------------- T1: tripo.multiview
def run_multiview(ctx: StepContext, p: MvParams, inputs: list[Any]) -> StepResult | Pending:
    if ctx.step.remote_ref:                                   # never submit twice
        return poll_multiview(ctx, p, ctx.step.remote_ref)
    adapter = ctx.provider("tripo")
    ctx.progress(0.05, "preparing the front view for Tripo")
    png = repad(ctx.read_asset(p.front_sha))
    token = adapter.upload(png, name=upload_name(p.part_id), ctx=ctx.call_ctx())
    adapter.ensure_credits(10.0)
    task = _submit(ctx, adapter, "image_to_multiview", token, tag="T1")
    return Pending(delay_s=_delay(ctx.rt), message=f"Tripo is drawing the views ({task[:8]})")


def poll_multiview(ctx: StepContext, p: MvParams, ref: str) -> StepResult | Pending:
    adapter = ctx.provider("tripo")
    st = adapter.task(ref, ctx=ctx.call_ctx())
    if not st.done:
        return Pending(delay_s=_delay(ctx.rt), message=f"Tripo {st.status}", progress=(st.progress or 0) / 100.0)
    err = st.failure_error()
    if err is not None:
        raise err
    views = store_views(ctx, adapter, ref, st, p.part_id, step_kind="tripo.multiview", front_sha=p.front_sha, nonce=p.nonce)
    _record_task_cost(ctx, adapter, st, "tripo.multiview")
    return StepResult(outputs=list(views.values()), result={"task_id": ref, "views": views, "source": "tripo"}, message="views downloaded")


# ---------------------------------------------------------------------------------------------------- T2: tripo.edit_view
def run_edit_view(ctx: StepContext, p: EditParams, inputs: list[Any]) -> StepResult | Pending:
    if ctx.step.remote_ref:
        return poll_edit_view(ctx, p, ctx.step.remote_ref)
    adapter = ctx.provider("tripo")
    from duoskin.prompts import compiler
    from duoskin.prompts.catalog import default_ctx

    _, spec = common.load_spec(ctx.rt, ctx.step.project_id or "")
    ref = common.split_part_id(p.part_id)
    cp = compiler.compile("T2.edit_view", spec, ref.character,        # type: ignore[arg-type]
                          {"view": p.view, "target": p.target, "fix_sentence": p.fix_sentence}, default_ctx(kits.load_context(ctx.rt).inventory))
    adapter.ensure_credits(5.0)
    _submit(ctx, adapter, "edit_multiview", p.mv_task_id, {p.view: cp.text}, tag="T2")
    return Pending(delay_s=_delay(ctx.rt), message="Tripo is editing one view")


def poll_edit_view(ctx: StepContext, p: EditParams, ref: str) -> StepResult | Pending:
    adapter = ctx.provider("tripo")
    st = adapter.task(ref, ctx=ctx.call_ctx())
    if not st.done:
        return Pending(delay_s=_delay(ctx.rt), message=f"Tripo {st.status}")
    err = st.failure_error()
    if err is not None:
        raise err
    views = store_views(ctx, adapter, ref, st, p.part_id, step_kind="tripo.edit_view", front_sha=p.front_sha, nonce=p.nonce)
    _record_task_cost(ctx, adapter, st, "tripo.edit_view")
    return StepResult(outputs=list(views.values()), result={"task_id": ref, "views": views, "source": "tripo", "edited_view": p.view},
                      message="edited views downloaded")


# ---------------------------------------------------------------------------------------------------- the chain
def start_views(ctx: StepContext, part: Any, spec: dict[str, Any], *, target: str, front_sha: str, board: str, nonce: str) -> None:
    """Spawn the multiview chain of one hair or accessory: T1 (or I10 without a Tripo key) -> ``mv.check`` -> ``board``."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    if tripo_available(rt):
        mv = rt.ops.new_step("tripo.multiview", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id,
                             params=MvParams(part_id=part.id, target=target, front_sha=front_sha, nonce=nonce, board=board).model_dump(mode="json"),
                             nonce=nonce, priority=ctx.step.priority)
        chk = rt.ops.new_step("mv.check", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id, deps=[mv.id],
                              params=CheckParams(part_id=part.id, target=target, front_sha=front_sha, nonce=nonce, board=board,
                                                 from_step=mv.id).model_dump(mode="json"), priority=ctx.step.priority)
        ctx.spawn([mv, chk])
        return
    start_gpt_views(ctx, part, spec, target=target, front_sha=front_sha, board=board, nonce=nonce)


def view_loop(rt: Runtime, project_id: str, spec: dict[str, Any], part: Any, *, view: str, target: str, front_sha: str, back_sha: str | None,
              board: str, nonce: str) -> AL.AssetLoopSpec:
    from duoskin.checks import gate_b
    from duoskin.pipeline.prints import loop_checks

    gate_a, hard_rules, soft_rules = loop_checks("I10.side_view")
    images = [front_sha] + ([back_sha] if back_sha and view != "back" else [])
    if len(images) < 2:                                   # the template lists a back reference too: the front stands in for it on the back view
        images = [front_sha, front_sha]
    slots_b = {"mv_view_direction": {"expected": FACES_TO.get(view, "left"), "view": view}}
    rules = [*hard_rules, *soft_rules]
    return AL.AssetLoopSpec(
        part_id=part.id, character=part.character, role=f"view_{view}", template_id="I10.side_view", slots={"view": view, "target": target},   # type: ignore[arg-type]
        images=images, n_drafts=3, finalize=False, gate_a=[g for g in gate_a if g != "A_VIEWS"],
        gate_b_hard=[r for r in rules if gate_b.is_hard(r)], gate_b_soft=[r for r in rules if not gate_b.is_hard(r)], technique_ladder="view",
        then="view", then_params={"view": view, "target": target, "front_sha": front_sha, "board": board}, palette_hex=[],
        expected={"components": [1, 6], "margin": 0.04, "symmetric": False, "allow_checker": False}, gate_b_slots=slots_b,
        reference_shas=[front_sha], nonce=nonce, available=sorted(common.available_providers(rt)))


def start_gpt_views(ctx: StepContext, part: Any, spec: dict[str, Any], *, target: str, front_sha: str, board: str, nonce: str) -> None:
    """I10: the back view first, then both side views with the back as their second reference (flagged ``views_from_gpt``)."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    with rt.db.tx():
        rt.repo.kv_set(views_key(project_id, part.id), {"views": {}, "pending": list(SIDE_VIEWS), "nonce": nonce, "board": board, "target": target,
                                                        "front_sha": front_sha, "checking": False})
    from duoskin.pipeline import parts as P

    P.set_part_state(rt, project_id, part.id, part.state, flags_add=["views_from_gpt"])
    loop = view_loop(rt, project_id, spec, part, view="back", target=target, front_sha=front_sha, back_sha=None, board=board, nonce=nonce)
    AL.start_loop(rt, ctx.step.job_id, project_id, spec, loop, nonce=nonce, priority=ctx.step.priority)


def views_key(project_id: str, part_id: str) -> str:
    return f"views:{project_id}:{part_id}"


def view_done(ctx: StepContext, loop: AL.AssetLoopSpec, res: AL.LoopResult) -> None:
    """An I10 view finished. The back view starts both side views; the last view starts ``mv.check``."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    view = str(loop.then_params["view"])
    part = rt.repo.get_part(project_id, loop.part_id)
    sha = res.final_sha or res.best_sha
    go_sides = go_check = False
    with rt.db.tx():
        st = dict(rt.repo.kv_get(views_key(project_id, part.id)) or {})
        if not st:
            return
        if sha is None:                                    # the view could not be drawn: the check fails on the missing view
            st.setdefault("missing", []).append(view)
        else:
            st["views"][view] = sha
        st["pending"] = [v for v in st.get("pending", []) if v != view]
        if view == "back" and not st.get("sides_started"):
            st["sides_started"] = True
            st["pending"] = ["left", "right"]
            go_sides = True
        elif not st["pending"] and not st.get("checking") and view != "back":
            st["checking"] = True
            go_check = True
        rt.repo.kv_set(views_key(project_id, part.id), st)
    _, spec = common.load_spec(rt, project_id)
    if go_sides:
        back = st["views"].get("back")
        for v in ("left", "right"):
            lp = view_loop(rt, project_id, spec, part, view=v, target=st["target"], front_sha=st["front_sha"], back_sha=back, board=st["board"],
                           nonce=st["nonce"])
            AL.start_loop(rt, ctx.step.job_id, project_id, spec, lp, nonce=st["nonce"], priority=ctx.step.priority)
    if go_check:
        chk = rt.ops.new_step("mv.check", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id,
                              params=CheckParams(part_id=part.id, target=st["target"], front_sha=st["front_sha"], nonce=st["nonce"], board=st["board"],
                                                 mode="gpt", gpt_done=True).model_dump(mode="json"), priority=ctx.step.priority)
        ctx.spawn([chk])


# ---------------------------------------------------------------------------------------------------- mv.check
def _views_of(ctx: StepContext, p: CheckParams) -> tuple[dict[str, str], str, str]:
    """``(views, source, mv task id)``: Tripo's own four views, or our front plus the I10 views."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    if p.from_step:
        s = rt.repo.get_step(p.from_step)
        res = s.result
        views = dict(res.get("views") or {})
        if s.kind == "tripo.edit_view":                    # an edit returns the four views of the edited task
            pass
        return views, "tripo", str(res.get("task_id") or p.mv_task_id)
    st = rt.repo.kv_get(views_key(project_id, p.part_id)) or {}
    views = {"front": p.front_sha, **dict(st.get("views") or {})}
    return views, "gpt", ""


def run_mv_check(ctx: StepContext, p: CheckParams, inputs: list[Any]) -> StepResult:
    """A_VIEWS plus the direction rules. A failing set gets one T2 edit (Tripo views) and then the flagged GPT views; then a human look."""
    from duoskin.imaging import checks as C

    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    views, source, task_id = _views_of(ctx, p)
    ims = {v: common.open_image(ctx.read_asset(s)) for v, s in views.items() if s}
    ctx.progress(0.2, "checking the views")
    from duoskin.checks import thresholds

    # Tripo's own views are never re-cropped, so their margin (about 6% by their own framing) is not ours to fix: only a view that touches the
    # border counts as cropped. Our own (GPT) views keep the full 6%.
    with thresholds.overrides({"img.margin_min": 0.04} if source == "tripo" else {}):
        a_views = C.check_views({v: subject_alpha(im) for v, im in ims.items()}, subject_sha=views.get("front", ""))
    results = [a_views]
    rules = ["mv_same_object", "mv_view_direction", "mv_back_plausible"]
    if not common.provider_available(rt, "anthropic") or not all(v in ims for v in VIEWS):
        pass
    else:
        from duoskin.pipeline import llmcall

        counter = llmcall.CallCounter()
        sheet = _sheet(ims)
        for view in ("left", "right"):
            reqs = llmcall.rule_requests(["mv_view_direction"], {"mv_view_direction": {"expected": FACES_TO[view], "view": view}})
            res = llmcall.run_rules(ctx, reqs, [sheet], measured_facts=f"views shown left to right: {', '.join(VIEWS)}", subject_sha=views[view],
                                    counter=counter)
            results += list(res.results)
        reqs = llmcall.rule_requests([r for r in rules if r != "mv_view_direction"], {})
        res = llmcall.run_rules(ctx, reqs, [sheet], measured_facts=f"views shown left to right: {', '.join(VIEWS)}", subject_sha=views["front"],
                                counter=counter)
        results += list(res.results)
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    flags = ["views_from_gpt"] if source == "gpt" else []
    if blocking:
        if source == "tripo" and not p.t2_done:                # T2 once per set
            view = _worst_view(blocking, ims)
            fix = "Turn the object so its front faces the " + FACES_TO.get(view, "left") + " side." if view in FACES_TO else "Show the plain back of the object."
            ed = rt.ops.new_step("tripo.edit_view", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id,
                                 params=EditParams(part_id=p.part_id, target=p.target, front_sha=p.front_sha, mv_task_id=task_id, view=view,
                                                   fix_sentence=fix, nonce=p.nonce, board=p.board).model_dump(mode="json"),
                                 nonce=common.new_nonce(), priority=ctx.step.priority)
            chk = rt.ops.new_step("mv.check", job_id=ctx.step.job_id, project_id=project_id, part_id=part.id, deps=[ed.id],
                                  params=CheckParams(part_id=p.part_id, target=p.target, front_sha=p.front_sha, nonce=p.nonce, board=p.board,
                                                     from_step=ed.id, t2_done=True, mv_task_id=task_id).model_dump(mode="json"),
                                  priority=ctx.step.priority)
            ctx.spawn([ed, chk])
            return StepResult(result={"next": "t2", "view": view}, message=f"one view needs a Tripo edit ({view})")
        if not p.gpt_done and common.provider_available(rt, "openai"):
            _, spec = common.load_spec(rt, project_id)
            start_gpt_views(ctx, part, spec, target=p.target, front_sha=p.front_sha, board=p.board, nonce=common.new_nonce())
            return StepResult(result={"next": "gpt_views"}, message="drawing the views with GPT instead (lower reliability)")
    # pass, or nothing left to try: the board step shows what there is (a failing set is a NEEDS_HUMAN tile with the evidence)
    out_views = {v: s for v, s in views.items()}
    nxt = rt.ops.new_step(p.board, job_id=ctx.step.job_id, project_id=project_id, part_id=part.id, deps=[],
                          params={"part_id": p.part_id, "front_sha": p.front_sha, "nonce": p.nonce, "mv_step": ctx.step.id},
                          nonce=p.nonce, priority=ctx.step.priority)
    ctx.spawn([nxt])
    return StepResult(result={"views": out_views, "source": source, "task_id": task_id, "flags": flags, "ok": not blocking,
                              "report": "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3]), "hard_failures": [r.check_id for r in blocking]},
                      message="views checked" if not blocking else "the views need a look")


def _worst_view(blocking: list[Any], ims: dict[str, Image.Image]) -> str:
    for r in blocking:
        for v in ("left", "right", "back"):
            if v in (r.evidence or "") or v in (r.subject_sha or ""):
                return v
    return "left"


def _sheet(ims: dict[str, Image.Image]) -> Image.Image:
    from duoskin.imaging import guides

    tiles = [guides.vlm_image(ims[v].convert("RGBA"), with_checkerboard=False).resize((384, 384)) for v in VIEWS if v in ims]
    return guides.side_by_side(tiles, gutter=16)


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("tripo.multiview", run_multiview, version=1, pool="api", paid=True, provider="tripo", Params=MvParams,
                              estimate=estimate_t1, poll=poll_multiview, cacheable=False)
    registry.register_handler("tripo.edit_view", run_edit_view, version=1, pool="api", paid=True, provider="tripo", Params=EditParams,
                              estimate=estimate_t2, poll=poll_edit_view, cacheable=False)
    registry.register_handler("mv.check", run_mv_check, version=1, pool="api", paid=True, provider="anthropic", Params=CheckParams,
                              estimate=lambda p: 0.05, cacheable=False)
    AL.register_callback("view", view_done)
