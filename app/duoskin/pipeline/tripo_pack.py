"""The Tripo pack for manual mode (APP_SPEC 11.3, Appendix B.3, bible 15.2 H1; FAILURE_MODES ACC-09, ACC-10). Pure functions.

``build_pack`` writes the folder the user takes to Tripo's website: the approved views at 2048 px flattened on white (or ``#D9D9D9``
when the object's edge is near white), the single front view, a contact sheet with code-drawn arrows, ``SETTINGS.txt`` with exactly
which website options to choose, and ``asset.json``. Nothing is sent anywhere and no network is used.

Decisions that come from the issue file:

* every pack gets a GLOBALLY unique id ``DS-<project>-<part>-<6hex>`` (asset ids such as ``a.acc.0`` repeat in every project and
  the inbox is shared); ``SETTINGS.txt`` tells the user to rename the downloaded file to it;
* ``SETTINGS.txt`` is ASCII; a non-ASCII inbox path is left out and the Open-folder button is named instead;
* the website steps are third-party UI: they are worded generically and marked as possibly out of date;
* hair packs carry the hair-ONLY views (no grey head cube) and say so; DuoSkin still removes a head if one comes back.
"""
from __future__ import annotations

import io
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from duoskin.mesh import colour as col
from duoskin.models.common import sha256_of

SCHEMA = "duoskin.tripo_pack/1"
PACK_VERSION = 1
VIEW_FILES = {"front": "01_FRONT.png", "left": "02_LEFT_subject-left.png", "back": "03_BACK.png", "right": "04_RIGHT_subject-right.png"}
SINGLE_FILE = "00_FRONT_single.png"
PACK_FILES = (SINGLE_FILE, *VIEW_FILES.values(), "views_sheet.png", "SETTINGS.txt", "asset.json")
PACK_PX = 2048
WHITE = (255, 255, 255)
LIGHT_GREY = (217, 217, 217)       # #D9D9D9

FREE_PLAN_WARNING = (
    "On Tripo's FREE plan your model becomes PUBLIC (shown in Tripo's community, labelled CC BY 4.0)\n"
    "  and Tripo gives no commercial-use rights. Use a paid plan for anything you may sell, and do not\n"
    "  upload unreleased designs on the free plan.")


class PackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PackResult:
    pack_id: str
    out_dir: str
    files: dict[str, str] = field(default_factory=dict)
    asset: dict[str, Any] = field(default_factory=dict)
    settings_text: str = ""
    background: str = "#FFFFFF"
    warnings: list[str] = field(default_factory=list)


def _slug(text: str, default: str, maxlen: int = 24) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:maxlen].strip("-")
    return s or default


def make_pack_id(project_slug: str, part_id: str, *, token: str | None = None) -> str:
    """``DS-<project slug>-<part>-<6 hex>``, for example ``DS-pink-duo-a_acc_0-3f9c1a``. Unique across projects."""
    part = re.sub(r"[^a-z0-9]+", "_", part_id.lower()).strip("_") or "part"
    t = (token or secrets.token_hex(3)).lower()
    if not re.fullmatch(r"[0-9a-f]{6}", t):
        raise PackError("bad_token", "the pack token must be 6 hex digits")
    return f"DS-{_slug(project_slug, 'project')}-{part}-{t}"


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "replace").decode("ascii")


def _load(src: Any) -> Image.Image:
    if isinstance(src, Image.Image):
        return src
    if isinstance(src, (bytes, bytearray)):
        im = Image.open(io.BytesIO(bytes(src)))
    else:
        im = Image.open(Path(src))
    im.load()
    return im


def _source_bytes(src: Any) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    if isinstance(src, (str, Path)):
        return Path(src).read_bytes()
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    return buf.getvalue()


def object_mask(img: Image.Image) -> np.ndarray:
    """Foreground mask: alpha when the view is transparent, else everything different from the border colour."""
    rgba = np.asarray(img.convert("RGBA"))
    if rgba[..., 3].min() < 250:
        return rgba[..., 3] > 127
    rgb = rgba[..., :3].astype(float)
    ring = np.concatenate([rgb[:3].reshape(-1, 3), rgb[-3:].reshape(-1, 3), rgb[:, :3].reshape(-1, 3), rgb[:, -3:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    return np.abs(rgb - bg).max(axis=2) > 14


def edge_near_white(views: list[Image.Image], *, de_max: float = 10.0, ring_px: int = 3) -> tuple[bool, float]:
    """True when the object's outer 3-px ring is within ``de_max`` dE2000 of white in any view (then flatten on #D9D9D9)."""
    worst = 99.0
    for v in views:
        mask = object_mask(v)
        if not mask.any():
            continue
        ring = mask & ~ndimage.binary_erosion(mask, iterations=ring_px)
        if not ring.any():
            continue
        rgb = np.asarray(v.convert("RGB"))[ring].astype(float)
        de = float(col.de2000_rgb(np.median(rgb, axis=0), np.array(WHITE, float)))
        worst = min(worst, de)
    return worst <= de_max, worst


def flatten(img: Image.Image, bg: tuple[int, int, int], size: int = PACK_PX) -> Image.Image:
    """The view on a flat background, resized to ``size`` x ``size`` only when it is not already (no cropping, no re-centring)."""
    rgba = img.convert("RGBA")
    if rgba.size != (size, size):
        rgba = rgba.resize((size, size), Image.Resampling.LANCZOS)
    base = Image.new("RGBA", rgba.size, bg + (255,))
    return Image.alpha_composite(base, rgba).convert("RGB")


def views_sheet(views: Mapping[str, Image.Image], *, tile: int = 512, bg: tuple[int, int, int] = (242, 242, 242)) -> Image.Image:
    """Four views in a row with a code-drawn marker under each showing which way the object's FRONT faces: a dot toward you
    (front view), an arrow to the image's LEFT edge (left view), a cross away from you (back view), an arrow to the RIGHT edge
    (right view). No text."""
    order = ("front", "left", "back", "right")
    strip = tile // 4
    gap = 16
    sheet = Image.new("RGB", (4 * tile + 5 * gap, tile + strip + 2 * gap), bg)
    d = ImageDraw.Draw(sheet)
    ink = (40, 40, 40)
    for i, name in enumerate(order):
        x0 = gap + i * (tile + gap)
        im = views[name].convert("RGB").resize((tile, tile), Image.Resampling.LANCZOS)
        sheet.paste(im, (x0, gap))
        cx, cy = x0 + tile // 2, gap + tile + strip // 2
        r = strip // 3
        if name == "front":
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=4)
            d.ellipse([cx - r // 3, cy - r // 3, cx + r // 3, cy + r // 3], fill=ink)
        elif name == "back":
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=4)
            k = int(r * 0.7)
            d.line([cx - k, cy - k, cx + k, cy + k], fill=ink, width=4)
            d.line([cx - k, cy + k, cx + k, cy - k], fill=ink, width=4)
        else:
            sgn = -1 if name == "left" else 1
            tail, head = cx - sgn * r * 1.6, cx + sgn * r * 1.6
            d.line([tail, cy, head, cy], fill=ink, width=6)
            d.polygon([(head, cy), (head - sgn * r * 0.9, cy - r * 0.8), (head - sgn * r * 0.9, cy + r * 0.8)], fill=ink)
    return sheet


def render_settings_txt(*, pack_id: str, asset_label: str, kind: str, category: str, attachment: str, face_limit: int, inbox_path: str = "",
                        views_variant: str = "accessory", hair: bool = False, created_at: str = "") -> str:
    """The ASCII ``SETTINGS.txt`` (bible H1 template with the issue-file changes). Returns the text."""
    attach_phrase = re.sub(r"(?<!^)(?=[A-Z])", " ", attachment.replace("Attachment", "")).strip().lower() or "attachment"
    label = _ascii(asset_label or pack_id)
    inbox = _ascii(inbox_path)
    where = (f"or save it into: {inbox}" if inbox and inbox == inbox_path
             else "or save it into the inbox folder (use the Open inbox folder button in DuoSkin Studio)")
    lines = [
        f"DuoSkin Studio - Tripo pack {pack_id}",
        f"For {label} ({_ascii(kind)}, {_ascii(category)} item on the {attach_phrase} attachment)",
        "Views in this folder (all 2048 x 2048, same scale):",
        "  01_FRONT.png   02_LEFT_subject-left.png   03_BACK.png   04_RIGHT_subject-right.png",
        "  00_FRONT_single.png is the same front view for single-image mode.",
        "",
        "BEFORE YOU START",
        f"  {FREE_PLAN_WARNING}",
    ]
    if hair:
        lines += [
            "",
            "ABOUT THE HAIR VIEWS",
            "  These views show the hair ON ITS OWN (no head). If the model you get back still has a grey cube",
            "  head under the hair, leave it in: DuoSkin finds and cuts it out, then fits the hair to the real head.",
        ]
    lines += [
        "",
        "STEPS (Tripo's screens change; the names below are how they were described when this pack was made [UNVERIFIED]:",
        "  look for the newest model, called P2.0 at the time)",
        "  1. Open Tripo Studio in your browser and sign in.",
        "  2. Choose Smart Mesh, then the newest model (P2.0).",
        "  3. If a Multi-view option is offered, choose it and fill the slots:",
        "       Front = 01_FRONT.png   Left = 02_LEFT_subject-left.png",
        "       Back  = 03_BACK.png    Right = 04_RIGHT_subject-right.png",
        "     \"Left\" is the object's own left side: in that picture the object's front points to the left edge.",
        "     If Multi-view is not offered, use single image with 00_FRONT_single.png.",
        "  4. If these settings are shown: Triangles (not quads). Face limit about " + str(face_limit) + ".",
        "     Texture ON, standard (2K). PBR OFF. No compression.",
        "  5. Generate. Download the model as GLB straight away (free-plan history is kept about one day).",
        f"  6. Rename the downloaded file to {pack_id}.glb (Tripo uses its own file names; this name tells",
        "     DuoSkin which item it belongs to).",
        f"  7. Drag the .glb file onto the \"{label}\" tile in DuoSkin Studio,",
        f"     {where}",
        "     Do not edit or rename parts first; DuoSkin repairs, reduces and checks it for Roblox.",
        "",
        f"Pack version {PACK_VERSION}" + (f", made {created_at}" if created_at else ""),
    ]
    text = "\n".join(lines) + "\n"
    assert text.isascii(), "SETTINGS.txt must be ASCII"
    return text


def build_pack(out_dir: str | Path, *, asset_id: str, project_id: str, part_id: str, kind: str, category: str, attachment: str,
               target_studs: tuple[float, float, float], face_limit: int, views: Mapping[str, Any], pack_id: str | None = None,
               project_slug: str = "", asset_label: str = "", inbox_path: str = "", views_variant: str | None = None,
               front_single: Any = None, created_at: str | None = None, free_plan_warning_acknowledged: bool = False) -> PackResult:
    """Write the 7-file manual pack into ``out_dir`` (``...\\TripoPacks\\<duo>\\<asset_id>\\``) and return what was written.

    ``views`` maps ``front``, ``left``, ``back``, ``right`` to the approved views (PIL image, PNG bytes or path; transparent or
    flattened, any size). ``free_plan_warning_acknowledged`` must be True: the UI shows the free-plan warning as a confirm dialog
    before the first pack of a project (ACC-10); this function refuses to write a pack otherwise.
    """
    if not free_plan_warning_acknowledged:
        raise PackError("free_plan_warning_required", "the free-plan warning must be shown and confirmed before a Tripo pack is exported (ACC-10)")
    missing = [k for k in VIEW_FILES if k not in views]
    if missing:
        raise PackError("missing_views", "the pack needs the four approved views; missing: " + ", ".join(missing))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pid = pack_id or make_pack_id(project_slug or project_id, part_id)
    created = created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    imgs = {k: _load(views[k]) for k in VIEW_FILES}
    near_white, worst = edge_near_white(list(imgs.values()))
    bg = LIGHT_GREY if near_white else WHITE
    bg_hex = "#D9D9D9" if near_white else "#FFFFFF"
    warnings: list[str] = []
    if near_white:
        warnings.append(f"the object's edge is near white (dE {worst:.1f} <= 10): the views are flattened on {bg_hex}")
    flat = {k: flatten(v, bg) for k, v in imgs.items()}
    single = flatten(_load(front_single), bg) if front_single is not None else flat["front"]
    variant = views_variant or ("hair_only" if kind.lower().startswith("hair") or category.lower() == "hair" else "accessory")
    files: dict[str, str] = {}
    hashes: dict[str, str] = {}
    src_hashes: dict[str, str] = {}

    def save_png(img: Image.Image, name: str) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        (out / name).write_bytes(data)
        files[name] = str(out / name)
        return data

    import hashlib

    for k, fname in VIEW_FILES.items():
        hashes[k] = hashlib.sha256(save_png(flat[k], fname)).hexdigest()
        src_hashes[k] = hashlib.sha256(_source_bytes(views[k])).hexdigest()
    save_png(single, SINGLE_FILE)
    save_png(views_sheet(flat), "views_sheet.png")
    text = render_settings_txt(pack_id=pid, asset_label=asset_label or asset_id, kind=kind, category=category, attachment=attachment,
                               face_limit=face_limit, inbox_path=inbox_path, views_variant=variant, hair=variant == "hair_only", created_at=created)
    (out / "SETTINGS.txt").write_text(text, encoding="ascii")
    files["SETTINGS.txt"] = str(out / "SETTINGS.txt")
    asset = {
        "schema": SCHEMA, "asset_id": asset_id, "project_id": project_id, "part_id": part_id, "kind": kind, "category": category,
        "attachment": attachment, "target_studs": [float(x) for x in target_studs], "face_limit": int(face_limit), "views": hashes,
        "pack_version": PACK_VERSION, "created_at": created,
        # additive fields (schema stays duoskin.tripo_pack/1)
        "pack_id": pid, "views_variant": variant, "views_source_sha256": src_hashes, "background": bg_hex, "views_px": PACK_PX,
        "free_plan_warning_acknowledged": True, "single_front_sha256": hashlib.sha256((out / SINGLE_FILE).read_bytes()).hexdigest(),
    }
    (out / "asset.json").write_text(json.dumps(asset, indent=1, sort_keys=True), encoding="utf-8")
    files["asset.json"] = str(out / "asset.json")
    return PackResult(pid, str(out), files, asset, text, bg_hex, warnings)


def verify_pack(out_dir: str | Path) -> list[str]:
    """Problems with a pack folder (missing files, hash mismatches, wrong sizes); empty when it is intact."""
    import hashlib

    out = Path(out_dir)
    problems = [f"missing {name}" for name in PACK_FILES if not (out / name).is_file()]
    if problems:
        return problems
    asset = json.loads((out / "asset.json").read_text(encoding="utf-8"))
    if asset.get("schema") != SCHEMA:
        problems.append(f"unknown schema {asset.get('schema')!r}")
    for k, fname in VIEW_FILES.items():
        data = (out / fname).read_bytes()
        if hashlib.sha256(data).hexdigest() != asset["views"].get(k):
            problems.append(f"{fname} does not match asset.json")
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.size != (PACK_PX, PACK_PX):
                    problems.append(f"{fname} is {im.size}, not {PACK_PX} x {PACK_PX}")
        except Exception:  # noqa: BLE001 - a damaged PNG is a problem to report, not a crash
            problems.append(f"{fname} cannot be opened as an image")
    if not (out / "SETTINGS.txt").read_text(encoding="ascii").isascii():
        problems.append("SETTINGS.txt is not ASCII")
    return problems


def pack_fingerprint(asset: Mapping[str, Any]) -> str:
    """A stable hash of an ``asset.json`` (for cache keys): everything except the creation time."""
    return sha256_of({k: v for k, v in asset.items() if k != "created_at"})
