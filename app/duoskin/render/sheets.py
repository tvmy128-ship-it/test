"""Fixed contact-sheet layouts and mesh view renders (APP_SPEC 10.11, 10.8; FAILURE_MODES CHK-D01).

* ``render_mesh_views`` / ``mesh_judge_sheet``: front, left, back, right, top and three-quarter on grey at one common scale (the
  renders ``mesh.judge`` and Gate 2 tiles use).
* ``scale_render`` (``guide_scale_<attachment>``): the mannequin outline at a fixed stud scale, the Classic box outline of the
  attachment (off-centre boxes for Hair, Back and Waist), the attachment point, and the accessory at its planned size.
* ``duo_sheet`` (Gate 3), ``phone_strip`` (about 150 px high, shown 2x nearest), ``face_pose_sheet``, ``five_tone_sheet``.
* ``RenderManifest``: every render records its camera and pixel hash; ``assert_manifest`` is the CHK-D01 assertion.

No text is drawn on any sheet.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from duoskin.mesh.types import MeshData
from duoskin.render import avatar, raster
from duoskin.roblox import limits

JUDGE_VIEWS = ("front", "left", "back", "right", "top", "three_quarter")
SHEET_BG = (242, 242, 242)
BODY_GREY = (184, 184, 184)


# --------------------------------------------------------------------------------------------------------------------
# manifest (CHK-D01)
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class RenderEntry:
    name: str
    view: str
    size: tuple[int, int]
    px_per_stud: float
    ss: int
    sha256: str


@dataclass
class RenderManifest:
    entries: list[RenderEntry] = field(default_factory=list)

    def add(self, name: str, view: str, img: Image.Image | np.ndarray, px_per_stud: float, ss: int = 1) -> RenderEntry:
        arr = np.asarray(img)
        e = RenderEntry(name, view, (arr.shape[1], arr.shape[0]), float(px_per_stud), ss, hashlib.sha256(arr.tobytes()).hexdigest())
        self.entries.append(e)
        return e

    def names(self) -> list[str]:
        return [e.name for e in self.entries]


def assert_manifest(manifest: RenderManifest, expected: Sequence[str], *, same_scale: bool = False) -> None:
    """CHK-D01: every expected render exists exactly once, nothing unexpected, no blank image (and one scale when asked)."""
    names = manifest.names()
    missing = [n for n in expected if n not in names]
    dup = sorted({n for n in names if names.count(n) > 1})
    extra = [n for n in names if n not in expected]
    problems = []
    if missing:
        problems.append("missing renders: " + ", ".join(missing))
    if dup:
        problems.append("duplicate renders: " + ", ".join(dup))
    if extra:
        problems.append("unexpected renders: " + ", ".join(extra))
    if same_scale and len({round(e.px_per_stud, 4) for e in manifest.entries}) > 1:
        problems.append("renders were made at different scales")
    if problems:
        raise AssertionError("render manifest: " + "; ".join(problems))


# --------------------------------------------------------------------------------------------------------------------
# mesh views
# --------------------------------------------------------------------------------------------------------------------
def _mesh_to_render(mesh: MeshData, *, unlit: bool = False) -> raster.RenderMesh:
    tex = None
    if mesh.texture is not None:
        t = mesh.texture if isinstance(mesh.texture, Image.Image) else Image.fromarray(np.asarray(mesh.texture))
        tex = np.asarray(t.convert("RGB"))
    return raster.RenderMesh("mesh", mesh.vertices, mesh.faces, mesh.uv, tex, (200, 200, 200), 1, None, True, True, unlit)


def render_mesh_views(mesh: MeshData, views: Sequence[str] = JUDGE_VIEWS, *, size: int = 384, bg: tuple[int, int, int] = raster.GREY_BG, ss: int = 2,
                      margin: float = 0.08, manifest: RenderManifest | None = None, unlit: bool = False) -> dict[str, Image.Image]:
    """Beauty renders of a mesh on a flat background, all at ONE scale (px per stud) and centred on the bounding box."""
    rm = _mesh_to_render(mesh, unlit=unlit)
    used = mesh.vertices[np.unique(mesh.faces)] if mesh.n_tris else np.zeros((1, 3))
    centre = (used.min(axis=0) + used.max(axis=0)) / 2
    radius = float(np.linalg.norm(used.max(axis=0) - used.min(axis=0))) / 2 or 1.0
    px = size * (1 - 2 * margin) / (2 * radius)
    out: dict[str, Image.Image] = {}
    for v in views:
        cam = raster.make_camera(v, centre, px)
        r = raster.render([rm], cam, size, size, ss=ss)
        img = Image.fromarray(r.on_background(bg), "RGB")
        out[v] = img
        if manifest is not None:
            manifest.add(f"mesh.{v}", v, img, px, ss)
    return out


def contact_sheet(tiles: Sequence[Image.Image], cols: int, *, gutter: int = 8, bg: tuple[int, int, int] = SHEET_BG,
                  tile_size: tuple[int, int] | None = None) -> Image.Image:
    """A fixed grid: ``cols`` columns, row-major, every tile resized (aspect kept, centred) into the same cell."""
    if not tiles:
        raise ValueError("no tiles")
    cw, ch = tile_size or (max(t.width for t in tiles), max(t.height for t in tiles))
    rows = -(-len(tiles) // cols)
    sheet = Image.new("RGB", (cols * cw + (cols + 1) * gutter, rows * ch + (rows + 1) * gutter), bg)
    for i, t in enumerate(tiles):
        s = min(cw / t.width, ch / t.height)
        tt = t if s == 1 else t.resize((max(1, int(t.width * s)), max(1, int(t.height * s))), Image.Resampling.LANCZOS)
        r, c = divmod(i, cols)
        x = gutter + c * (cw + gutter) + (cw - tt.width) // 2
        y = gutter + r * (ch + gutter) + (ch - tt.height) // 2
        sheet.paste(tt.convert("RGB"), (x, y))
    return sheet


def mesh_judge_sheet(views: dict[str, Image.Image], *, order: Sequence[str] = JUDGE_VIEWS, cols: int = 3) -> Image.Image:
    """The mesh judge sheet: the six views in a 3 x 2 grid on light grey (no text)."""
    missing = [v for v in order if v not in views]
    if missing:
        raise ValueError("missing views: " + ", ".join(missing))
    return contact_sheet([views[v] for v in order], cols)


# --------------------------------------------------------------------------------------------------------------------
# scale render (guide_scale_<attachment>)
# --------------------------------------------------------------------------------------------------------------------
def scale_render(mannequin: avatar.Mannequin | None, asset_type: str, attachment: str | None, *, target_studs: tuple[float, float, float],
                 mesh: MeshData | None = None, accessory_rgba: Image.Image | None = None, view: str = "front", size: int = 1024,
                 margin_studs: float = 0.8, manifest: RenderManifest | None = None, hair_front_rgba: Image.Image | None = None) -> Image.Image:
    """The character outline at a fixed stud scale with the Classic box and the accessory at its planned size and attachment.

    ``mesh`` (Handle space) is drawn through the real mannequin attachment; otherwise ``accessory_rgba`` (a transparent front
    view) is scaled so its bounding box equals ``target_studs`` (x by y in the front view, z by y in the side view) and placed
    with the type's default anchor; with neither, only the planned box is drawn.
    """
    mq = mannequin or avatar.default_mannequin()
    box = limits.box_for(asset_type, attachment or None)
    att = mq.attachment(box.attachment)
    lo_b, hi_b = box.lo_hi(att)
    lo_m, hi_m = mq.bounds()
    lo = np.minimum(lo_m, lo_b)
    hi = np.maximum(hi_m, hi_b)
    side = view in ("left", "right")
    ax = 2 if side else 0
    span_x = (hi[ax] - lo[ax]) + 2 * margin_studs
    span_y = (hi[1] - lo[1]) + 2 * margin_studs
    px = min(size / span_x, size / span_y)
    centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2])
    cam = raster.make_camera(view, centre, px)
    body = [raster.RenderMesh(p.name, avatar.part_mesh(p, {}, None).vertices, avatar.part_mesh(p, {}, None).faces) for p in mq.parts]
    sil = raster.silhouette(body, cam, size, size)
    img = Image.new("RGB", (size, size), SHEET_BG)
    arr = np.asarray(img).copy()
    arr[sil] = BODY_GREY
    img = Image.fromarray(arr, "RGB")
    d = ImageDraw.Draw(img)

    def to_px(p: np.ndarray) -> tuple[float, float]:
        xy, _ = cam.project(np.asarray(p, float), size, size)
        return float(xy[0]), float(xy[1])

    # Classic box outline
    corners = [to_px(np.array([x, y, z])) for x in (lo_b[0], hi_b[0]) for y in (lo_b[1], hi_b[1]) for z in (lo_b[2], hi_b[2])]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    d.rectangle([min(xs), min(ys), max(xs), max(ys)], outline=(59, 130, 246), width=2)
    ax_, ay_ = to_px(att)
    d.line([ax_ - 8, ay_, ax_ + 8, ay_], fill=(220, 38, 38), width=2)
    d.line([ax_, ay_ - 8, ax_, ay_ + 8], fill=(220, 38, 38), width=2)
    if hair_front_rgba is not None and not side:
        from duoskin.mesh.hair import GUIDE_HEAD_CX_PX, GUIDE_HEAD_TOP_PX, GUIDE_PX_PER_STUD

        k = px / GUIDE_PX_PER_STUD
        hair = hair_front_rgba.convert("RGBA")
        hair = hair.resize((max(1, round(hair.width * k)), max(1, round(hair.height * k))), Image.Resampling.LANCZOS)
        hx, hy = to_px(np.array([0.0, mq.head.hi[1], 0.0]))
        layer = img.convert("RGBA")
        layer.alpha_composite(hair, (round(hx - GUIDE_HEAD_CX_PX * k), round(hy - GUIDE_HEAD_TOP_PX * k)))
        img = layer.convert("RGB")
        d = ImageDraw.Draw(img)
    img_arr = np.asarray(img).copy()
    if mesh is not None:
        rm = avatar.place_mesh(mq, mesh, box.attachment, name="acc.0")
        r = raster.render([rm], cam, size, size, ss=2)
        a = r.beauty[..., 3:4] / 255.0
        img_arr = (img_arr * (1 - a) + r.beauty[..., :3] * a).astype(np.uint8)
        img = Image.fromarray(img_arr, "RGB")
    elif accessory_rgba is not None:
        w, h, dep = target_studs
        wide = dep if side else w
        pw, ph = round(wide * px), round(h * px)
        art = accessory_rgba.convert("RGBA")
        bb = art.getbbox()
        if bb:
            art = art.crop(bb)
        art = art.resize((max(pw, 1), max(ph, 1)), Image.Resampling.LANCZOS)
        anchor = limits.default_anchor(asset_type)
        target_c = np.asarray(box.offset_file, float) + att
        if anchor == "bottom":
            target_c[1] = att[1] + h / 2
        elif anchor == "top":
            target_c[1] = att[1] - h / 2
        cx, cy = to_px(target_c)
        img = img.convert("RGBA")
        img.alpha_composite(art, (round(cx - art.width / 2), round(cy - art.height / 2)))
        img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        d.rectangle([cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2], outline=(22, 163, 74), width=1)
    if manifest is not None:
        manifest.add(f"scale.{view}", view, img, px)
    return img


# --------------------------------------------------------------------------------------------------------------------
# character sheets
# --------------------------------------------------------------------------------------------------------------------
def render_character_views(meshes: list[raster.RenderMesh], views: Sequence[str] = ("front", "back"), *, px_per_stud: float = 120.0,
                           size: tuple[int, int] = (480, 700), centre=(0.0, 2.6, 0.0), bg: tuple[int, int, int] = SHEET_BG, ss: int = 2,
                           manifest: RenderManifest | None = None, name: str = "char") -> dict[str, Image.Image]:
    """Dressed character renders at one fixed scale (120 px per stud like the concept guide)."""
    out = {}
    for v in views:
        cam = raster.make_camera(v, centre, px_per_stud)
        r = raster.render(meshes, cam, size[0], size[1], ss=ss)
        img = Image.fromarray(r.on_background(bg), "RGB")
        out[v] = img
        if manifest is not None:
            manifest.add(f"{name}.{v}", v, img, px_per_stud, ss)
    return out


def duo_sheet(a: dict[str, Image.Image], b: dict[str, Image.Image], *, order: Sequence[str] = ("front", "back"), gutter: int = 12) -> Image.Image:
    """The Gate 3 duo sheet: character A on the left, B on the right, ``order`` views side by side inside each half."""
    ta = [a[v] for v in order]
    tb = [b[v] for v in order]
    cw, ch = max(t.width for t in ta + tb), max(t.height for t in ta + tb)
    return contact_sheet(ta + tb, cols=len(order) * 2, gutter=gutter, tile_size=(cw, ch))


def phone_strip(images: Sequence[Image.Image], *, target_h: int = 150, gutter: int = 6, scale: int = 2) -> Image.Image:
    """Images area-downscaled to ``target_h`` pixels high, laid out in one row and shown at ``scale`` x with nearest sampling."""
    small = []
    for im in images:
        w = max(1, round(im.width * target_h / im.height))
        small.append(im.convert("RGB").resize((w, target_h), Image.Resampling.BOX))
    width = sum(s.width for s in small) + gutter * (len(small) + 1)
    strip = Image.new("RGB", (width, target_h + 2 * gutter), SHEET_BG)
    x = gutter
    for s in small:
        strip.paste(s, (x, gutter))
        x += s.width + gutter
    return strip.resize((strip.width * scale, strip.height * scale), Image.Resampling.NEAREST)


def face_pose_sheet(poses: Sequence[Image.Image], *, cols: int = 3) -> Image.Image:
    """The face pose sheet (neutral, blink, jaw drop, happy, sad ...): a fixed grid, no text."""
    return contact_sheet(poses, cols)


def five_tone_sheet(renders: Sequence[Image.Image]) -> Image.Image:
    """The 5-skin-tone sheet: exactly five renders in one row."""
    if len(renders) != 5:
        raise ValueError("the 5-tone sheet needs exactly 5 renders")
    return contact_sheet(renders, cols=5)
