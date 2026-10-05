"""Fixtures and helpers of the pipeline tests (test code only): a project with a locked fixture spec, waiting for the scheduler, step dumps."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spec"))

from duoskin.models.common import new_id, sha256_of, utcnow
from duoskin.models.project import Project, ProjectSettings, Stage, VersionPins
from duoskin.models.spec_record import SpecRecord

FIXTURES = Path(__file__).resolve().parents[1] / "spec" / "fixtures"


def load_spec_dict(name: str = "spec_complement_gb") -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def locked_spec(name: str = "spec_complement_gb") -> dict[str, Any]:
    """The fixture spec as the part pipeline sees a locked spec. The fixture's near-black line colour cannot pass FACE-06 (line-to-skin dE >= 20
    on the darkest of the 5 tones), so the ink colour becomes a dark indigo and both brows use it; nothing else changes."""
    spec = load_spec_dict(name)
    for c in spec["palette"]:
        if c["id"] == "p6":
            c["hex"] = "#2D1B69"
            c["name"] = "ink indigo"
    for ch in ("a", "b"):
        spec[ch]["face"]["brow_ref"] = "p6"
        spec[ch]["face"]["pupil_ref"] = "p4"     # orange iris + indigo pupil blend outside the iris/pupil colours (F_LASH_LID_SPLIT): charcoal does not
    return spec


def make_pins(rt) -> VersionPins:
    from duoskin import __version__
    from duoskin.checks import thresholds
    from duoskin.models import kitenums, llm_io
    from duoskin.prompts import registry as preg

    s = rt.effective_settings()
    return VersionPins(models={"planner": s.models.planner, "image_draft": s.models.image_draft, "image_final": s.models.image_final,
                               "tripo_mesh": s.models.tripo_mesh}, prompt_versions=preg.prompt_versions(), schema_hashes={},
                       house_style_version=0, style_guide_version=1, kit_manifest_sha=kitenums.kit_manifest_sha(),
                       thresholds_version=thresholds.THRESHOLDS_VERSION, rules_version=llm_io.RULES_VERSION, app_version=__version__)


def make_project(rt, spec_name: str = "spec_complement_gb", *, name: str = "Test Duo", spec: dict | None = None, combo: str | None = None,
                 stage: Stage = Stage.PARTS, **settings) -> tuple[Project, SpecRecord]:
    """A project whose spec is locked (approved): what the parts pipeline starts from (the Gate 1 approval is another track's)."""
    sd = spec if spec is not None else locked_spec(spec_name)
    now = utcnow()
    p = Project(id=new_id("prj"), name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo=combo or sd["combo"],
                brief="", stage=stage, settings=ProjectSettings(**settings), pins=make_pins(rt))
    rt.repo.create_project(p)
    rec = SpecRecord(id=new_id("spc"), project_id=p.id, plan_set_id="pls_test", plan_index=0, spec=sd, status="approved",
                     palette_source="concept_extracted", sha256=sha256_of(sd))     # type: ignore[arg-type]
    rt.repo.add_spec(rec)
    p = rt.repo.mutate_project(p.id, lambda x: (setattr(x, "approved_spec_id", rec.id), setattr(x, "current_spec_id", rec.id)))
    return p, rec


def wait_for(pred, timeout: float = 60.0, interval: float = 0.05, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {message}")
        time.sleep(interval)


def dump_steps(rt, project_id: str, states: tuple[str, ...] = ()) -> str:
    out = []
    for s in rt.repo.list_steps(project_id=project_id, limit=2000):
        if states and s.state.value not in states:
            continue
        err = f" ERR {s.error.kind}:{s.error.message[:140]}" if s.error else ""
        dur = f"{(s.finished_at - s.started_at).total_seconds():5.1f}s" if s.started_at and s.finished_at else "   -  "
        out.append(f"{s.kind:18} {s.part_id or '-':14} {s.state.value:14} {dur} {s.message[:60]}{err}")
    return "\n".join(out)


def open_gate(rt, project_id: str, kind: str):
    for g in rt.repo.list_gates(project_id, "open"):
        if g.kind.value == kind:
            return g
    return None


def part_states(rt, project_id: str) -> dict[str, str]:
    return {p.id: p.state.value for p in rt.repo.list_parts(project_id)}


def post_decision(client, gate_id: str, tile_id: str, action: str, *, version: int | None = None, confirm: bool = True, rt=None, **kw):
    """POST a gate decision through the API (the tile's current version unless given). A decision that shows warnings is provisional (APP_SPEC §9.9):
    with ``confirm`` it is confirmed at once ("approve anyway", every shown warning overridden); the returned response is the decision's."""
    if version is None:
        g = client.get(f"/api/gates/{gate_id}").json()
        version = next(t["version"] for t in g["tiles"] if t["tile_id"] == tile_id)
    body = {"tile_id": tile_id, "action": action, "expected_version": version, "client_decision_id": new_id("cli"), **kw}
    r = client.post(f"/api/gates/{gate_id}/decisions", json=body)
    if confirm and r.status_code == 200 and r.json().get("provisional"):
        did = r.json()["decision"]["id"]
        shown = r.json()["decision"]["warnings_shown"]
        r2 = client.post(f"/api/gates/{gate_id}/decisions/{did}/confirm", json={"override_warnings": shown})
        assert r2.status_code == 200, r2.text
    return r


def wait_gate(rt, project_id: str, kind: str, timeout: float = 240.0):
    return wait_for(lambda: open_gate(rt, project_id, kind), timeout=timeout, message=f"the {kind} gate")


def wait_gate_checked(rt, project_id: str, kind: str, timeout: float = 240.0, allow_failed: tuple[str, ...] = ()):
    """Like ``wait_gate`` but a failed step (other than the allowed kinds) ends the wait at once with the step dump: no waiting out a timeout."""
    def done():
        g = open_gate(rt, project_id, kind)
        if g:
            return g
        bad = [s for s in rt.repo.list_steps(project_id=project_id, limit=2000) if s.state.value == "failed" and s.kind not in allow_failed]
        if bad:
            raise AssertionError(f"a step failed while waiting for the {kind} gate:\n" + dump_steps(rt, project_id, states=("failed",)))
        return None

    return wait_for(done, timeout=timeout, message=f"the {kind} gate")


def wait_tiles_settled(rt, project_id: str, gate_id: str, timeout: float = 240.0):
    def done():
        g = rt.repo.get_gate(gate_id)
        return g if all(t.state.value in ("ready", "approved", "needs_human", "failed") for t in g.tiles) else None

    return wait_for(done, timeout=timeout, message="every tile to settle")


def png_of(rgb: tuple[int, int, int], size: tuple[int, int] = (256, 256), box: tuple[int, int, int, int] | None = None) -> bytes:
    """A transparent PNG with one opaque box (a stand-in view)."""
    import io

    from PIL import Image

    im = Image.new("RGBA", size, (0, 0, 0, 0))
    im.paste(rgb + (255,), box or (size[0] // 4, size[1] // 8, size[0] * 3 // 4, size[1] * 7 // 8))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def give_views(rt, project_id: str, part_id: str, *, with_hair_only: bool = False) -> dict[str, str]:
    """Put four views (and the front, or the hair-only front) on a part's board so the manual and polish routes have what they need."""
    from duoskin.pipeline import common, parts

    assets: dict[str, str] = {}
    for i, v in enumerate(("front", "left", "back", "right")):
        a = rt.cas.put(png_of((80 + 20 * i, 120, 60)), "png", prov=common.prov("code", params={"test": v}))
        assets[f"view.{v}"] = a.sha256
    front = rt.cas.put(png_of((90, 130, 70)), "png", prov=common.prov("code", params={"test": "front"}))
    assets["hair_only" if with_hair_only else "front"] = front.sha256
    parts.set_board_assets(rt, project_id, part_id, assets)
    return assets
