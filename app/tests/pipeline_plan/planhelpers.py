"""Small helpers the plan-loop integration tests share: create a project, start the plan, wait for Gate 1, decide on a tile."""
from __future__ import annotations

import uuid
from typing import Any

from duoskin.engine.testkit import wait_for

WAIT_S = 280.0


def new_project(client, *, brief: str = "a cosy duo", combo: str = "bg", structure_request: str = "auto", must_include: list[str] | None = None,
                name: str = "Test duo") -> str:
    r = client.post("/api/projects", json={"name": name, "combo": combo, "brief": brief, "structure_request": structure_request,
                                           "must_include": must_include or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def start_plan(client, pid: str) -> dict[str, Any]:
    r = client.post(f"/api/projects/{pid}/plan")
    assert r.status_code == 200, r.text
    return r.json()


def concept_gate(client, pid: str) -> dict[str, Any] | None:
    gates = client.get("/api/gates", params={"project_id": pid, "state": "open"}).json()
    return next((g for g in gates if g["kind"] == "concept"), None)


def wait_gate1(client, pid: str, *, ready: bool = True, timeout: float = WAIT_S) -> dict[str, Any]:
    """Wait until Gate 1 is open (and, with ``ready``, every tile is READY or NEEDS_HUMAN: nothing is still being drawn)."""
    def check():
        g = concept_gate(client, pid)
        if g is None:
            return None
        if ready and any(t["state"] == "generating" for t in g["tiles"]):
            return None
        return g

    return wait_for(check, timeout=timeout, interval=0.3, message="Gate 1")


def plan_to_gate1(client, **kw) -> tuple[str, dict[str, Any]]:
    pid = new_project(client, **kw)
    start_plan(client, pid)
    return pid, wait_gate1(client, pid)


def decide(client, gate: dict[str, Any], tile: dict[str, Any], action: str, **kw):
    body = {"tile_id": tile["tile_id"], "action": action, "expected_version": tile["version"], "client_decision_id": uuid.uuid4().hex}
    body.update({k: v for k, v in kw.items() if v is not None})
    return client.post(f"/api/gates/{gate['id']}/decisions", json=body)


def approve(client, gate: dict[str, Any], tile: dict[str, Any], *, choice: str | None = None):
    """Approve a tile; when the decision is provisional (warnings were released) confirm it ("approve anyway")."""
    r = decide(client, gate, tile, "approve", choice=choice)
    assert r.status_code == 200, r.text
    body = r.json()
    if body["provisional"]:
        ids = [w["id"] for w in body["released_warnings"]]
        r2 = client.post(f"/api/gates/{gate['id']}/decisions/{body['decision']['id']}/confirm", json={"override_warnings": ids})
        assert r2.status_code == 200, r2.text
    return body


def specs_of(client, pid: str) -> list[dict[str, Any]]:
    return client.get(f"/api/projects/{pid}/specs").json()


def job_steps(client, pid: str, kind: str | None = None) -> list[dict[str, Any]]:
    rows = client.get("/api/jobs", params={"project_id": pid, "steps": "true"}).json()
    steps = [s for jv in rows for s in jv["steps"]]
    return [s for s in steps if kind is None or s["kind"] == kind]


def db_project(rt, *, brief: str = "a duo", combo: str = "bg", structure_request: str = "auto", must_include: list[str] | None = None,
               name: str = "Unit duo", stage=None):
    """A project row made directly (for unit tests that never run the pipeline)."""
    from duoskin.models.common import new_id, utcnow
    from duoskin.models.project import Project, ProjectSettings, Stage

    now = utcnow()
    p = Project(id=new_id("prj"), name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo=combo, brief=brief,
                structure_request=structure_request, must_include=must_include or [], stage=stage or Stage.BRIEF, settings=ProjectSettings())
    return rt.repo.create_project(p)


def approved_spec(rt, project_id: str, spec: dict, *, plan_set_id: str = "pls_unit"):
    """An approved ``SpecRecord`` (and its DNA card) for ``project_id``."""
    from duoskin.pipeline import plan as PL

    return PL.new_spec_record(rt, project_id, plan_set_id, 0, spec, status="approved")


def wait_select(client, pid: str, *, timeout: float = WAIT_S) -> None:
    """Wait for ``plan.select`` (the end of the planning part) and stop the job, so the concept pictures are never drawn."""
    def done():
        steps = job_steps(client, pid, "plan.select")
        if steps and steps[0]["state"] in ("succeeded", "failed"):
            return steps[0]
        return None

    step = wait_for(done, timeout=timeout, interval=0.2, message="plan.select")
    assert step["state"] == "succeeded", step
    jobs = client.get("/api/jobs", params={"project_id": pid}).json()
    for jv in jobs:
        client.post(f"/api/jobs/{jv['job']['id']}/cancel")


def plan_only(client, **kw) -> str:
    pid = new_project(client, **kw)
    start_plan(client, pid)
    wait_select(client, pid)
    return pid


def shown(client, pid: str) -> list[dict[str, Any]]:
    """The plans Gate 1 would show (status ``shown``), best first, as the spec records."""
    rows = [r["spec"] for r in specs_of(client, pid) if r["spec"]["status"] == "shown"]
    return sorted(rows, key=lambda s: (s["rank"] if s["rank"] is not None else 99, s["plan_index"]))


def all_records(client, pid: str) -> list[dict[str, Any]]:
    return [r["spec"] for r in specs_of(client, pid)]


def failed_hard(rec: dict[str, Any]) -> list[str]:
    return [r["check_id"] for r in rec["lint"] if r["kind"] in ("hard", "assert") and not r["passed"]]


def step_result(rt, step_id: str) -> dict[str, Any]:
    return dict(rt.repo.get_step(step_id).result)


def png_size(rt, sha: str) -> tuple[int, int]:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(rt.cas.get(sha))) as im:
        return im.size


def tile_by_slot(gate: dict[str, Any], slot: int) -> dict[str, Any]:
    return next(t for t in gate["tiles"] if t["tile_id"] == f"plan{slot}")


def fresh_gate(client, pid: str, *, ready: bool = True) -> dict[str, Any]:
    return wait_gate1(client, pid, ready=ready)


def wait_tile(client, pid: str, tile_id: str, *, version_above: int, timeout: float = WAIT_S) -> dict[str, Any]:
    """Wait until a Gate 1 tile has been updated (a newer version) and is not being drawn any more."""
    def check():
        g = concept_gate(client, pid)
        if g is None:
            return None
        t = next((x for x in g["tiles"] if x["tile_id"] == tile_id), None)
        if t is not None and t["version"] > version_above and t["state"] != "generating":
            return g, t
        return None

    return wait_for(check, timeout=timeout, interval=0.3, message=f"tile {tile_id} to be redrawn")


def open_side_gates(client, pid: str) -> list[dict[str, Any]]:
    gates = client.get("/api/gates", params={"project_id": pid, "state": "open"}).json()
    return [g for g in gates if g["kind"] in ("clarify", "change_confirm")]


def change_status(client, change_id: str) -> str:
    return client.get(f"/api/changes/{change_id}").json()["status"]
