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
              ran: bool = True, fm_ids: list[str] | None = None) -> None:
    r = CheckResult(check_id=check_id, kind=kind, passed=passed, metric=metric, value=value, ran=ran, fm_ids=fm_ids or [])     # type: ignore[arg-type]
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
