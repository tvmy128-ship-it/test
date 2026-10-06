"""Drills: the BLIND calibration labelling screen (APP_SPEC §3.8, PROPOSAL_DECISION upgrade 6, FAILURE_MODES T10).

A drill session shows the person pairs made in code from their own approved duos and asks one question: is this a **clone**, a **real
duo** or **strangers** (and, separately, like or dislike). The answers are labels. Together with the real gate decisions they set the
bad-tail bounds of the [DES] thresholds (``engine/calibration.py``).

Rules that this module keeps (each has a test in ``tests/learning/``):

* **Unlocked at 5 approved duos** (``calib.drill_min_duos``) that have their renders.
* **Blind**: a served item is ``{item_id, images, question}`` and nothing else. The recipe, the measured numbers (``pair_metrics``), the
  base duos and the intended answer of the obvious anchors stay on the server; the checks' verdicts are never computed for the screen.
* **Sessions of about 20 items** (``calib.drill_session_items``), **at most 5 variants of one base duo** per session
  (``calib.drill_variants_per_base``), bases that cover several combinations and colour-plan types, rotated between sessions.
* **At most 25% of the calibration set** (``calib.drill_share_max``): a session is not served, and an answer is not taken, once the drill
  labels would pass a quarter of all labels (``engine.calibration.drill_room``); ``calibration`` also down-weights an excess.
* **Isolation**: every picture is tagged ``stream="drill"`` (its asset link, its provenance and a PNG text chunk, so the bytes never
  coincide with a pipeline asset). Drill pictures and drill answers never enter the registries, the taste profile or the critic's examples
  (``engine.calibration.assert_not_drill`` is the guard the loaders call; ``registries.register`` already refuses a non-pipeline stream).
* Free in API terms: recolours, clones and strangers are made from the stored renders; part swaps (hair, accessory, print, face) re-dress
  the second character with the first one's build files through the same renderer as the duo job, and are offered only when the build
  files exist.

Variant kinds: ``original`` (the approved pair as it is), ``recolour_both`` (one new colourway for both), ``recolour_one`` (only B gets a
new colourway), ``swap_roles`` (A's and B's two main colours trade places in B), ``clone`` (B is A recoloured: an obvious clone),
``strangers`` (A of one duo, B of another: obvious strangers), ``swap_hair``, ``swap_accessory``, ``swap_print`` and ``swap_face``.
"""
from __future__ import annotations

import io
import json
import logging
import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from duoskin.checks import thresholds as TH
from duoskin.engine import calibration as cal
from duoskin.models.asset import AssetLink
from duoskin.models.common import iso_utc, new_id, sha256_of, utcnow

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.drills")

DRILL_STREAM = "drill"
SESSION_PREFIX = "drill:session:"
ITEM_PREFIX = "drill:item:"
CURRENT_KEY = "drill:current"
USES_PREFIX = "drill:uses:"
JUDGE_PREFIX = "judge:session:"
JUDGE_CURRENT = "judge:current"
VARIANT_KINDS = ("original", "recolour_both", "recolour_one", "swap_roles", "clone", "strangers", "swap_hair", "swap_accessory", "swap_print", "swap_face")
ANCHORS = {"original": "real_duo", "clone": "clone", "strangers": "strangers"}      # the obvious ones: the answer the maker intends
SWAP_KINDS = ("swap_hair", "swap_accessory", "swap_print", "swap_face")
SERVE_AHEAD = 3                  # items made per request (swap renders take a second or two each)
HUE_STEPS = (30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330)     # degrees: the colourways of a recolour
CLONE_HUES = (20, 35, 330, 345)                                      # an obvious clone is the same figure in a nearby colourway
BG_TOL, SKIN_TOL = 4.0, 14.0     # dE2000 of a pixel to the sheet background / the skin tone: those pixels never change colour
SERVED_WIDTH = 1100              # the served sheet is downscaled to this width (the numbers are measured on the full-size renders)


class DrillError(Exception):
    """A drill request the API answers with a 4xx (``status`` 403 locked, 409 cap reached or already answered, 404 unknown)."""

    def __init__(self, message: str, code: str, status: int = 409, **extra: Any) -> None:
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


# ======================================================================================================================
# images
# ======================================================================================================================
def _bg_rgb() -> tuple[int, int, int]:
    from duoskin.render.sheets import SHEET_BG

    return tuple(SHEET_BG)       # type: ignore[return-value]


def _masks(arr: np.ndarray, skin: tuple[int, int, int] | None, bg: tuple[int, int, int]) -> np.ndarray:
    """True for the pixels a recolour may change: not the sheet background and not the skin tone."""
    from duoskin.imaging import palette as P

    lab = P.srgb_to_lab(arr.reshape(-1, 3))
    keep = P.deltaE2000(lab, P.srgb_to_lab(np.array(bg, float))) > BG_TOL
    if skin is not None:
        keep &= P.deltaE2000(lab, P.srgb_to_lab(np.array(skin, float))) > SKIN_TOL
    return keep.reshape(arr.shape[:2])


def _only(original: np.ndarray, recoloured: Image.Image, keep: np.ndarray) -> Image.Image:
    """The recoloured pixels where ``keep``, the untouched original everywhere else (the HSV round trip would move skin by a level)."""
    out = original.copy()
    out[keep] = np.asarray(recoloured)[keep]
    return Image.fromarray(out, "RGB")


def hue_shift(im: Image.Image, degrees: float, *, skin: tuple[int, int, int] | None = None, bg: tuple[int, int, int] | None = None) -> Image.Image:
    """A new colourway: every clothing and hair pixel moves ``degrees`` round the hue circle (brightness and saturation stay, so the shading
    survives); the skin tone and the sheet background are left alone."""
    rgb = im.convert("RGB")
    arr = np.asarray(rgb)
    keep = _masks(arr, skin, bg or _bg_rgb())
    hsv = np.asarray(rgb.convert("HSV")).copy()
    hsv[..., 0] = np.where(keep, (hsv[..., 0].astype(np.int32) + round(degrees * 256.0 / 360.0)) % 256, hsv[..., 0]).astype(np.uint8)
    return _only(arr, Image.fromarray(hsv, "HSV").convert("RGB"), keep)


def swap_main_colours(im: Image.Image, *, skin: tuple[int, int, int] | None = None, bg: tuple[int, int, int] | None = None) -> Image.Image:
    """The two biggest colour areas of a figure trade hues (the main and the second colour change roles); skin and background stay."""
    from duoskin.imaging import palette as P

    rgb = im.convert("RGB")
    arr = np.asarray(rgb)
    keep = _masks(arr, skin, bg or _bg_rgb())
    if keep.sum() < 20:
        return rgb
    pix = arr[keep]
    lab = P.srgb_to_lab(pix)
    rng = np.random.default_rng(0)
    centres = lab[rng.choice(len(lab), size=min(3, len(lab)), replace=False)].copy()
    for _ in range(8):
        d = np.stack([np.linalg.norm(lab - c, axis=1) for c in centres], axis=1)
        assign = d.argmin(axis=1)
        for k in range(len(centres)):
            if (assign == k).any():
                centres[k] = lab[assign == k].mean(axis=0)
    sizes = np.bincount(assign, minlength=len(centres))
    order = np.argsort(sizes)[::-1]
    if len(order) < 2 or sizes[order[1]] == 0:
        return rgb
    one, two = int(order[0]), int(order[1])
    hsv = np.asarray(rgb.convert("HSV")).copy()
    h_pix = hsv[keep][:, 0].astype(np.int32)
    h_one = int(np.median(h_pix[assign == one]))
    h_two = int(np.median(h_pix[assign == two]))
    new_h = h_pix.copy()
    new_h[assign == one] = (h_pix[assign == one] + (h_two - h_one)) % 256
    new_h[assign == two] = (h_pix[assign == two] + (h_one - h_two)) % 256
    layer = hsv[..., 0].copy()
    layer[keep] = new_h.astype(np.uint8)
    hsv[..., 0] = layer
    return _only(arr, Image.fromarray(hsv, "HSV").convert("RGB"), keep)


def recolour_views(views: dict[str, Image.Image], op: str, amount: float = 0.0, *, skin: tuple[int, int, int] | None = None) -> dict[str, Image.Image]:
    """Apply one recolour to every side of a character (``op`` is ``hue`` or ``swap``)."""
    if op == "hue":
        return {s: hue_shift(im, amount, skin=skin) for s, im in views.items()}
    if op == "swap":
        return {s: swap_main_colours(im, skin=skin) for s, im in views.items()}
    raise ValueError(f"unknown recolour {op!r}")


def pair_sheet(a: dict[str, Image.Image], b: dict[str, Image.Image]) -> Image.Image:
    """A front and back of both characters side by side: what the person sees (the Gate 3 duo sheet layout, downscaled)."""
    from duoskin.render import sheets

    order = [s for s in ("front", "back") if s in a and s in b] or sorted(set(a) & set(b))[:2]
    sheet = sheets.duo_sheet(a, b, order=order)
    if sheet.width > SERVED_WIDTH:
        sheet = sheet.resize((SERVED_WIDTH, round(sheet.height * SERVED_WIDTH / sheet.width)), Image.Resampling.BOX)
    return sheet


def store_item_image(rt: Runtime, im: Image.Image, item_id: str) -> str:
    """Save a drill picture: its provenance, its asset link and a PNG text chunk all say ``drill``, so it can never be mistaken for a pipeline asset."""
    from duoskin.pipeline import common

    info = PngInfo()
    info.add_text("duoskin-stream", DRILL_STREAM)
    info.add_text("duoskin-item", item_id)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG", pnginfo=info)
    pv = common.prov("code", stream=DRILL_STREAM, step_kind="drill.item", params={"item": item_id})        # type: ignore[arg-type]
    link = AssetLink(id=new_id("lnk"), asset_sha="0" * 64, project_id=None, role="drill_item", status="candidate", provenance=pv)
    asset = rt.cas.put(buf.getvalue(), "png", prov=pv)
    rt.cas.add_link(link.model_copy(update={"asset_sha": asset.sha256}))
    return asset.sha256


# ======================================================================================================================
# the base duos
# ======================================================================================================================
@dataclass
class DrillBase:
    project_id: str
    name: str
    combo: str
    colour_plans: tuple[str, str]
    spec: dict[str, Any]
    views: dict[str, dict[str, Image.Image]]
    skin: dict[str, tuple[int, int, int] | None] = field(default_factory=dict)
    parts: dict[str, Any] = field(default_factory=dict)         # part id -> Part (only when the build files exist)

    @property
    def swappable(self) -> set[str]:
        """The part swaps this duo can be dressed for: both characters need the files, and the two must actually differ."""
        out: set[str] = set()
        p = self.parts
        if all(f"{c}.shirt" in p and f"{c}.pants" in p for c in "ab") and p["a.shirt"].build_assets.get("template") != p["b.shirt"].build_assets.get("template"):
            out.add("swap_print")
        if all(f"{c}.face" in p for c in "ab") and all(any(k.startswith("layer.") for k in (*p[f"{c}.face"].build_assets, *p[f"{c}.face"].board_assets))
                                                       for c in "ab"):
            out.add("swap_face")
        if all(f"{c}.hair" in p and p[f"{c}.hair"].build_assets.get("gltf") for c in "ab") and p["a.hair"].build_assets["gltf"] != p["b.hair"].build_assets["gltf"]:
            out.add("swap_hair")
        accs = [x for x in p.values() if x.kind.value == "accessory" and x.build_assets.get("gltf")]
        if {x.character for x in accs} == {"a", "b"}:
            out.add("swap_accessory")
        return out


def _skin(rt: Runtime, spec: dict[str, Any], c: str) -> tuple[int, int, int] | None:
    try:
        from duoskin.pipeline import duo

        return duo.skin_of(rt, spec, c)
    except Exception:    # noqa: BLE001 - without a skin tone the recolour simply has no skin to protect
        return None


def load_base(rt: Runtime, project_id: str) -> DrillBase | None:
    """An approved duo as a drill base, or ``None`` when it has no stored renders."""
    views = cal.pair_views(rt, project_id)
    if views is None:
        return None
    from duoskin.pipeline import common

    try:
        _, spec = common.load_spec(rt, project_id)
        project = rt.repo.get_project(project_id)
    except Exception:    # noqa: BLE001
        return None
    plans = tuple(str(((spec.get(c) or {}).get("dna") or {}).get("colour_plan", "")) for c in "ab")
    parts = {}
    for part in rt.repo.list_parts(project_id):
        if part.build_assets or part.board_assets:
            parts[part.id] = part
    return DrillBase(project_id, project.name, project.combo, plans, spec, {"a": views[0], "b": views[1]},    # type: ignore[arg-type]
                     {c: _skin(rt, spec, c) for c in "ab"}, parts)


def renderable_ids(rt: Runtime) -> list[str]:
    """Approved duos whose stored renders hold both characters (cheap: nothing is decoded)."""
    out = []
    for pid in cal.approved_duo_ids(rt):
        renders = (rt.repo.kv_get(f"duo:{pid}") or {}).get("renders") or {}
        if any(k.startswith("a.") for k in renders) and any(k.startswith("b.") for k in renders):
            out.append(pid)
    return out


def usable_bases(rt: Runtime) -> list[DrillBase]:
    return [b for b in (load_base(rt, pid) for pid in renderable_ids(rt)) if b is not None]


def pick_bases(bases: list[DrillBase], count: int, uses: dict[str, int], rng: random.Random) -> list[DrillBase]:
    """``count`` bases that cover as many combinations and colour-plan types as there are; the least used so far come first."""
    pool = sorted(bases, key=lambda b: (uses.get(b.project_id, 0), rng.random()))
    chosen: list[DrillBase] = []
    seen_combo: set[str] = set()
    seen_plan: set[tuple[str, str]] = set()
    for b in pool:                                   # first pass: only bases that add a new combination or colour-plan pair
        if len(chosen) >= count:
            break
        if b.combo not in seen_combo or b.colour_plans not in seen_plan:
            chosen.append(b)
            seen_combo.add(b.combo)
            seen_plan.add(b.colour_plans)
    for b in pool:
        if len(chosen) >= count:
            break
        if b not in chosen:
            chosen.append(b)
    return chosen


# ======================================================================================================================
# making a variant
# ======================================================================================================================
@dataclass
class Variant:
    kind: str
    a: dict[str, Image.Image]
    b: dict[str, Image.Image]
    detail: dict[str, Any]
    others: list[str] = field(default_factory=list)       # other base duos used (strangers)


class _Ctx:
    """The little of a step context the dressing code reads: the runtime and the stored files."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    def read_asset(self, sha: str) -> bytes:
        return self.rt.cas.get(sha)

    def get_asset(self, sha: str):
        return self.rt.cas.get_asset(sha)

    def progress(self, *_a: Any, **_k: Any) -> None:
        return None


def dress_variant(rt: Runtime, base: DrillBase, kind: str, work: Path) -> dict[str, Image.Image]:
    """B re-dressed with one thing of A's (through the same code and camera as the duo render, APP_SPEC §10.11); A's own renders are kept."""
    from duoskin.pipeline import duo, itemspec
    from duoskin.render import avatar, sheets

    ctx: Any = _Ctx(rt)
    p = base.parts
    mq = avatar.default_mannequin()
    skin = duo.skin_of(rt, base.spec, "b")
    shirt = Image.open(io.BytesIO(rt.cas.get(p["b.shirt"].build_assets["template"])))
    pants = Image.open(io.BytesIO(rt.cas.get(p["b.pants"].build_assets["template"])))
    if kind == "swap_print":
        shirt = Image.open(io.BytesIO(rt.cas.get(p["a.shirt"].build_assets["template"])))
    face_owner = "a" if kind == "swap_face" else "b"
    overlay = duo.face_overlay(ctx, p[f"{face_owner}.face"], "neutral") if f"{face_owner}.face" in p else None
    colours = p.get("b.colours")
    modesty = Image.open(io.BytesIO(rt.cas.get(colours.board_assets["modesty_layer"]))) if colours and "modesty_layer" in colours.board_assets else None
    meshes = avatar.dress(mq, shirt=shirt, pants=pants, skin_rgb=skin, body_rgb=skin, modesty=modesty, head_overlay=overlay)
    hair_owner = "a" if kind == "swap_hair" else "b"
    hair = p.get(f"{hair_owner}.hair")
    if hair is not None and hair.build_assets.get("gltf"):
        meshes.append(avatar.place_mesh(mq, duo.load_part_mesh(ctx, hair, work / "hair"), "HairAttachment", name="hair"))
    acc_owner = "a" if kind == "swap_accessory" else "b"
    for part in sorted(p.values(), key=lambda x: x.id):
        if part.character == acc_owner and part.kind.value == "accessory" and part.build_assets.get("gltf"):
            item = itemspec.accessory_item(base.spec, part.id)
            idx = part.id.rsplit(".", 1)[-1]
            mesh = duo.load_part_mesh(ctx, part, work / part.id.replace(".", "_"))
            meshes.append(avatar.place_mesh(mq, mesh, item.attachment, name=("sticker." if item.build == "sticker_slab" else "acc.") + idx))
    return sheets.render_character_views(meshes, ("front", "back"), name="b")


def make_variant(rt: Runtime, kind: str, base: DrillBase, rng: random.Random, *, others: list[DrillBase] | None = None) -> Variant:
    """One variant of ``kind`` from ``base`` (``strangers`` also needs ``others``: other approved duos)."""
    a, b = base.views["a"], base.views["b"]
    skin_a, skin_b = base.skin.get("a"), base.skin.get("b")
    if kind == "original":
        return Variant(kind, a, b, {})
    if kind == "recolour_both":
        deg = rng.choice(HUE_STEPS)
        return Variant(kind, recolour_views(a, "hue", deg, skin=skin_a), recolour_views(b, "hue", deg, skin=skin_b), {"hue": deg})
    if kind == "recolour_one":
        deg = rng.choice(HUE_STEPS)
        return Variant(kind, a, recolour_views(b, "hue", deg, skin=skin_b), {"hue": deg, "who": "b"})
    if kind == "swap_roles":
        return Variant(kind, a, recolour_views(b, "swap", skin=skin_b), {"who": "b"})
    if kind == "clone":
        deg = rng.choice(CLONE_HUES)
        return Variant(kind, a, recolour_views(a, "hue", deg, skin=skin_a), {"hue": deg, "of": "a"})
    if kind == "strangers":
        pool = [o for o in (others or []) if o.project_id != base.project_id]
        if not pool:
            raise DrillError("strangers need a second approved duo", "no_other", 409)
        other = rng.choice(pool)
        return Variant(kind, a, other.views["b"], {"other": other.project_id}, [other.project_id])
    if kind in SWAP_KINDS:
        if kind not in base.swappable:
            raise DrillError(f"{base.project_id} cannot be dressed for {kind}", "not_swappable", 409)
        with tempfile.TemporaryDirectory(prefix="drill_", dir=str(rt.paths.tmp_dir), ignore_cleanup_errors=True) as td:
            return Variant(kind, a, dress_variant(rt, base, kind, Path(td)), {"swapped": kind.removeprefix("swap_")})
    raise ValueError(f"unknown drill kind {kind!r}")


def plan_recipes(bases: list[DrillBase], total: int, per_base: int, rng: random.Random, have_second_duo: bool) -> list[tuple[DrillBase, str]]:
    """Which variants of which bases make up a session: each base gets one obvious anchor (rotating through an original, a clone and strangers)
    and then kinds from the menu, never more than ``per_base`` in all; swaps only for bases that can be dressed for them."""
    anchors = [k for k in ("clone", "strangers", "original") if k != "strangers" or have_second_duo]
    plan: list[tuple[DrillBase, str]] = []
    menus: list[list[str]] = []
    for i, b in enumerate(bases):
        menu = ["recolour_both", "recolour_one", "swap_roles", *sorted(b.swappable)]
        rng.shuffle(menu)
        menus.append([anchors[i % len(anchors)], *menu])
    depth = 0
    while len(plan) < total and depth < per_base:
        progressed = False
        for b, menu in zip(bases, menus, strict=True):
            if depth < len(menu) and len(plan) < total:
                plan.append((b, menu[depth]))
                progressed = True
        if not progressed:
            break
        depth += 1
    rng.shuffle(plan)
    return plan


# ======================================================================================================================
# sessions
# ======================================================================================================================
def status(rt: Runtime) -> dict[str, Any]:
    """Is the drill screen unlocked, and how full is the calibration set?"""
    approved = cal.approved_duo_count(rt)
    need = int(TH.get("calib.drill_min_duos"))
    counts = cal.label_counts(rt)
    usable = len(renderable_ids(rt)) if approved >= need else 0
    return {"locked": approved < need or usable < need, "approved_duos": approved, "usable_duos": usable, "needed": need, "counts": counts.as_dict(),
            "cap": counts.cap, "cap_reached": counts.drill_room <= 0}


def _session(rt: Runtime, sid: str) -> dict[str, Any] | None:
    doc = rt.repo.kv_get(SESSION_PREFIX + sid)
    return dict(doc) if isinstance(doc, dict) else None


def _save_session(rt: Runtime, s: dict[str, Any]) -> None:
    rt.repo.kv_set(SESSION_PREFIX + s["id"], s)


def start_session(rt: Runtime, *, seed: int | None = None) -> dict[str, Any]:
    """Plan a new session: about 20 items from at least 5 base duos, at most 5 of each. Nothing is drawn yet (items are made as they are served)."""
    st = status(rt)
    if st["locked"]:
        raise DrillError(f"Drills unlock with {st['needed']} approved duos; there are {st['approved_duos']}.", "locked", 403, **st)
    if st["cap_reached"]:
        raise DrillError("Practice rounds are capped at a quarter of all your answers. Approve more duos and come back.", "cap_reached", 409, **st)
    sid = new_id("drs")
    rng = random.Random(seed if seed is not None else int(sha256_of(sid)[:8], 16))
    bases = usable_bases(rt)
    per_base = int(TH.get("calib.drill_variants_per_base"))
    total = int(TH.get("calib.drill_session_items"))
    uses = {b.project_id: int(rt.repo.kv_get(USES_PREFIX + b.project_id) or 0) for b in bases}
    need_bases = max(int(TH.get("calib.drill_min_duos")), -(-total // per_base))
    chosen = pick_bases(bases, min(need_bases, len(bases)), uses, rng)
    plan = plan_recipes(chosen, total, per_base, rng, have_second_duo=len(bases) >= 2)
    room = st["counts"]["drill_room"]
    plan = plan[:max(0, room)]                                      # never plan more than the cap lets the person answer
    items = []
    for base, kind in plan:
        items.append(new_id("dri"))
        rt.repo.kv_set(ITEM_PREFIX + items[-1], {"id": items[-1], "session": sid, "base": base.project_id, "kind": kind, "state": "planned",
                                                 "seed": rng.randrange(1 << 30)})
    for b in chosen:
        rt.repo.kv_set(USES_PREFIX + b.project_id, uses.get(b.project_id, 0) + sum(1 for x, _ in plan if x is b))
    s = {"id": sid, "kind": "drill", "state": "open", "started_at": iso_utc(utcnow()), "items": items, "answered": [], "skipped": [],
         "bases": [b.project_id for b in chosen], "limit": total}
    _save_session(rt, s)
    rt.repo.kv_set(CURRENT_KEY, sid)
    return s


def current_session(rt: Runtime) -> dict[str, Any] | None:
    sid = rt.repo.kv_get(CURRENT_KEY)
    s = _session(rt, str(sid)) if sid else None
    return s if s and s.get("state") == "open" else None


def end_session(rt: Runtime) -> dict[str, Any] | None:
    """The person pressed "stop here" (or the session ran out): the session closes and the next request plans a new one."""
    s = current_session(rt)
    if s is None:
        return None
    s["state"] = "closed"
    s["ended_at"] = iso_utc(utcnow())
    _save_session(rt, s)
    rt.repo.kv_set(CURRENT_KEY, None)
    return s


def _render_item(rt: Runtime, item: dict[str, Any], bases: dict[str, DrillBase]) -> dict[str, Any]:
    """Make the picture and the hidden numbers of a planned item."""
    base = bases.get(item["base"]) or load_base(rt, item["base"])
    if base is None:
        item["state"] = "unavailable"
        return item
    rng = random.Random(item["seed"])
    others = [b for k, b in bases.items() if k != base.project_id]
    try:
        v = make_variant(rt, item["kind"], base, rng, others=others)
        model = cal._dreamsim_model(rt)
        metrics = cal.pair_metrics(v.a, v.b, model=model)
        image = store_item_image(rt, pair_sheet(v.a, v.b), item["id"])
    except DrillError:
        item["state"] = "unavailable"
        return item
    item.update({"state": "ready", "image": image, "metrics": metrics, "detail": v.detail, "others": v.others,
                 "expected": ANCHORS.get(item["kind"])})
    return item


def served(item: dict[str, Any]) -> dict[str, Any]:
    """What the screen gets: nothing but an id, a picture and the question. No recipe, no numbers, no base, no intended answer."""
    return {"item_id": item["id"], "images": [item["image"]], "question": "What is this?"}


def next_items(rt: Runtime, session: dict[str, Any], n: int = SERVE_AHEAD) -> list[dict[str, Any]]:
    """The next unanswered items of a session, made when they are first asked for (``n`` at most)."""
    bases: dict[str, DrillBase] = {}
    out: list[dict[str, Any]] = []
    done = set(session["answered"]) | set(session["skipped"])
    for iid in session["items"]:
        if iid in done:
            continue
        item = dict(rt.repo.kv_get(ITEM_PREFIX + iid) or {})
        if not item:
            continue
        if item["state"] == "planned":
            if not bases:
                bases = {b.project_id: b for b in usable_bases(rt)}
            item = _render_item(rt, item, bases)
            rt.repo.kv_set(ITEM_PREFIX + iid, item)
        if item["state"] != "ready":
            session["skipped"].append(iid)
            continue
        out.append(item)
        if len(out) >= n:
            break
    return out


def session_view(rt: Runtime, *, create: bool = True) -> dict[str, Any]:
    """``GET /api/calibration/session?kind=drill``: the open session's next items (blind), or the reason there are none."""
    st = status(rt)
    base = {"locked": st["locked"], "approved_duos": st["approved_duos"], "needed": st["needed"], "counts": st["counts"], "kind": "drill",
            "minutes": list(TH.get("calib.drill_session_minutes"))}
    if st["locked"]:
        return {**base, "items": [], "message": f"Calibration needs at least {st['needed']} approved duos."}
    s = current_session(rt)
    if s is None and create and not st["cap_reached"]:
        s = start_session(rt)
    if s is None:
        return {**base, "items": [], "cap_reached": st["cap_reached"], "message": "Practice rounds are capped at a quarter of all answers.",
                "session": None}
    items = next_items(rt, s)
    _save_session(rt, s)
    answered = len(s["answered"])
    total = len(s["items"])
    if not items:
        end_session(rt)
        return {**base, "items": [], "session": {"id": s["id"], "answered": answered, "total": total, "finished": True},
                "message": "That is the end of this round. Thank you."}
    return {**base, "items": [served(i) for i in items], "session": {"id": s["id"], "answered": answered, "skipped": len(s["skipped"]), "total": total,
                                                                      "finished": False, "started_at": s["started_at"]}}


def answer(rt: Runtime, item_id: str, label: str, like: bool | None = None) -> dict[str, Any]:
    """``POST /api/calibration/labels`` for a drill item: store the answer as labels (``source="drill"``) with the hidden numbers beside it.

    ``label`` is ``clone``, ``real_duo``, ``strangers`` or ``skip``; ``like`` is optional. Refused when the item is unknown or was answered, or
    when the drill labels would pass the cap."""
    item = rt.repo.kv_get(ITEM_PREFIX + item_id)
    if not isinstance(item, dict) or item.get("state") not in ("ready", "answered", "skipped"):
        raise DrillError("That item is not in a round any more.", "unknown_item", 404)
    s = _session(rt, item["session"])
    if s is None or s.get("state") != "open":
        raise DrillError("That round is over.", "session_closed", 409)
    if item_id in s["answered"] or item_id in s["skipped"]:
        raise DrillError("You already answered this one.", "already_answered", 409)
    if label == "skip":
        s["skipped"].append(item_id)
        item["state"] = "skipped"
        _save_session(rt, s)
        rt.repo.kv_set(ITEM_PREFIX + item_id, item)
        return {"stored": 0, "session": _progress(s)}
    if label not in cal.DRILL_ANSWERS:
        raise DrillError(f"The answer must be one of {', '.join(cal.DRILL_ANSWERS)}.", "bad_label", 422)
    counts = cal.label_counts(rt)
    need = 2 if like is not None else 1
    if counts.drill_room < 1:
        raise DrillError("Practice rounds are capped at a quarter of all answers.", "cap_reached", 409)
    subjects = [item_id, item["base"], *item.get("others", [])]
    with rt.db.tx():
        cal.log_label(rt, "clone_real_stranger", "drill", subjects, {"label": label, "kind": item["kind"], "metrics": item.get("metrics", {}),
                                                                   "expected": item.get("expected")})
        if like is not None and counts.drill_room >= need:
            cal.log_label(rt, "like_dislike", "drill", subjects, {"liked": bool(like)})
        s["answered"].append(item_id)
        item["state"] = "answered"
        _save_session(rt, s)
        rt.repo.kv_set(ITEM_PREFIX + item_id, item)
    if len(s["answered"]) >= s["limit"] or cal.label_counts(rt).drill_room < 1:
        end_session(rt)
    return {"stored": 1 + (1 if like is not None and counts.drill_room >= need else 0), "session": _progress(s)}


def _progress(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": s["id"], "answered": len(s["answered"]), "skipped": len(s["skipped"]), "total": len(s["items"])}


def anchor_agreement(rt: Runtime) -> dict[str, Any]:
    """How often the person's answer matched the obvious anchors (an original is a real duo, a clone is a clone, strangers are strangers).
    A low number means the person was tired or clicking through: the session is then worth less. Never shown during a round."""
    total = right = 0
    for lab in cal.list_labels(rt, kind="clone_real_stranger", source="drill"):
        v = lab.value if isinstance(lab.value, dict) else {}
        if v.get("expected"):
            total += 1
            right += 1 if v.get("label") == v["expected"] else 0
    return {"anchors": total, "agreed": right, "rate": right / total if total else None}


# ======================================================================================================================
# judge calibration sets (FAILURE_MODES T9): "does this picture pass this rule?"
# ======================================================================================================================
def judge_rules() -> dict[str, Any]:
    """The rubric checks the vision judge answers (lower-case ids such as ``cn_back_view``), from the check registry."""
    from duoskin.checks import policy

    return {cid: m for cid, m in policy.registry().items() if cid[:1].islower()}


def judge_session_view(rt: Runtime) -> dict[str, Any]:
    """``GET /api/calibration/session?kind=judge``: stored judge verdicts on pictures of the person's own parts, to be marked right or wrong.
    The judge's verdict is never shown (the label would just agree with it)."""
    rules = judge_rules()
    done = {str(lab.subject_ids[0]) for lab in cal.list_labels(rt, kind="rule_verdict", source="calibration") if lab.subject_ids}
    rows = rt.db.conn().execute("SELECT c.id AS id, c.subject_sha AS sha, c.check_id AS cid, c.passed AS passed, p.json AS pj FROM checks c "
                                "JOIN projects p ON p.id = c.project_id WHERE c.subject_sha != '' AND c.passed IN (0, 1) "
                                "AND json_extract(c.json, '$.ran') = 1 ORDER BY c.created_at DESC").fetchall()
    limit = int(TH.get("calib.drill_session_items"))
    items = []
    for r in rows:
        if r["cid"] not in rules or r["id"] in done or cal.is_regression_name(str(json.loads(r["pj"]).get("name", ""))):
            continue
        if rt.cas.find_asset(r["sha"]) is None:
            continue
        items.append({"item_id": r["id"], "images": [r["sha"]], "question": f"Does this picture pass: {rules[r['cid']].title or r['cid']}?"})
        if len(items) >= limit:
            break
    counts = {"labels": dict(cal.label_counts(rt).by_kind)}
    per_rule: dict[str, int] = {}
    for lab in cal.list_labels(rt, kind="rule_verdict", source="calibration"):
        if isinstance(lab.value, dict) and lab.value.get("check_id"):
            per_rule[lab.value["check_id"]] = per_rule.get(lab.value["check_id"], 0) + 1
    titles = {cid: rules[cid].title for cid in per_rule if cid in rules}
    return {"kind": "judge", "locked": False, "items": items[:SERVE_AHEAD], "counts": {**counts, "labels_per_rule": per_rule, "rule_titles": titles},
            "message": "" if items else "There are no judged pictures waiting for your opinion."}


def judge_answer(rt: Runtime, item_id: str, label: str) -> dict[str, Any]:
    """``POST /api/calibration/labels`` for a judge item: your pass or fail next to the judge's verdict (stored, never shown)."""
    if label == "skip":
        return {"stored": 0}
    if label not in cal.JUDGE_ANSWERS:
        raise DrillError(f"The answer must be one of {', '.join(cal.JUDGE_ANSWERS)}.", "bad_label", 422)
    row = rt.db.conn().execute("SELECT subject_sha AS sha, check_id AS cid, passed FROM checks WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise DrillError("That picture is not in a round any more.", "unknown_item", 404)
    if any(str(lab.subject_ids[0]) == item_id for lab in cal.list_labels(rt, kind="rule_verdict", source="calibration") if lab.subject_ids):
        raise DrillError("You already answered this one.", "already_answered", 409)
    cal.log_label(rt, "rule_verdict", "calibration", [item_id], {"label": label, "check_id": row["cid"], "subject_sha": row["sha"], "judge_passed": bool(row["passed"])})
    return {"stored": 1}


def session_for(rt: Runtime, kind: str) -> dict[str, Any]:
    return judge_session_view(rt) if kind == "judge" else session_view(rt)


def answer_any(rt: Runtime, item_id: str, label: str, like: bool | None = None) -> dict[str, Any]:
    """Route an answer to the drill or the judge set by the item's id (judge items are check rows, ``chk_...``; drill items are ``dri_...``)."""
    return judge_answer(rt, item_id, label) if item_id.startswith("chk_") else answer(rt, item_id, label, like)


def register(rt: Runtime | None = None) -> None:     # the drill lane needs no step handler: items are made when they are served
    return None
