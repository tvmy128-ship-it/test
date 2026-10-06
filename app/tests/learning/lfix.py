"""Builders of the learning tests: projects, approved duos with their stored renders and spec, check rows and labels."""
from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from duoskin.checks.model import CheckResult
from duoskin.engine.cas import make_prov
from duoskin.models.common import iso_utc, new_id, sha256_of, utcnow
from duoskin.models.project import Project, ProjectSettings, Stage
from duoskin.models.spec_record import SpecRecord

FIXTURES = Path(__file__).resolve().parents[1] / "spec" / "fixtures"
SIDES = ("front", "back", "left", "right")


def spec_dict(**world: Any) -> dict[str, Any]:
    spec = json.loads((FIXTURES / "spec_complement_gb.json").read_text(encoding="utf-8"))
    spec["world"].update(world)
    return spec


def make_project(rt, name: str = "Duo", *, combo: str = "bg", stage: Stage = Stage.GATE3, archived: bool = False, brief: str = "",
                 age_days: float = 0.0) -> Project:
    now = utcnow() - timedelta(days=age_days)
    p = Project(id=new_id("prj"), name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo=combo, brief=brief,
                stage=stage, archived=archived, settings=ProjectSettings())
    return rt.repo.create_project(p)


def approve_spec(rt, project: Project, spec: dict[str, Any] | None = None, *, plan_set_id: str = "pls_l") -> SpecRecord:
    sd = spec or spec_dict()
    rec = SpecRecord(id=new_id("spc"), project_id=project.id, plan_set_id=plan_set_id, plan_index=0, spec=sd, status="approved",
                     sha256=sha256_of(sd))     # type: ignore[arg-type]
    rt.repo.add_spec(rec)
    rt.repo.mutate_project(project.id, lambda p: (setattr(p, "approved_spec_id", rec.id), setattr(p, "current_spec_id", rec.id)))
    return rec


def remember_duo(rt, project: Project, *, vector: dict[str, Any] | None = None, minutes: int = 0) -> None:
    """The Gate 3 pick: a ``duo_memory`` row (what makes a duo an approved duo for the learning loop)."""
    when = utcnow() + timedelta(minutes=minutes)
    with rt.db.tx() as c:
        c.execute("INSERT OR REPLACE INTO duo_memory (project_id, embedding, json, approved_at) VALUES (?,?,?,?)",
                  (project.id, None, json.dumps({"vector": vector} if vector else {}), iso_utc(when)))


def approved_duo(rt, name: str = "Duo", *, spec: dict[str, Any] | None = None, minutes: int = 0, **kw: Any) -> Project:
    p = make_project(rt, name, **kw)
    approve_spec(rt, p, spec)
    remember_duo(rt, p, minutes=minutes)
    return p


def add_check(rt, project_id: str, check_id: str, *, passed: bool, kind: str = "soft", metric: str = "", value: float | None = None,
              ran: bool = True, fm_ids: list[str] | None = None, subject_sha: str = "") -> None:
    r = CheckResult(check_id=check_id, kind=kind, passed=passed, metric=metric, value=value, ran=ran, fm_ids=fm_ids or [],     # type: ignore[arg-type]
                    subject_sha=subject_sha)
    rt.repo.insert_checks([r], project_id=project_id, step_id=None)


def figure(rgb: tuple[int, int, int], *, size: tuple[int, int] = (160, 240), bg: tuple[int, int, int] = (242, 242, 242), hair=(40, 30, 30),
           shape: int = 0) -> Image.Image:
    """A blocky stand-in figure on the sheet background (what a duo render looks like to the similarity code)."""
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    w, h = size
    d.rectangle([w * .34, h * .05, w * .66, h * .27], fill=(232, 190, 150))            # head
    d.rectangle([w * .32 - shape * 4, h * .03, w * .68 + shape * 4, h * .12 + shape * 6], fill=hair)       # hair
    d.rectangle([w * .26, h * .28, w * .74, h * .60], fill=rgb)                         # torso
    d.rectangle([w * .30, h * .60, w * .70, h * .95], fill=tuple(max(0, c - 70) for c in rgb))   # legs
    return im


def store_png(rt, im: Image.Image, *, stream: str = "pipeline") -> str:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return rt.cas.put(buf.getvalue(), "png", prov=make_prov("code", stream=stream)).sha256


def store_renders(rt, project: Project, a_rgb: tuple[int, int, int], b_rgb: tuple[int, int, int], *, a_shape: int = 0, b_shape: int = 3) -> dict[str, str]:
    """The stored ``duo.render`` assets of a project (every side of both characters + the sheet) and its ``duo:<id>`` state."""
    renders: dict[str, str] = {}
    for c, rgb, shape in (("a", a_rgb, a_shape), ("b", b_rgb, b_shape)):
        for i, s in enumerate(SIDES):
            renders[f"{c}.{s}"] = store_png(rt, figure(rgb, hair=(40 + 30 * i, 30, 30), shape=shape))
    sheet = Image.new("RGB", (640, 240), (242, 242, 242))
    for i, key in enumerate(("a.front", "a.back", "b.front", "b.back")):
        sheet.paste(Image.open(io.BytesIO(rt.cas.get(renders[key]))).convert("RGB"), (i * 160, 0))
    renders["sheet"] = store_png(rt, sheet)
    rt.repo.kv_set(f"duo:{project.id}", {"candidate": "c1", "renders": renders})
    return renders


def seed_labels(rt, *, gate: int = 0, drill: int = 0, calibration: int = 0) -> None:
    from duoskin.engine import calibration as cal

    for i in range(gate):
        cal.log_label(rt, "like_dislike", "gate", [f"g{i}"], {"liked": i % 4 != 0})
    for i in range(drill):
        cal.log_label(rt, "clone_real_stranger", "drill", [f"d{i}"], {"label": "real_duo"})
    for i in range(calibration):
        cal.log_label(rt, "rule_verdict", "calibration", [f"c{i}"], {"label": "pass"})


def locked_spec() -> dict[str, Any]:
    """The fixture spec as the part pipeline sees a locked spec (needs the demo kit inventory)."""
    import pfix

    return pfix.locked_spec()


# ---------------------------------------------------------------------------------------------------- a duo with real build files
TEMPLATES = Path(__file__).resolve().parents[1] / "fixtures"


def _put_named(rt, data: bytes, ext: str, name: str) -> str:
    return rt.cas.put(data, ext, prov=make_prov("code", params={"file_name": name})).sha256


def _face_layers(rt, iris: tuple[int, int, int], brow: tuple[int, int, int]) -> dict[str, str]:
    """A tiny face layer pack (every layer a 128 px RGBA picture): an iris and a brow are enough for the drills to show a difference."""
    out = {}
    for name in ("shading", "blush", "sclera", "iris", "highlights", "lash", "lower_ticks", "brow", "nose", "mouth_closed"):
        im = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if name == "iris":
            d.ellipse([30, 50, 60, 80], fill=(*iris, 255))
            d.ellipse([70, 50, 100, 80], fill=(*iris, 255))
        elif name == "brow":
            d.rectangle([26, 36, 64, 42], fill=(*brow, 255))
            d.rectangle([66, 36, 104, 42], fill=(*brow, 255))
        elif name == "mouth_closed":
            d.line([46, 100, 82, 100], fill=(120, 40, 40, 255), width=4)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out[f"layer.{name}"] = rt.cas.put(buf.getvalue(), "png", prov=make_prov("code")).sha256
    return out


def _gltf_set(rt, tmp: Path, stem: str, *, scale: float, tint: tuple[int, int, int]) -> dict[str, str]:
    """A stand-in 3D piece (the asymmetric F fixture) stored the way a built hair or accessory is: gltf, bin and png assets with file names."""
    from duoskin.mesh import export, fixtures

    mesh = fixtures.f_fixture(width=1.8 * scale, height=2.0 * scale, depth=0.4 * scale, texture_px=64)
    tex = Image.new("RGB", (64, 64), tint)
    mesh.texture = tex
    mesh.meta["attachment_offset"] = [0.0, 0.0, 0.0]
    paths = export.write_gltf_set(mesh, tmp / stem, stem)
    return {role: _put_named(rt, Path(p).read_bytes(), {"gltf": "gltf", "bin": "bin", "png": "png"}[role], Path(p).name) for role, p in paths.items()}


def dressed_duo(rt, tmp: Path, name: str = "Dressed", *, minutes: int = 0) -> Project:
    """An approved duo with its build files (shirt and pants templates, a face layer pack, a hair piece and an accessory per character) and
    stored renders made by the real dressing code: what a drill needs to re-dress B with something of A's. Needs the demo kit inventory."""
    from duoskin.pipeline import duo as DUO
    from duoskin.pipeline import parts as PARTS
    from duoskin.render import sheets

    spec = locked_spec()
    p = approved_duo(rt, name, spec=spec, minutes=minutes)
    PARTS.ensure_parts(rt, p.id, spec)
    shirt = Image.open(TEMPLATES / "Template-Shirts-R15.png").convert("RGBA")
    pants = Image.open(TEMPLATES / "Template-Pants-R15.png").convert("RGBA")

    def png(im: Image.Image) -> str:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return rt.cas.put(buf.getvalue(), "png", prov=make_prov("code")).sha256

    def tinted(im: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
        layer = Image.new("RGBA", im.size, (*rgb, 255))
        out = Image.composite(layer, im, im.getchannel("A"))
        return Image.blend(im, out, 0.6)

    for c, shirt_rgb, pants_rgb, iris, hair_tint, scale in (("a", (200, 60, 60), (40, 60, 120), (60, 140, 220), (90, 50, 30), 0.5),
                                                              ("b", (60, 170, 90), (200, 160, 50), (200, 90, 40), (30, 30, 60), 0.6)):
        rt.repo.mutate_part(p.id, f"{c}.shirt", lambda x, c=c, rgb=shirt_rgb: x.build_assets.update({"template": png(tinted(shirt, rgb))}))
        rt.repo.mutate_part(p.id, f"{c}.pants", lambda x, rgb=pants_rgb: x.build_assets.update({"template": png(tinted(pants, rgb))}))
        layers = _face_layers(rt, iris, (60, 40, 30))
        rt.repo.mutate_part(p.id, f"{c}.face", lambda x, layers=layers: x.board_assets.update(layers))
        hair = _gltf_set(rt, tmp, f"hair_{c}_{name}".replace(" ", "_"), scale=scale, tint=hair_tint)
        rt.repo.mutate_part(p.id, f"{c}.hair", lambda x, hair=hair: x.build_assets.update(hair))
        acc = _gltf_set(rt, tmp, f"acc_{c}_{name}".replace(" ", "_"), scale=scale * 0.6, tint=shirt_rgb)
        rt.repo.mutate_part(p.id, f"{c}.acc.0", lambda x, acc=acc: x.build_assets.update(acc))

    class Ctx:
        def __init__(self) -> None:
            self.rt = rt

        def read_asset(self, sha: str) -> bytes:
            return rt.cas.get(sha)

        def get_asset(self, sha: str):
            return rt.cas.get_asset(sha)

    renders: dict[str, str] = {}
    work = tmp / "work" / name.replace(" ", "_")
    views: dict[str, dict[str, Image.Image]] = {}
    for c in "ab":
        meshes, _ = DUO.dress_character(Ctx(), p.id, spec, c, work)       # type: ignore[arg-type]
        views[c] = sheets.render_character_views(meshes, DUO.SIDES, name=c)
        for s, im in views[c].items():
            renders[f"{c}.{s}"] = store_png(rt, im)
    renders["sheet"] = store_png(rt, sheets.duo_sheet(views["a"], views["b"], order=("front", "back")))
    rt.repo.kv_set(f"duo:{p.id}", {"candidate": "c1", "renders": renders})
    return p
