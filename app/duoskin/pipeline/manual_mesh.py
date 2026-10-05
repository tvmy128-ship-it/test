"""Manual 3D mode (APP_SPEC §11.1, §11.3, §11.4, §9.10): the Tripo pack, the MANUAL_IMPORT gate, the inbox and the import wizard.

* ``manual.pack`` writes ``EXPORTS\\TripoPacks\\<duo slug>\\<pack_id>\\`` (``pipeline.tripo_pack.build_pack``: the 8 files and an empty ``return\\`` drop folder)
  and opens a **MANUAL_IMPORT** gate whose tile shows the folder, the SETTINGS text and the free-plan warning. The part is WAITING_MANUAL.
* A file arrives by drop (``api/imports``), in the pack's ``return\\`` folder, or in the shared inbox named ``DS-....glb``. The inbox watcher
  (``InboxWatcher``: ignores partial downloads, waits until the size was stable for 2 polls and the file opens exclusively, hashes it, records it in
  the ``inbox`` table) assigns it to its pack; the wizard (``assign_import``) asks which Tripo plan made it (licence flag) and starts the same mesh
  chain as the API route (``build.mesh_chain``). A failing import reopens the gate with the reasons; a mirrored model offers **Flip left/right**.
* The gate's applier: ``cancel`` closes it (the tile goes back to the API route), ``flip_mirrored`` runs ``build.flip_mirrored``.

``start_manual`` is also what "Make it myself on Tripo" (``GateAction.MAKE_MANUAL``) and every last-resort failure of the API route call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duoskin import config
from duoskin.engine import registry
from duoskin.engine.gates import ApplyContext, ApplyResult
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict, iso_utc, new_id, utcnow
from duoskin.models.gate import Gate, GateAction, GateKind, GateTile, TileState
from duoskin.models.job import JobKind, Step
from duoskin.models.part import Part, PartState
from duoskin.pipeline import common, itemspec, kits, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.manual")

INBOX_POLL_S = 2.0


class PackParams(Strict):
    part_id: str
    reason: str = ""
    nonce: str = ""


# ---------------------------------------------------------------------------------------------------- folders
def exports_root(rt: Runtime) -> Path:
    return config.exports_root(rt.effective_settings())


def inbox_dir(rt: Runtime) -> Path:
    d = config.tripo_inbox(rt.effective_settings())
    d.mkdir(parents=True, exist_ok=True)
    return d


def pack_root(rt: Runtime, project_slug: str) -> Path:
    d = exports_root(rt) / "TripoPacks" / project_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def pack_state(rt: Runtime, project_id: str, part_id: str) -> dict[str, Any] | None:
    return rt.repo.kv_get(f"pack:{project_id}:{part_id}")


# ---------------------------------------------------------------------------------------------------- the pack
def build_pack_for(rt: Runtime, project_id: str, part: Part, *, acknowledged: bool = True) -> dict[str, Any]:
    """Write the pack folder of one hair or accessory tile and remember it. Returns the pack state (``pack_id``, ``folder``, ``return_dir``, ...)."""
    from duoskin.pipeline import tripo_pack

    project = rt.repo.get_project(project_id)
    _, spec = common.load_spec(rt, project_id)
    item = itemspec.item_of(spec, part.id)
    views = {v: rt.cas.get(part.board_assets[f"view.{v}"]) for v in ("front", "left", "back", "right") if f"view.{v}" in part.board_assets}
    if len(views) < 4:
        raise ValueError("the tile has no approved views yet: a pack needs the four views")
    single_sha = part.board_assets.get("hair_only") if item.is_hair else part.board_assets.get("front")
    pack_id = tripo_pack.make_pack_id(project.slug, part.id)
    out = pack_root(rt, project.slug) / pack_id
    res = tripo_pack.build_pack(out, asset_id=part.id, project_id=project_id, part_id=part.id, kind=item.kind, category=item.category,
                                attachment=item.attachment, target_studs=item.target_studs, face_limit=item.face_limit, views=views, pack_id=pack_id,
                                project_slug=project.slug, asset_label=part.label, inbox_path=str(inbox_dir(rt)),
                                front_single=rt.cas.get(single_sha) if single_sha else None, free_plan_warning_acknowledged=acknowledged)
    ret = out / "return"
    ret.mkdir(exist_ok=True)
    state = {"pack_id": res.pack_id, "folder": str(out), "return_dir": str(ret), "settings_text": res.settings_text, "background": res.background,
             "created_at": iso_utc(utcnow()), "project_id": project_id, "part_id": part.id, "gate_id": None, "step_id": None, "warnings": res.warnings}
    rt.repo.kv_set(f"pack:{project_id}:{part.id}", state)
    rt.repo.kv_set(f"packid:{res.pack_id}", {"project_id": project_id, "part_id": part.id})
    return state


def pack_step(rt: Runtime, job_id: str, project_id: str, part: Part, *, priority: int = 100, reason: str = "") -> Step:
    return rt.ops.new_step("manual.pack", job_id=job_id, project_id=project_id, part_id=part.id, params=PackParams(part_id=part.id, reason=reason).model_dump(mode="json"),
                           priority=priority)


def start_manual(rt: Runtime, project_id: str, part_id: str, *, job_id: str | None = None, reason: str = "", step_ctx: Any = None) -> None:
    """"Make it myself on Tripo" (and every last-resort failure): write the pack (once) and open the MANUAL_IMPORT gate."""
    part = rt.repo.get_part(project_id, part_id)
    step = pack_step(rt, job_id or "", project_id, part, reason=reason or "made by hand")
    if step_ctx is not None:
        step_ctx.spawn([step])
    else:
        rt.scheduler.submit_job(JobKind.MANUAL_MESH, project_id, {"part_id": part_id, "reason": reason}, steps=[step])
    parts.set_part_state(rt, project_id, part_id, PartState.WAITING_MANUAL, flags_add=["waiting_manual"])
    parts.refresh_tile(rt, project_id, part_id)


def run_pack(ctx: StepContext, p: PackParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    state = pack_state(rt, project_id, part.id)
    if state is None or not Path(state["folder"]).exists():
        state = build_pack_for(rt, project_id, part)
    open_import_gate(ctx, part, state, reason=p.reason)
    return StepResult(result={"pack_id": state["pack_id"]}, message="waiting for the model")


def open_import_gate(ctx: StepContext, part: Part, state: dict[str, Any], *, reason: str = "", flip: bool = False) -> Gate:
    """The MANUAL_IMPORT gate of a tile: the pack folder, the SETTINGS text, a drop zone and the inbox status (APP_SPEC §9.10)."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    from duoskin.pipeline import mesh_import

    facts = {"pack_id": state.get("pack_id"), "folder": state.get("folder"), "return_dir": state.get("return_dir"), "inbox": str(inbox_dir(rt)),
             "settings_text": state.get("settings_text", ""), "free_plan_warning": mesh_import.FREE_PLAN_BANNER, "reason": reason, "status": "waiting",
             "can_flip": flip}
    actions = [GateAction.CANCEL] + ([GateAction.FLIP_MIRRORED] if flip else [])
    tile = GateTile(tile_id=part.id, part_id=part.id, label=part.label, state=TileState.WAITING_MANUAL, facts=facts,
                    badges=["Make it on Tripo's website"], allowed_actions=actions)   # type: ignore[arg-type]
    gate = Gate(id="", project_id=project_id, job_id=ctx.step.job_id, kind=GateKind.MANUAL_IMPORT, tiles=[tile], opened_at=utcnow())   # type: ignore[arg-type]
    opened = ctx.open_gate(gate)
    state = {**state, "gate_id": opened.id, "step_id": ctx.step.id}
    rt.repo.kv_set(f"pack:{ctx.step.project_id}:{part.id}", state)
    start_watcher(rt)
    return opened


class WaitParams(Strict):
    part_id: str
    reason: str = ""
    flip: bool = False


def run_wait(ctx: StepContext, p: WaitParams, inputs: list[Any]) -> StepResult:
    """Reopen the MANUAL_IMPORT gate of a tile (a failed import, a mirrored model) with the reasons."""
    rt = ctx.rt
    project_id = ctx.step.project_id or ""
    part = rt.repo.get_part(project_id, p.part_id)
    state = pack_state(rt, project_id, part.id) or {}
    open_import_gate(ctx, part, state, reason=p.reason, flip=p.flip)
    return StepResult(message="waiting for you")


def offer_flip(rt: Runtime, project_id: str, part_id: str, *, job_id: str, reason: str) -> None:
    """A mirrored model: the gate offers "Flip left/right (I checked)". Nothing is flipped without the click (APP_SPEC §10.9 step 8)."""
    step = rt.ops.new_step("manual.wait", job_id=job_id, project_id=project_id, part_id=part_id,
                           params=WaitParams(part_id=part_id, reason=reason, flip=True).model_dump(mode="json"))
    rt.scheduler.spawn(job_id, [step])
    parts.set_part_state(rt, project_id, part_id, PartState.WAITING_MANUAL, flags_add=["mirrored", "waiting_manual"])


def apply_manual_import(ac: ApplyContext) -> ApplyResult | None:
    """MANUAL_IMPORT decisions: Cancel (back to the API route or the tile) and Flip left/right."""
    rt, a, project_id = ac.rt, ac.decision.action, ac.project_id
    part_id = ac.tile.part_id or ac.tile.tile_id
    if a == GateAction.FLIP_MIRRORED:
        from duoskin.pipeline import build

        build.flip_mirrored(rt, project_id, part_id, ac.decision.id)
        ac.tile.state = TileState.APPROVED
        return ApplyResult(close_gate=True)
    if a == GateAction.CANCEL:
        part = rt.repo.get_part(project_id, part_id)
        back = PartState.READY if part.approval is None else PartState.APPROVED
        parts.set_part_state(rt, project_id, part_id, back, flags_remove=["waiting_manual", "mirrored"])
        ac.tile.state = TileState.READY
        return ApplyResult(close_gate=True)
    return None


# ---------------------------------------------------------------------------------------------------- the inbox
def _entry_id(path: str) -> str:
    return "ib_" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def inbox_entries(rt: Runtime) -> list[dict[str, Any]]:
    rows = rt.db.conn().execute("SELECT path, sha256, size, state, assigned_part, json, seen_at FROM inbox ORDER BY seen_at DESC").fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["json"]) if r["json"] else {}
        out.append({"id": meta.get("id") or _entry_id(r["path"]), "name": Path(r["path"]).name, "path": r["path"], "sha256": r["sha256"], "size": r["size"],
                    "state": r["state"], "assigned_part": r["assigned_part"], "pack_id": meta.get("pack_id"), "project_id": meta.get("project_id"),
                    "origin": meta.get("origin", ""), "seen_at": r["seen_at"]})
    return out


def entry_by_id(rt: Runtime, inbox_id: str) -> dict[str, Any] | None:
    return next((e for e in inbox_entries(rt) if e["id"] == inbox_id), None)


def _upsert_entry(rt: Runtime, path: Path, *, state: str, sha: str | None, size: int, assigned: str | None, meta: dict[str, Any]) -> dict[str, Any]:
    meta = {**meta, "id": _entry_id(str(path))}
    with rt.db.tx() as c:
        c.execute("INSERT INTO inbox (path, sha256, size, state, assigned_part, json, seen_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                  "sha256=excluded.sha256, size=excluded.size, state=excluded.state, assigned_part=excluded.assigned_part, json=excluded.json",
                  (str(path), sha, size, state, assigned, json.dumps(meta), iso_utc(utcnow())))
    return meta


class InboxWatcher:
    """Polls the shared inbox and the ``return\\`` folders of open packs. ``poll_once`` is the whole logic (tests call it); ``start`` runs it
    on a daemon thread every ``INBOX_POLL_S`` seconds, only while a MANUAL_IMPORT gate is open."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._sizes: dict[str, list[int]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def watch_dirs(self) -> list[tuple[Path, dict[str, Any] | None]]:
        rt = self.rt
        out: list[tuple[Path, dict[str, Any] | None]] = [(inbox_dir(rt), None)]
        for g in rt.repo.list_gates(None, "open"):
            if g.kind != GateKind.MANUAL_IMPORT:
                continue
            t = g.tiles[0]
            ret = t.facts.get("return_dir")
            if ret and Path(ret).is_dir():
                out.append((Path(ret), {"project_id": g.project_id, "part_id": t.part_id, "pack_id": t.facts.get("pack_id")}))
        return out

    def poll_once(self) -> list[str]:
        """One pass over every watched folder. Returns the entry ids that became ready in this pass."""
        from duoskin.pipeline import mesh_import

        rt = self.rt
        ready: list[str] = []
        for folder, pack in self.watch_dirs():
            for f in sorted(folder.iterdir()) if folder.is_dir() else []:
                if not f.is_file() or mesh_import.is_partial_name(f.name):
                    continue
                key = str(f)
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                hist = self._sizes.setdefault(key, [])
                hist.append(size)
                self._sizes[key] = hist[-4:]
                known = next((e for e in inbox_entries(rt) if e["path"] == key), None)
                if known and known["state"] in ("ready", "assigned", "imported"):
                    continue
                if not mesh_import.ready_to_import(f, self._sizes[key]):
                    _upsert_entry(rt, f, state="arriving", sha=None, size=size, assigned=None, meta={"origin": "return" if pack else "inbox"})
                    continue
                sha = mesh_import.file_digest(f)
                pid = pack["pack_id"] if pack else mesh_import.pack_id_from_name(f.name)
                target = pack or (rt.repo.kv_get(f"packid:{pid}") if pid else None)
                assigned = target["part_id"] if target else None
                meta = {"origin": "return" if pack else "inbox", "pack_id": pid, "project_id": target["project_id"] if target else None}
                e = _upsert_entry(rt, f, state="ready" if not assigned else "assigned", sha=sha, size=size, assigned=assigned, meta=meta)
                rt.bus.emit("inbox.file", {"id": e["id"], "state": "assigned" if assigned else "ready", "name": f.name}, meta.get("project_id"))
                ready.append(e["id"])
        return ready

    def _run(self) -> None:
        while not self._stop.wait(INBOX_POLL_S):
            try:
                self.poll_once()
                if not any(g.kind == GateKind.MANUAL_IMPORT for g in self.rt.repo.list_gates(None, "open")):
                    break
            except Exception:  # noqa: BLE001 - the watcher must never die on one bad file
                log.exception("inbox poll failed")
        self._thread = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="duoskin-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_WATCHERS: dict[int, InboxWatcher] = {}


def watcher(rt: Runtime) -> InboxWatcher:
    w = _WATCHERS.get(id(rt))
    if w is None:
        w = _WATCHERS[id(rt)] = InboxWatcher(rt)
    return w


def start_watcher(rt: Runtime) -> None:
    """Start the background poll (only while a MANUAL_IMPORT gate is open; a test that polls by hand sets ``DUOSKIN_NO_INBOX_THREAD=1``)."""
    import os

    if os.environ.get("DUOSKIN_NO_INBOX_THREAD") == "1":
        return
    watcher(rt).start()


# ---------------------------------------------------------------------------------------------------- the wizard
def ingest_upload(rt: Runtime, name: str, data: bytes, *, project_id: str | None = None, part_id: str | None = None) -> dict[str, Any]:
    """``POST /api/imports``: store a dropped file in the inbox folder (an app-created directory; the name is cleaned) and record it as ready."""
    from duoskin.pipeline import mesh_import

    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).name)[:120] or "upload.glb"
    if mesh_import.is_partial_name(safe):
        raise ValueError("this looks like an unfinished download: wait for the browser to finish")
    dest = inbox_dir(rt) / safe
    n = 1
    while dest.exists():
        dest = inbox_dir(rt) / f"{Path(safe).stem}-{n}{Path(safe).suffix}"
        n += 1
    dest.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    pid = mesh_import.pack_id_from_name(dest.name)
    target = rt.repo.kv_get(f"packid:{pid}") if pid else None
    if part_id and project_id:
        target = {"project_id": project_id, "part_id": part_id}
    meta = _upsert_entry(rt, dest, state="assigned" if target else "ready", sha=sha, size=len(data), assigned=target["part_id"] if target else None,
                         meta={"origin": "upload", "pack_id": pid, "project_id": target["project_id"] if target else None})
    return entry_by_id(rt, meta["id"]) or {}


def assign_import(rt: Runtime, inbox_id: str, project_id: str, part_id: str, *, tripo_plan: str, task_link: str = "") -> dict[str, Any]:
    """The import wizard's answer ("which Tripo plan made this file?") and the start of the mesh chain (APP_SPEC §11.4 steps 2-3)."""
    from duoskin.pipeline import build, mesh_import

    entry = entry_by_id(rt, inbox_id)
    if entry is None:
        raise KeyError("inbox entry")
    if tripo_plan not in ("free", "paid", "not_tripo", "api"):
        raise ValueError("tripo_plan must be free, paid or not_tripo")
    part = rt.repo.get_part(project_id, part_id)
    path = Path(entry["path"])
    data = path.read_bytes()
    licence = mesh_import.licence_from_plan(tripo_plan)           # type: ignore[arg-type]
    state = pack_state(rt, project_id, part_id) or {}
    expected = state.get("pack_id") or ""
    gate_id, step_id = state.get("gate_id"), state.get("step_id")
    pv = common.prov(mesh_import.provenance_source(licence), step_kind="mesh.upload", license=licence,   # type: ignore[arg-type]
                     params={"original_name": path.name, "tripo_plan": tripo_plan, "task_link": task_link, "pack_id": expected, "bytes": len(data)})
    asset = rt.cas.put(data, path.suffix.lstrip(".").lower() or "glb", prov=pv,
                       link=_link(project_id, part_id, "mesh_upload", pv))
    job_id = _job_for(rt, project_id, step_id)
    if gate_id:
        try:
            rt.gates.close_gate(gate_id)
        except Exception:  # noqa: BLE001 - an already closed gate is fine
            log.debug("gate %s was already closed", gate_id)
        if step_id:
            rt.gates.complete_step(step_id, result={"imported": asset.sha256})
    source = {"kind": "manual", "licence": licence, "plan": tripo_plan, "task_link": task_link, "pack_id": expected, "judge": True}
    chain = build.mesh_chain(rt, job_id, project_id, part, source=source, model_sha=asset.sha256, work_name=path.name, expect_pack_id=expected,
                             nonce=common.new_nonce())
    rt.scheduler.spawn(job_id, chain)
    with rt.db.tx() as c:
        c.execute("UPDATE inbox SET state='imported', assigned_part=? WHERE path=?", (part_id, str(path)))
    parts.set_part_state(rt, project_id, part_id, PartState.BUILDING, flags_remove=["waiting_manual"],
                         flags_add=["licence_free_plan"] if licence == "tripo_free_public_ccby_noncommercial" else [])
    return {"started": True, "steps": [s.id for s in chain], "licence": licence, "banner": mesh_import.licence_banner(licence), "pack_id": expected}


def _link(project_id: str, part_id: str, role: str, pv: Any) -> Any:
    from duoskin.models.asset import AssetLink

    return AssetLink(id=new_id("lnk"), asset_sha="0" * 64, project_id=project_id, part_id=part_id, step_id=None, role=role, status="candidate",   # type: ignore[arg-type]
                     provenance=pv)


def _job_for(rt: Runtime, project_id: str, step_id: str | None) -> str:
    if step_id:
        try:
            return rt.repo.get_step(step_id).job_id
        except Exception:  # noqa: BLE001
            pass
    for j in reversed(rt.repo.list_jobs(project_id=project_id, limit=50)):
        if j.kind in (JobKind.BUILD, JobKind.MANUAL_MESH) and j.state.value != "cancelled":
            return j.id
    return rt.scheduler.submit_job(JobKind.MANUAL_MESH, project_id, {}, steps=[]).id


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("manual.pack", run_pack, version=1, pool="cpu", paid=False, Params=PackParams, cacheable=False)
    registry.register_handler("manual.wait", run_wait, version=1, pool="cpu", paid=False, Params=WaitParams, cacheable=False)
    if rt is not None:
        rt.gates.register_applier(GateKind.MANUAL_IMPORT, apply_manual_import)


_ = (kits, time)
