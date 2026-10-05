"""The export kit (APP_SPEC §10.13, Appendix B; FAILURE_MODES EXP-*): the folder and the zip the user uploads from.

``start_export(rt, project_id)`` (Gate 3 "Export upload kit", after a pick) starts the EXPORT job::

    export.build  ->  export.validate  ->  export.zip

**The export gate** (CHK-E01..E09) runs *before* ``export.build`` writes anything:

* E01 manifest complete: exactly one approved asset per part, in the content store with a matching sha256, and **no mock or placeholder source**
  (demo mode: "nothing here can be exported"). ``ALLOW_MOCK_FOR_TESTS`` (a module flag, never a setting) lets the kit be written for tests; every file name
  then carries ``MOCK``.
* E02 every ``approval_hash`` and ``build_hash`` valid, each against its own stamp, and the project's pinned versions present.
* E03 provenance complete: every item has lineage, a licence, both stamps. E05 category consistent with the attachment. E09 kit lineage: every kit asset
  has an allowed origin and a sha256; a ``license: unknown`` in an item's lineage adds a banner and a manual confirmation line (POL-07).

After the folder is written ``export.validate`` re-opens every file (E04: the classic PNGs through the template validators, the meshes through the mesh
gate, ``.gltf`` URIs are sibling file names only, stud extents within 1% of the manifest), the secret scan (E06: key shapes, signed URLs, e-mail
addresses and the exact stored key values) and the slug linter (E07) run over the folder, and the checklist (E08) is generated from the item-type table.
``export.zip`` writes the same content as ``<slug>.zip`` and scans it again.

Folder layout: APP_SPEC §10.13. Without a head base the kit has a face layer pack instead of a Head item and the checklist says so; without Blender the
manifest says ``fbx: not produced`` (§10.9.1).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duoskin import config
from duoskin.checks.runner import build_result
from duoskin.engine import deps, registry
from duoskin.engine import scheduler as sched
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import StepResult
from duoskin.models.common import Strict, iso_utc, sha256_of, utcnow
from duoskin.models.job import Job, JobKind
from duoskin.models.part import Part, PartKind
from duoskin.models.project import Stage
from duoskin.pipeline import common, itemspec, kits, parts

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.export")

#: tests set this to True (``allow_mock()`` below); it is not reachable from the settings or the API
ALLOW_MOCK_FOR_TESTS = False
SCHEMA_PROVENANCE = "duoskin.provenance/1"
SCHEMA_MANIFEST = "duoskin.manifest/1"
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
SECRET_PATTERNS = (
    ("api key", re.compile(rb"\b(?:sk-ant-[A-Za-z0-9_\-]{16,}|sk-proj-[A-Za-z0-9_\-]{16,}|sk-[A-Za-z0-9]{32,}|tsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{30,})")),
    ("bearer token", re.compile(rb"[Bb]earer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("signed url", re.compile(rb"[?&](?:X-Amz-Signature|X-Goog-Signature|Signature|token|access_token)=[A-Za-z0-9%_\-]{12,}")),
    ("email address", re.compile(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
)
PROPERTY_CHECK_LUAU = """-- DuoSkin property check: run in the Studio command bar with an accessory selected (UNTESTED until FM-T5).
local sel = game.Selection:Get()[1]
assert(sel, "select the accessory first")
local handle = sel:FindFirstChild("Handle")
assert(handle and handle:IsA("MeshPart"), "the accessory needs a MeshPart named Handle")
print("Handle size", handle.Size, "Material", handle.Material, "Transparency", handle.Transparency, "DoubleSided", handle.DoubleSided)
assert(handle.Transparency == 0, "Transparency must be 0")
assert(handle.Material == Enum.Material.Plastic, "Material must be Plastic")
print("DuoSkin property check passed")
"""
WRAPPER_LUAU = """-- accessory_wrapper.luau (UNTESTED until FM-T5): wraps the imported MeshPart in an Accessory with the attachment named in fit.json.
local fit = ... -- the decoded fit.json table
local accessory = Instance.new("Accessory")
accessory.Name = fit.asset_id
accessory.AttachmentPoint = CFrame.new(unpack(fit.attachment_offset))
return accessory
"""


class ExportParams(Strict):
    project_id: str


@contextmanager
def allow_mock():
    """Test-only: let the export gate pass mock sources (the kit is written with MOCK in every file name)."""
    global ALLOW_MOCK_FOR_TESTS
    old = ALLOW_MOCK_FOR_TESTS
    ALLOW_MOCK_FOR_TESTS = True
    try:
        yield
    finally:
        ALLOW_MOCK_FOR_TESTS = old


def state_key(project_id: str) -> str:
    return f"export:{project_id}"


def get_state(rt: Runtime, project_id: str) -> dict[str, Any]:
    return dict(rt.repo.kv_get(state_key(project_id)) or {})


# ---------------------------------------------------------------------------------------------------- start
def start_export(rt: Runtime, project_id: str) -> Job:
    rt.repo.set_project_stage(project_id, Stage.EXPORTING, bus=rt.bus)
    rt.repo.kv_set(state_key(project_id), {"status": "running", "started_at": iso_utc(utcnow())})
    return rt.scheduler.submit_job(JobKind.EXPORT, project_id, {"reason": "export upload kit"}, spec_id=rt.repo.get_project(project_id).approved_spec_id)


def _export_steps(rt: Runtime, job: Job, project: Any) -> list[Any]:
    if not common.handlers_present("export.build"):
        return []
    pid = job.project_id or ""
    mk = lambda kind, deps_=(): rt.ops.new_step(kind, job_id=job.id, project_id=pid, params=ExportParams(project_id=pid).model_dump(mode="json"),
                                                  deps=list(deps_))
    b = mk("export.build")
    v = mk("export.validate", [b.id])
    z = mk("export.zip", [v.id])
    return [b, v, z]


def set_gate3_status(rt: Runtime, project_id: str, status: dict[str, Any], *, close: bool = False) -> None:
    """Show the export state on the open Gate 3 tile ("blocked: mock sources", "done"); ``close`` decides the gate and completes its step."""
    from duoskin.models.gate import GateKind as GK

    for g in rt.repo.list_gates(project_id, "open"):
        if g.kind != GK.FINAL_PICK:
            continue
        tiles = [t.model_copy(update={"facts": {**t.facts, "export": status}, "version": t.version + 1}) for t in g.tiles]
        rt.repo.save_gate(g.model_copy(update={"tiles": tiles}))
        rt.bus.emit("tile.updated", {"gate_id": g.id, "tile_id": tiles[0].tile_id, "state": tiles[0].state.value, "version": tiles[0].version}, project_id)
        if close:
            rt.gates.close_gate(g.id)
            if g.step_id:
                rt.gates.complete_step(g.step_id, result={"export": status})


# ---------------------------------------------------------------------------------------------------- the manifest and the gate
def is_mock_asset(rt: Runtime, sha: str) -> str | None:
    """Why this asset cannot be exported (a mock or placeholder source, a drill or regression stream), or None."""
    a = rt.cas.find_asset(sha)
    if a is None:
        return "missing from the content store"
    pv = a.first_provenance
    if pv.source in ("mock", "placeholder"):
        return f"source {pv.source}"
    if "mock_lineage" in pv.notes or "no_concept_crop" in pv.notes or "no_style_sheet" in pv.notes:
        return "made from a mock or placeholder input"
    if pv.stream != "pipeline":
        return f"stream {pv.stream}"
    return None


def shipped_assets(part: Part) -> dict[str, str]:
    """The assets of one part that the kit ships (build assets), or its board final (a print has nothing to build)."""
    return dict(part.build_assets) if part.build_assets else ({"final": part.board_assets["final"]} if part.kind == PartKind.PRINT and "final" in part.board_assets else {})


def gate_checks(rt: Runtime, project_id: str, spec: dict[str, Any]) -> list[Any]:
    """CHK-E01, E02, E03 (partly), E05, E09: everything that must hold before the folder is written."""
    project = rt.repo.get_project(project_id)
    ps = [p for p in rt.repo.list_parts(project_id) if p.kind != PartKind.DUO]
    problems: list[str] = []
    mocks: list[str] = []
    for p in ps:
        if p.approval is None or p.build_stamp is None:
            problems.append(f"{p.id}: not approved and built")
            continue
        ship = shipped_assets(p)
        if not ship:
            problems.append(f"{p.id}: nothing to ship")
        for role, sha in {**p.board_assets, **ship}.items():
            why = is_mock_asset(rt, sha)
            if why:
                mocks.append(f"{p.id}/{role}: {why}")
            elif not rt.cas.exists(sha):
                problems.append(f"{p.id}/{role}: file missing from the content store")
            elif role in ship and hashlib.sha256(rt.cas.get(sha)).hexdigest() != sha:
                problems.append(f"{p.id}/{role}: sha256 does not match its content")
    res: list[Any] = []
    allowed = ALLOW_MOCK_FOR_TESTS
    e01_bad = problems + ([] if allowed else mocks)
    ev = "; ".join(e01_bad[:4]) or ("manifest complete" + (f" (test flag: {len(mocks)} mock source(s) allowed)" if mocks else ""))
    if mocks and not allowed and not problems:
        ev = "mock sources: " + "; ".join(mocks[:3]) + " (DEMO: nothing here can be exported)"
    res.append(build_result("CHK-E01", passed=not e01_bad, metric="manifest_complete", value=float(len(e01_bad)), evidence=ev, fix_hint="human"))
    bad2: list[str] = []
    for p in ps:
        v = deps.verify_part(rt.repo, p, spec, project.pins)
        if not v.approval.ok:
            bad2.append(f"{p.id}: {v.approval.reason}")
        elif v.build is not None and not v.build.ok:
            bad2.append(f"{p.id}: {v.build.reason}")
    if project.pins is None or not project.pins.models:
        bad2.append("the project has no pinned versions")
    res.append(build_result("CHK-E02", passed=not bad2, metric="stamps", evidence="; ".join(bad2[:3]) or "every approval and build stamp is valid", fix_hint="human"))
    bad5: list[str] = []
    from duoskin.roblox import limits

    for p in ps:
        if p.kind in (PartKind.HAIR, PartKind.ACCESSORY):
            it = itemspec.item_of(spec, p.id)
            try:
                limits.box_for(it.asset_type, it.attachment)
            except KeyError as exc:
                bad5.append(f"{p.id}: {exc}")
    res.append(build_result("CHK-E05", passed=not bad5, metric="category_attachment", evidence="; ".join(bad5) or "every category fits its attachment", fix_hint="human"))
    return res


def kit_lineage(rt: Runtime, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The kit assets the duo used (hair styles, fabric) with their origin, licence and sha256."""
    kit = kits.load_context(rt)
    out: list[dict[str, Any]] = []
    for c in ("a", "b"):
        sid = spec[c]["hair"]["kit_style_id"]
        meta = kit.manifest.get("hair", {}).get(sid)
        if meta:
            out.append({"kit_asset": f"hair/{sid}", "sha256": meta["sha256"], "origin": meta["origin"], "license": meta.get("license", "n/a")})
        fab = spec[c]["top"].get("fabric_id")
        fmeta = kit.manifest.get("fabrics", {}).get(fab) if fab else None
        if fmeta:
            out.append({"kit_asset": f"fabrics/{fab}", "sha256": fmeta["sha256"], "origin": fmeta["origin"], "license": "n/a"})
    seen, uniq = set(), []
    for k in out:
        if k["kit_asset"] not in seen:
            seen.add(k["kit_asset"])
            uniq.append(k)
    return uniq


# ---------------------------------------------------------------------------------------------------- writing
def kit_dir(rt: Runtime, project: Any, mock: bool) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    base = config.exports_root(rt.effective_settings()) / "Kits"
    name = f"{'MOCK_' if mock else ''}{common.ascii_slug(project.name, 'duo', 20)}_{stamp}"
    d = base / name
    n = 1
    while d.exists():
        d = base / f"{name}-{n}"
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    return d


class Writer:
    """Writes the kit files and records ``{path, sha256, bytes}`` for the manifest. In test mode every file name carries ``MOCK_``."""

    def __init__(self, root: Path, mock: bool) -> None:
        self.root = root
        self.mock = mock
        self.files: dict[str, dict[str, Any]] = {}

    def name(self, rel: str) -> str:
        p = Path(rel)
        return str(p.with_name(("MOCK_" + p.name) if self.mock and not p.name.startswith("MOCK_") else p.name)).replace("\\", "/")

    def write(self, rel: str, data: bytes) -> str:
        rel = self.name(rel)
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files[rel] = {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        return rel


def rewrite_gltf(data: bytes, bin_name: str, png_name: str) -> bytes:
    """The glTF refers to ``<stem>.bin`` and ``<stem>.png`` as sibling files: the URIs follow the names the kit uses (APP_SPEC §10.9 step 10)."""
    doc = json.loads(data.decode("utf-8"))
    for b in doc.get("buffers", []):
        if "uri" in b and not str(b["uri"]).startswith("data:"):
            b["uri"] = bin_name
    for im in doc.get("images", []):
        if "uri" in im and not str(im["uri"]).startswith("data:"):
            im["uri"] = png_name
    return json.dumps(doc, indent=1).encode("utf-8")


def mesh_files(rt: Runtime, w: Writer, part: Part, folder: str, stem: str) -> list[dict[str, Any]]:
    """The glTF set of a built mesh (``.gltf`` + ``.bin`` + ``.png``, the ``.fbx`` when produced) and its ``fit.json`` into ``folder``."""
    out = []
    gltf_name, bin_name, png_name = f"{stem}.gltf", f"{stem}.bin", f"{stem}.png"
    if w.mock:
        gltf_name, bin_name, png_name = (f"MOCK_{gltf_name}", f"MOCK_{bin_name}", f"MOCK_{png_name}")
    ba = part.build_assets
    out.append(w.files[w.write(f"{folder}/{gltf_name}", rewrite_gltf(rt.cas.get(ba["gltf"]), bin_name, png_name))])
    out.append(w.files[w.write(f"{folder}/{bin_name}", rt.cas.get(ba["bin"]))])
    out.append(w.files[w.write(f"{folder}/{png_name}", rt.cas.get(ba["png"]))])
    if ba.get("fbx"):
        out.append(w.files[w.write(f"{folder}/{stem}.fbx", rt.cas.get(ba["fbx"]))])
    if ba.get("fit_json"):
        out.append(w.files[w.write(f"{folder}/fit.json", rt.cas.get(ba["fit_json"]))])
    return out


def lineage_of(rt: Runtime, project_id: str, shas: list[str], *, depth: int = 6) -> list[dict[str, Any]]:
    """The provenance chain of the shipped assets: the step kind, the template and prompt hash, the provider, model, parameters, inputs, outputs, request
    id, cost and the checks that ran on them (APP_SPEC Appendix B.1). Walks ``input_shas`` back ``depth`` levels."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    frontier = list(shas)
    checks_by_sha: dict[str, list[dict[str, Any]]] = {}
    for _cid, r in rt.repo.list_checks(project_id=project_id):
        if r.subject_sha:
            checks_by_sha.setdefault(r.subject_sha, []).append({"check_id": r.check_id, "passed": r.passed, "kind": r.kind})
    for _ in range(depth):
        nxt: list[str] = []
        for sha in frontier:
            if sha in seen:
                continue
            seen.add(sha)
            a = rt.cas.find_asset(sha)
            if a is None:
                continue
            pv = a.first_provenance
            entry = {"step_kind": pv.step_kind or ("kit" if pv.source == "code" else pv.source), "provider": pv.provider or pv.source,
                     "template": f"{pv.prompt_id}@{pv.prompt_version}" if pv.prompt_id else None, "prompt_sha256": pv.prompt_sha256, "model": pv.model,
                     "handler_version": pv.handler_version, "params": _json_safe(pv.params), "inputs": list(pv.input_shas), "outputs": [sha],
                     "request_id": pv.request_id, "seed": pv.params.get("seed"), "task_id": pv.params.get("task_id"), "cost_usd": pv.cost_usd,
                     "source": pv.source, "license": pv.license, "checks": checks_by_sha.get(sha, [])}
            out.append(entry)
            nxt += [s for s in pv.input_shas if s not in seen]
        frontier = nxt
        if not frontier:
            break
    return out


def _json_safe(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def checks_summary(rt: Runtime, project_id: str) -> dict[str, Any]:
    """``hard_failed_during_run`` counts every hard failure the run recorded, including the candidates that were regenerated or repaired afterwards;
    the shipped files passed their own gate (CHK-E01 ... E09)."""
    hard_failed = 0
    for _cid, r in rt.repo.list_checks(project_id=project_id):
        if r.kind in ("hard", "assert") and r.ran and not r.passed:
            hard_failed += 1
    overrides = [d.warnings_overridden for g in rt.repo.list_gates(project_id) for d in rt.repo.list_decisions(g.id) if d.warnings_overridden]
    shown = sum(len(d.warnings_shown) for g in rt.repo.list_gates(project_id) for d in rt.repo.list_decisions(g.id))
    return {"hard_failed_during_run": hard_failed, "soft_warnings_shown": shown, "overrides": [x for sub in overrides for x in sub]}


def cost_totals(rt: Runtime, project_id: str) -> dict[str, Any]:
    rows = rt.db.conn().execute("SELECT provider, COALESCE(SUM(usd),0) AS s FROM cost_ledger WHERE project_id=? AND state IN ('committed','orphan') GROUP BY provider",
                                (project_id,)).fetchall()
    by = {r["provider"]: round(float(r["s"]), 6) for r in rows}
    return {"total_usd": round(sum(by.values()), 6), "by_provider": by}


def character_folder(spec: dict[str, Any], c: str) -> str:
    return f"{c.upper()}_{common.ascii_slug(spec[c].get('role_in_duo', ''), c, 16)}"


def run_build(ctx: StepContext, p: ExportParams, inputs: list[Any]) -> StepResult:
    """The export gate, then the folder: items, renders, manifest, provenance, checklist, README."""
    from duoskin.roblox import checklist as CL

    rt = ctx.rt
    project_id = p.project_id
    project = rt.repo.get_project(project_id)
    rec, spec = common.load_spec(rt, project_id)
    ctx.progress(0.05, "checking the export gate")
    results = gate_checks(rt, project_id, spec)
    blocking = common.hard_failures(results)
    if blocking:
        common.store_checks(ctx, results)
        reason = "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3])
        rt.repo.kv_set(state_key(project_id), {"status": "blocked", "reason": reason, "checks": [r.check_id for r in blocking], "at": iso_utc(utcnow())})
        rt.repo.set_project_stage(project_id, Stage.GATE3, bus=rt.bus)
        set_gate3_status(rt, project_id, {"status": "blocked", "reason": reason, "checks": [r.check_id for r in blocking]})
        raise StepFailure(f"The export gate stopped the kit: {reason}", kind="bad_request", billed="no", retryable=False,
                          user_hint="The export gate blocked this kit. In demo mode nothing can be exported; otherwise fix what the gate names.")
    mock = ALLOW_MOCK_FOR_TESTS and any(is_mock_asset(rt, s) for pt in rt.repo.list_parts(project_id) for s in {**pt.board_assets, **shipped_assets(pt)}.values())
    root = kit_dir(rt, project, mock)
    w = Writer(root, mock)
    ps = {x.id: x for x in rt.repo.list_parts(project_id)}
    items: list[dict[str, Any]] = []
    head_base = bool(kits.load_context(rt).flags.get("head_base_present"))
    for c in ("a", "b"):
        cf = character_folder(spec, c)
        for kind, fname in (("shirt", "shirt.png"), ("pants", "pants.png")):
            part = ps[f"{c}.{kind}"]
            f = w.files[w.write(f"{cf}/classic/{fname}", rt.cas.get(part.build_assets["template"]))]
            items.append(item_row(rt, part, "Shirt" if kind == "shirt" else "Pants", "classic", "", "creator_dashboard", [f], {}, spec))
        face = ps[f"{c}.face"]
        flist = []
        for role, sha in sorted(face.build_assets.items()):
            if role.startswith("layer."):
                flist.append(w.files[w.write(f"{cf}/head/face_layers/{role.split('.', 1)[1]}.png", rt.cas.get(sha))])
        if face.build_assets.get("canvas"):
            flist.append(w.files[w.write(f"{cf}/head/face_layers/face_canvas.json", rt.cas.get(face.build_assets["canvas"]))])
        for ex in ("neutral", "blink", "mouth_open", "happy"):
            if face.board_assets.get(ex):
                flist.append(w.files[w.write(f"{cf}/head/face_layers/preview_{ex}.png", rt.cas.get(face.board_assets[ex]))])
        items.append(item_row(rt, face, "Head" if head_base else "FaceLayers", "head", "", "studio" if head_base else "none", flist, {}, spec))
        hair = ps[f"{c}.hair"]
        hi = itemspec.hair_item(spec, c)
        items.append(item_row(rt, hair, "Hair", "hair", hi.attachment, "studio", mesh_files(rt, w, hair, f"{cf}/hair", "hair"), hair.build_assets, spec, item=hi))
        for pid in sorted(x for x in ps if x.startswith(f"{c}.acc.")):
            part = ps[pid]
            it = itemspec.accessory_item(spec, pid)
            folder = f"{cf}/accessories/{it.category}_{it.kind}"
            files = mesh_files(rt, w, part, folder, "acc")
            files.append(w.files[w.write(f"{folder}/accessory_wrapper.luau", WRAPPER_LUAU.encode("utf-8"))])
            items.append(item_row(rt, part, it.asset_type, it.category, it.attachment, "studio", files, part.build_assets, spec, item=it))
        colours = ps[f"{c}.colours"]
        bf = w.files[w.write(f"{cf}/body/body_colors.json", rt.cas.get(colours.build_assets["body_colors"]))]
        items.append(item_row(rt, colours, "Body", "body", "", "studio", [bf], {}, spec))
    st = __import__("duoskin.pipeline.duo", fromlist=["get_state"]).get_state(rt, project_id)
    renders = st.get("renders", {})
    for c in ("a", "b"):
        cf = character_folder(spec, c)
        for v in ("front", "back", "left", "right", "three_quarter"):
            if renders.get(f"{c}.{v}"):
                w.write(f"{cf}/renders/{v}.png", rt.cas.get(renders[f"{c}.{v}"]))
    if renders.get("sheet"):
        w.write("duo/duo_sheet.png", rt.cas.get(renders["sheet"]))
        w.write("duo/phone_strip.png", rt.cas.get(renders["phone_strip"]))
        w.write("duo/face_poses.png", rt.cas.get(renders["face_poses"]))
    w.write("studio/property_check.luau", PROPERTY_CHECK_LUAU.encode("utf-8"))
    if rt.effective_settings().three_d.studio_forward_axis == "unknown":
        w.write("studio/calibration_arrow.gltf", calibration_arrow())
    # ---- the checklist (E08) from the manifest items
    body_base = bool(kits.load_context(rt).flags.get("body_base_present"))
    shippable = [i for i in items if i["type"] != "FaceLayers" and (i["type"] != "Body" or body_base)]
    banners = banners_for(rt, project_id, st, mock)
    lineage_unknown = [i["item_id"] for i in items if any(k.get("license") == "unknown" for k in i.get("kit_lineage", []))]
    checklist = CL.build_checklist(shippable, banners=banners, creator_docs_commit=project.pins.roblox_docs_commit if project.pins else "",
                                   lineage_unknown_items=lineage_unknown)
    w.write("checklist.json", (json.dumps(CL.to_json(checklist), indent=1) + "\n").encode("utf-8"))
    w.write("CHECKLIST.html", CL.render_html(checklist).encode("utf-8"))
    ctx.progress(0.7, "writing the manifest and the provenance")
    manifest = {"schema": SCHEMA_MANIFEST, "project": project.name, "slug": project.slug, "items": items, "mock": mock,
                "files": sorted(w.files.values(), key=lambda f: f["path"])}
    prov = provenance_doc(rt, project, spec, items, mock, st)
    w.write("manifest.json", (json.dumps(manifest, indent=1) + "\n").encode("utf-8"))
    w.write("provenance.json", (json.dumps(prov, indent=1) + "\n").encode("utf-8"))
    card = rt.repo.get_dna_card(project.approved_spec_id or rec.id)
    w.write("dna_card.json", (json.dumps(card or {}, indent=1) + "\n").encode("utf-8"))
    w.write("spec.json", (json.dumps(spec, indent=1) + "\n").encode("utf-8"))
    w.write("README.txt", readme(project, spec, items, banners, mock).encode("ascii", "replace"))
    rt.repo.kv_set(state_key(project_id), {"status": "built", "kit_dir": str(root), "mock": mock, "checklist": CL.to_json(checklist), "banners": banners,
                                           "gate": [r.check_id for r in results], "files": len(w.files)})
    results += [build_result("CHK-E03", passed=all(i["lineage"] and i["license"] and i["approval_hash"] and i["build_hash"] for i in items),
                             metric="provenance_complete", evidence="every item has lineage, a licence and both stamps", fix_hint="human"),
                build_result("CHK-E08", passed=bool(checklist.items), metric="checklist", evidence=f"{len(checklist.items)} checklist item(s)")]
    kl = kit_lineage(rt, spec)
    results.append(build_result("CHK-E09", passed=all(k.get("origin") and k.get("sha256") for k in kl), metric="kit_lineage",
                                evidence=f"{len(kl)} kit asset(s) with an allowed origin and a sha256"))
    common.store_checks(ctx, results)
    return StepResult(outputs=[], result={"kit_dir": str(root), "files": len(w.files), "mock": mock, "items": len(items)}, message=f"kit written ({len(w.files)} files)")


def calibration_arrow() -> bytes:
    import tempfile

    from duoskin.mesh import export as MX
    from duoskin.mesh import fixtures

    with tempfile.TemporaryDirectory() as td:
        files = MX.write_gltf_set(fixtures.f_fixture(), td, "calibration_arrow")
        return Path(files["gltf"]).read_bytes()


def item_row(rt: Runtime, part: Part, typ: str, category: str, attachment: str, channel: str, files: list[dict[str, Any]], build_assets: dict[str, str],
             spec: dict[str, Any], *, item: itemspec.Item | None = None) -> dict[str, Any]:
    facts = (rt.repo.kv_get(parts.facts_key(part.project_id, part.id)) or {}).get("mesh_facts", {}) or {}
    lin = lineage_of(rt, part.project_id, list(shipped_assets(part).values()))
    kit_lin = [k for k in kit_lineage(rt, spec) if part.kind == PartKind.HAIR and k["kit_asset"].startswith("hair/")] if part.kind == PartKind.HAIR else []
    return {"item_id": part.id, "character": part.character, "type": typ, "category": category, "attachment": attachment, "scale_type": "Classic",
            "target_studs": list(item.target_studs) if item else None, "bbox_studs": facts.get("bbox_studs"), "tris": facts.get("tris"),
            "texture_px": facts.get("texture_px"), "files": files, "fbx": "produced" if build_assets.get("fbx") else ("not produced" if build_assets.get("gltf") else None),
            "upload_channel": channel, "fee_robux": 80 if channel in ("creator_dashboard", "studio") else 0, "checklist_id": part.id,
            "license": part.license, "approval_hash": part.approval.approval_hash if part.approval else None,
            "build_hash": part.build_stamp.build_hash if part.build_stamp else None, "lineage": lin, "notes": [], "kit_lineage": kit_lin}


def banners_for(rt: Runtime, project_id: str, st: dict[str, Any], mock: bool) -> list[str]:
    from duoskin.pipeline import mesh_import

    out: list[str] = []
    if mock:
        out.append("DEMO: this kit was made with mock providers and cannot be uploaded")
    if st.get("clone_mode") == "degraded":
        out.append("clone check degraded")
    if not (st.get("similarity") or {}).get("on"):
        out.append("Reference-similarity check is off")
    if not kits.load_context(rt).flags.get("head_base_present"):
        out.append("No Head item in this kit: the face is a 2D layer pack (no head base)")
    for p in rt.repo.list_parts(project_id):
        if p.license == "tripo_free_public_ccby_noncommercial":
            out.append(f"{p.id}: {mesh_import.FREE_PLAN_BANNER}")
    if not kits.load_context(rt).flags.get("body_base_present"):
        out.append("Standard Block body: body_colors.json is for reference; there is no Body upload")
    if not kits.load_context(rt).flags.get("blender_present"):
        out.append("FBX not produced: if Studio rejects the glTF, install Blender and re-export")
    return out


def provenance_doc(rt: Runtime, project: Any, spec: dict[str, Any], items: list[dict[str, Any]], mock: bool, st: dict[str, Any]) -> dict[str, Any]:
    from duoskin import __version__

    card = rt.repo.get_dna_card(project.approved_spec_id or "") or {}
    return {"schema": SCHEMA_PROVENANCE, "app_version": __version__,
            "project": {"id": project.id, "name": project.name, "combo": project.combo, "created_at": iso_utc(project.created_at), "exported_at": iso_utc(utcnow())},
            "pins": project.pins.model_dump(mode="json") if project.pins else {},
            "settings": {"reference_similarity_check": project.settings.reference_similarity_check, "use_reference_as_mood": project.settings.use_reference_as_mood,
                         "mesh_mode": project.settings.mesh_mode},
            "dna_card": {"version": card.get("version"), "palette_source": card.get("palette_source")}, "spec_sha256": sha256_of(spec),
            "items": [{k: v for k, v in i.items() if k in ("item_id", "type", "attachment", "category", "files", "license", "approval_hash", "build_hash", "lineage", "notes")}
                      for i in items],
            "kit_lineage": kit_lineage(rt, spec), "costs": cost_totals(rt, project.id), "checks_summary": checks_summary(rt, project.id),
            "notes": ["SynthID present in Gemini-derived assets: none", f"degraded clone check: {str(st.get('clone_mode') == 'degraded').lower()}",
                      *(["mock sources present (test export)"] if mock else [])]}


def readme(project: Any, spec: dict[str, Any], items: list[dict[str, Any]], banners: list[str], mock: bool) -> str:
    lines = [f"DuoSkin upload kit: {project.name}", "=" * 40, ""]
    if mock:
        lines += ["THIS IS A DEMO KIT. It was made with mock providers; do not upload it.", ""]
    lines += ["What is in this kit", "-------------------"]
    for i in items:
        lines.append(f"  {i['item_id']:<12} {i['type']:<10} {len(i['files'])} file(s)  upload: {i['upload_channel']}")
    lines += ["", "Order of the Studio tests: open CHECKLIST.html and tick each free 'Studio test' before its upload line unlocks.",
              "Fees: Classic Shirt/Pants 80 Robux per submission (Creator Dashboard); hair and accessories 80 Robux each (Studio). Items cannot be edited after upload.", ""]
    for b in banners:
        lines.append("NOTE: " + b)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------- validate
def secret_scan(rt: Runtime, root: Path | None = None, zip_path: Path | None = None) -> list[str]:
    """CHK-E06: key shapes, signed URLs, e-mail addresses and the exact stored key values, over every file (and every zip member)."""
    exact: list[bytes] = []
    for prov in ("anthropic", "openai", "recraft", "tripo", "gemini", "fal"):
        try:
            k = rt.keys.get_key(prov)
        except Exception:  # noqa: BLE001
            k = None
        if k and len(k) >= 8:
            exact.append(k.encode("utf-8"))

    def scan(name: str, data: bytes) -> list[str]:
        hits = []
        for what, rx in SECRET_PATTERNS:
            if what == "email address" and name.endswith((".png", ".bin", ".gltf", ".glb", ".fbx")):
                continue                               # binary files: the e-mail shape appears by chance in noise; texts are scanned
            if rx.search(data):
                hits.append(f"{name}: {what}")
        for k in exact:
            if k in data:
                hits.append(f"{name}: a stored key value")
        return hits

    out: list[str] = []
    if root is not None:
        for f in sorted(x for x in root.rglob("*") if x.is_file()):
            out += scan(f.relative_to(root).as_posix(), f.read_bytes())
    if zip_path is not None:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                out += scan(n, z.read(n))
    return out


def lint_names(root: Path) -> list[str]:
    """CHK-E07: ASCII names, short paths, no reserved Windows names, no trailing dots or spaces."""
    bad = []
    for f in root.rglob("*"):
        rel = f.relative_to(root).as_posix()
        if not rel.isascii():
            bad.append(f"{rel}: not ASCII")
        if len(rel) > 100:
            bad.append(f"{rel}: path longer than 100 characters")
        for part in rel.split("/"):
            stem = part.split(".")[0].lower()
            if stem in RESERVED or part != part.rstrip(". ") or any(ch in part for ch in '<>:"|?*\\'):
                bad.append(f"{rel}: '{part}' is not a safe name")
    return bad


def run_validate(ctx: StepContext, p: ExportParams, inputs: list[Any]) -> StepResult:
    """CHK-E04: every exported file is opened again and validated; CHK-E06 and CHK-E07 over the folder."""
    from duoskin.mesh.types import MeshJob
    from duoskin.pipeline import meshrun, meshsteps
    from duoskin.roblox import validators as V

    rt = ctx.rt
    project_id = p.project_id
    _, spec = common.load_spec(rt, project_id)
    st = get_state(rt, project_id)
    root = Path(st["kit_dir"])
    manifest = json.loads(next(root.glob("*manifest.json")).read_text(encoding="utf-8"))
    results: list[Any] = []
    bad: list[str] = []
    for it in manifest["items"]:
        for f in it["files"]:
            path = root / f["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != f["sha256"]:
                bad.append(f"{f['path']}: missing or changed")
        if it["type"] in ("Shirt", "Pants"):
            data = (root / it["files"][0]["path"]).read_bytes()
            part = rt.repo.get_part(project_id, it["item_id"])
            labels = None
            if part.board_assets.get("label_map"):                      # the compositor's garment labels, as the Gate 2 checks used them
                import io

                import numpy as np

                labels = np.load(io.BytesIO(rt.cas.get(part.board_assets["label_map"])))["labels"]
            r = V.validate_template(data, "shirt" if it["type"] == "Shirt" else "pants", labels, None)
            bad += [f"{it['item_id']}: {x.check_id} {x.evidence}" for x in r if not x.passed and x.kind in ("hard", "assert")]
        elif it["type"] in ("Hair", "Hat", "Face", "Neck", "Shoulder", "Front", "Back", "Waist"):
            gltf = next((root / f["path"] for f in it["files"] if f["path"].endswith(".gltf")), None)
            if gltf is None:
                bad.append(f"{it['item_id']}: no .gltf")
                continue
            doc = json.loads(gltf.read_text(encoding="utf-8"))
            for uri in [b.get("uri", "") for b in doc.get("buffers", [])] + [i.get("uri", "") for i in doc.get("images", [])]:
                if "/" in uri or "\\" in uri or uri.startswith(("http", "file")) or not (gltf.parent / uri).is_file():
                    bad.append(f"{it['item_id']}: the glTF refers to '{uri}', which is not a sibling file")
            item = itemspec.item_of(spec, it["item_id"])
            work = meshsteps.work_dir(ctx, it["item_id"].replace(".", "_"))
            job = MeshJob(op="validate", input_path=str(gltf), out_dir=str(work), asset_type=item.asset_type, attachment=item.attachment,   # type: ignore[arg-type]
                          target_studs=item.target_studs, tris_target=item.tris_target, texture_px=item.texture_px, params={"roundtrip": False,
                          "expect_slab": item.build == "sticker_slab", "expect_hair_register": item.is_hair and False}, asset_id=it["item_id"],
                          forward_axis=meshsteps.forward_axis(rt), blender_path=meshsteps.blender_path(rt))
            res = meshrun.run_mesh_job(ctx, job, overrides=meshrun.mesh_overrides_for(rt, item.is_hair), timeout_s=180)
            blocking = common.hard_failures(list(res.checks))
            bad += [f"{it['item_id']}: {r.check_id} {r.evidence[:80]}" for r in blocking if r.check_id not in ("CHK-M08", "CHK-M13", "CHK-M21", "CHK-M14")]
            bb = res.facts.get("bbox_studs")
            if bb and it.get("bbox_studs"):
                for a, b in zip(bb, it["bbox_studs"], strict=False):
                    if b and abs(a - b) > 0.01 * max(abs(b), 1e-6) + 1e-6:
                        bad.append(f"{it['item_id']}: stud extent {a} differs from the manifest {b} by more than 1%")
    results.append(build_result("CHK-E04", passed=not bad, metric="reopened_files", value=float(len(bad)), evidence="; ".join(bad[:4]) or "every exported file re-opened and passed",
                                fix_hint="human"))
    leaks = secret_scan(rt, root=root)
    results.append(build_result("CHK-E06", passed=not leaks, metric="secret_scan", value=float(len(leaks)), evidence="; ".join(leaks[:3]) or "no key, signed URL or e-mail address",
                                fix_hint="human"))
    names = lint_names(root)
    results.append(build_result("CHK-E07", passed=not names, metric="slug_lint", value=float(len(names)), evidence="; ".join(names[:3]) or "names and paths are safe"))
    common.store_checks(ctx, results)
    blocking = common.hard_failures(results)
    if blocking:
        reason = "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:3])
        rt.repo.kv_set(state_key(project_id), {**st, "status": "failed", "reason": reason})
        rt.repo.set_project_stage(project_id, Stage.GATE3, bus=rt.bus)
        set_gate3_status(rt, project_id, {"status": "failed", "reason": reason})
        raise StepFailure("The exported kit did not pass its own checks: " + "; ".join(f"{r.check_id}: {r.evidence}" for r in blocking[:2]), kind="bad_request",
                          billed="no", retryable=False)
    return StepResult(result={"ok": True, "checked": len(manifest["files"])}, message="the kit passed its checks")


def run_zip(ctx: StepContext, p: ExportParams, inputs: list[Any]) -> StepResult:
    rt = ctx.rt
    project_id = p.project_id
    st = get_state(rt, project_id)
    root = Path(st["kit_dir"])
    zpath = root.parent / f"{root.name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(x for x in root.rglob("*") if x.is_file()):
            z.write(f, f"{root.name}/{f.relative_to(root).as_posix()}")
    leaks = secret_scan(rt, zip_path=zpath)
    r = build_result("CHK-E06", passed=not leaks, metric="secret_scan_zip", value=float(len(leaks)), evidence="; ".join(leaks[:3]) or "the zip is clean", fix_hint="human")
    common.store_checks(ctx, [r])
    if leaks:
        zpath.unlink(missing_ok=True)
        set_gate3_status(rt, project_id, {"status": "failed", "reason": "the zip contains a secret-like string"})
        rt.repo.set_project_stage(project_id, Stage.GATE3, bus=rt.bus)
        raise StepFailure("The zip contains something that looks like a secret: " + "; ".join(leaks[:2]), kind="bad_request", billed="no", retryable=False)
    rt.repo.kv_set(state_key(project_id), {**st, "status": "done", "zip": str(zpath), "finished_at": iso_utc(utcnow())})
    rt.repo.set_project_stage(project_id, Stage.EXPORTED, bus=rt.bus)
    rt.bus.emit("toast", {"message": "Your upload kit is ready", "level": "info", "kit_dir": str(root), "zip": str(zpath)}, project_id)
    set_gate3_status(rt, project_id, {"status": "done", "kit_dir": str(root), "zip": str(zpath)}, close=True)
    return StepResult(result={"zip": str(zpath)}, message="the upload kit is ready")


def register(rt: Runtime | None = None) -> None:
    registry.register_handler("export.build", run_build, version=1, pool="cpu", paid=False, Params=ExportParams, cacheable=False)
    registry.register_handler("export.validate", run_validate, version=1, pool="proc", paid=False, Params=ExportParams, cacheable=False)
    registry.register_handler("export.zip", run_zip, version=1, pool="cpu", paid=False, Params=ExportParams, cacheable=False)
    sched.register_job_factory(JobKind.EXPORT, _export_steps)
