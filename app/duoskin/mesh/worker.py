"""The mesh subprocess boundary (APP_SPEC 5.1, 10.9): ``python -m duoskin.mesh.worker job.json``.

Every mesh parse, repair, validation and render runs here, never in the server process: a native crash, a runaway allocation or
a hang can only kill this child. ``run_job`` is the parent-side entry point: it writes ``job.json``, starts the worker with a
timeout (default 300 s), a memory ceiling and a psutil tree kill, and reads back ``result.json`` (``MeshResult``). A job that
crashes, times out or writes no result still returns a ``MeshResult`` (``ok=False``, ``error`` set, a failed CHK-M01) so callers
never need a try/except.

Ops (``MeshJob.op``) and their ``params``:

``import``        parse and describe a file (CHK-M01). No files written.
``repair``        import -> repair -> export (glTF set + GLB archive + FBX when Blender exists) -> full validation -> renders.
                  params: ``stem``, ``want_fbx`` (True), ``render`` (True), ``view_size`` (384), ``scale_mode``, ``placement``,
                  ``anchor_offset``, ``hair_register`` (True for Hair), ``hair_mask_path`` / ``hair_bbox`` (alignment hint without
                  a head cube), ``guide_rgb`` (list of RGB), ``roundtrip`` (False).
``register_hair`` ``repair`` for a Tripo/manual hair mesh with the head cube cut out (hair.register) forced on.
``validate``      CHK-M01..M14, M18, M20, M21 on an exported .gltf/.glb. params: ``roundtrip``, ``hair_mesh_path``, ``expect_slab``.
``render_views``  front, left, back, right, top, three_quarter renders + judge sheet + on-body scale render.
``slab``          build_slab from ``params.art_path`` (size_studs, thickness, back, kind ...) -> export -> validate.
``primitive``     primitives.build from ``params.kind`` / ``params.params`` / ``params.palette`` -> export -> validate.
``fit_hair``      assemble a kit hair style (modules, palette recolour) in the HairAttachment frame -> repair -> export -> validate.
``flip_lr``       the tile action "Flip left/right (I checked)": mirror an exported mesh (``input_path``), re-export and re-validate.

``ok`` means "the op ran to the end"; the gate verdict comes from ``checks`` (``duoskin.checks.model.gate_verdict``).
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from duoskin import winplat
from duoskin.checks.model import CheckResult, not_run
from duoskin.mesh.types import MeshData, MeshError, MeshJob, MeshResult

PYMESHLAB_NOTE = "pymeshlab is not installed: the built-in UV-preserving quadric decimation was used"


# --------------------------------------------------------------------------------------------------------------------
# optional pymeshlab decimator (imported ONLY here, only inside the subprocess)
# --------------------------------------------------------------------------------------------------------------------
def write_obj(mesh: MeshData, path: Path, texture_name: str = "tex.png") -> None:
    """Minimal OBJ + MTL writer with per-vertex UVs (``vt`` = ``v`` flipped to OBJ's v-up convention)."""
    from duoskin.mesh.types import uv_gltf_to_gl

    uv = uv_gltf_to_gl(mesh.uv)
    lines = [f"mtllib {path.stem}.mtl", "usemtl m0"]
    lines += [f"v {x:.9g} {y:.9g} {z:.9g}" for x, y, z in mesh.vertices]
    lines += [f"vt {u:.9g} {v:.9g}" for u, v in uv]
    lines += [f"f {a + 1}/{a + 1} {b + 1}/{b + 1} {c + 1}/{c + 1}" for a, b, c in mesh.faces]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".mtl").write_text(f"newmtl m0\nKd 1 1 1\nmap_Kd {texture_name}\n", encoding="utf-8")


def read_obj(path: Path, texture: Image.Image | None) -> MeshData:
    """Read an OBJ (v / vt / f v/vt) back into a seam-split MeshData (one vertex per unique position+uv pair)."""
    from duoskin.mesh.types import uv_gl_to_gltf

    v, vt, faces = [], [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("v "):
                v.append([float(x) for x in line.split()[1:4]])
            elif line.startswith("vt "):
                vt.append([float(x) for x in line.split()[1:3]])
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    parts = tok.split("/")
                    idx.append((int(parts[0]) - 1, int(parts[1]) - 1 if len(parts) > 1 and parts[1] else -1))
                for k in range(1, len(idx) - 1):
                    faces.append([idx[0], idx[k], idx[k + 1]])
    pairs = {}
    verts, uvs, out_faces = [], [], []
    for tri in faces:
        row = []
        for vi, ti in tri:
            key = (vi, ti)
            if key not in pairs:
                pairs[key] = len(verts)
                verts.append(v[vi])
                uvs.append(vt[ti] if ti >= 0 else [0.0, 0.0])
            row.append(pairs[key])
        out_faces.append(row)
    return MeshData(np.array(verts), np.array(out_faces, np.int64), uv_gl_to_gltf(np.array(uvs)), texture)


def pymeshlab_decimator():
    """A ``(mesh, target) -> (mesh, report)`` wrapper around pymeshlab's texture-aware quadric edge collapse, or None when
    pymeshlab cannot be imported. Any failure at run time falls back to the built-in decimator (and says so)."""
    try:
        import pymeshlab  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency, any import problem means "not available"
        return None
    from duoskin.mesh.decimate import decimate as builtin

    def run(mesh: MeshData, target: int) -> tuple[MeshData, dict[str, Any]]:
        import pymeshlab

        try:
            with tempfile.TemporaryDirectory(prefix="duoskin_pml_", ignore_cleanup_errors=True) as td:
                tdp = Path(td)
                from duoskin.mesh import texture as tx

                tx.as_pil(mesh.texture).convert("RGB").save(tdp / "tex.png")
                write_obj(mesh, tdp / "in.obj")
                ms = pymeshlab.MeshSet()
                ms.load_new_mesh(str(tdp / "in.obj"))
                ms.meshing_decimation_quadric_edge_collapse_with_texture(
                    targetfacenum=int(target), preserveboundary=True, boundaryweight=1.0, preservenormal=True, optimalplacement=True,
                    planarquadric=True, extratcoordweight=1.0)
                ms.save_current_mesh(str(tdp / "out.obj"), save_vertex_color=False, save_wedge_texcoord=True, save_wedge_normal=False)
                out = read_obj(tdp / "out.obj", mesh.texture)
                out.meta = dict(mesh.meta)
                return out, {"method": "pymeshlab_quadric_edge_collapse_with_texture", "tris_before": mesh.n_tris, "tris_after": out.n_tris,
                             "reached_target": out.n_tris <= target, "collapses": mesh.n_tris - out.n_tris, "uv_preserved": True}
        except Exception:  # noqa: BLE001
            return builtin(mesh, target)

    return run


# --------------------------------------------------------------------------------------------------------------------
# job execution
# --------------------------------------------------------------------------------------------------------------------
def _slug(text: str, default: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", text or "").strip("-")[:40]
    return s or default


def _json_safe(obj: Any) -> Any:
    def default(o: Any) -> Any:
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, np.bool_):
            return bool(o)
        return str(o)

    return json.loads(json.dumps(obj, default=default))


def _failed(job: MeshJob, error: str, message: str, check_id: str = "CHK-M01", fm: list[str] | None = None, extra_checks: list[CheckResult] | None = None) -> MeshResult:
    chk = CheckResult(check_id=check_id, fm_ids=fm or ["SYS-04", "ACC-12", "MESH-14"], kind="hard", passed=False, metric="job", threshold="completes",
                      evidence=message, fix_hint="human")
    return MeshResult(ok=False, checks=[chk] + list(extra_checks or []), messages=[message], error=error)


def _approved(job: MeshJob) -> dict[str, str]:
    return {k: v for k, v in job.approved_views.items() if v}


def _guide_rgb(params: dict[str, Any]) -> tuple[tuple[int, int, int], ...]:
    from duoskin.mesh.hair import GUIDE_GREY

    raw = params.get("guide_rgb")
    if not raw:
        return (GUIDE_GREY,)
    if isinstance(raw[0], (int, float)):
        raw = [raw]
    return tuple(tuple(int(c) for c in g) for g in raw)


def _hair_options(job: MeshJob, mq) -> Any:
    from duoskin.mesh import hair

    p = job.params
    bbox = None
    if p.get("hair_bbox"):
        lo, hi = p["hair_bbox"]
        bbox = (tuple(float(x) for x in lo), tuple(float(x) for x in hi))
    elif p.get("hair_mask_path"):
        with Image.open(p["hair_mask_path"]) as im:
            rgba = np.asarray(im.convert("RGBA"))
        mask = rgba[..., 3] > 127 if rgba[..., 3].min() < 250 else (np.abs(rgba[..., :3].astype(int) - 255).max(axis=2) > 14)
        bbox = hair.bbox_from_guide_mask(mask)
    return hair.HairRegisterOptions(guide_rgb=_guide_rgb(p), tris_target=job.tris_target, mannequin=mq, target_bbox=bbox,
                                    head_studs=float(p.get("head_studs", 1.2)), z_centre_studs=float(p.get("z_centre_studs", -0.1)))


def _stem(job: MeshJob) -> str:
    return _slug(str(job.params.get("stem") or job.asset_id), "accessory")


def _facts_summary(vfacts: dict[str, Any], mesh_facts: dict[str, Any]) -> dict[str, Any]:
    """The facts every caller expects (APP_SPEC 10.9) on top of the detailed ones."""
    tris = int(vfacts.get("tris", 0))
    out = dict(vfacts)
    out.update({
        "bbox_studs": vfacts.get("bbox_studs"), "box_margins": vfacts.get("box_margins"), "surface_area": vfacts.get("surface_area"),
        "coplanar_frac": round(vfacts.get("coplanar_intersections", 0) / tris, 5) if tris else None, "centre_offset": vfacts.get("centre_offset"),
        "normals_out": vfacts.get("normals_out"), "watertight": vfacts.get("watertight"), "shells": vfacts.get("shells"),
        "texture_px": max(vfacts["tex_size"]) if vfacts.get("tex_size") else None,
        "orientation": vfacts.get("orientation"), "mirrored": vfacts.get("mirrored", False),
    })
    out.update(mesh_facts)
    return out


def _save_renders(mesh: MeshData, job: MeshJob, out_dir: Path, mq, files: dict[str, str], size: int) -> list[str]:
    """judge views + sheet + the on-body scale render. Returns messages."""
    from duoskin.render import sheets

    msgs: list[str] = []
    rdir = out_dir / "renders"
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = sheets.RenderManifest()
    views = sheets.render_mesh_views(mesh, size=size, manifest=manifest)
    for name, img in views.items():
        p = rdir / f"{name}.png"
        img.save(p)
        files[f"renders/{name}"] = str(p)
    sheet = sheets.mesh_judge_sheet(views)
    p = rdir / "judge_sheet.png"
    sheet.save(p)
    files["renders/judge_sheet"] = str(p)
    try:
        sc = sheets.scale_render(mq, job.asset_type, job.attachment or None, target_studs=job.target_studs, mesh=mesh, size=1024, manifest=manifest)
        p = rdir / "scale_front.png"
        sc.save(p)
        files["renders/scale_front"] = str(p)
        scs = sheets.scale_render(mq, job.asset_type, job.attachment or None, target_studs=job.target_studs, mesh=mesh, view="left", size=1024, manifest=manifest)
        p = rdir / "scale_left.png"
        scs.save(p)
        files["renders/scale_left"] = str(p)
    except Exception as exc:  # noqa: BLE001
        msgs.append(f"the on-body scale render could not be made: {type(exc).__name__}: {exc}")
    sheets.assert_manifest(manifest, [e.name for e in manifest.entries])
    return msgs


def _export_and_validate(mesh: MeshData, job: MeshJob, out_dir: Path, mq, *, expect_slab: bool = False, expect_hair: bool = False, code_built: bool = False,
                         pre_checks: list[CheckResult] | None = None, extra_facts: dict[str, Any] | None = None,
                         messages: list[str] | None = None, degraded: list[str] | None = None) -> MeshResult:
    from duoskin.mesh import export
    from duoskin.mesh.validate import ValidateContext, validate_file

    messages = list(messages or [])
    degraded = list(degraded or [])
    p = job.params
    stem = _stem(job)
    exp_mesh = export.to_export_frame_mesh(mesh, job.forward_axis if job.forward_axis else "+Z")
    exp_mesh.meta["asset_id"] = job.asset_id
    files, msgs = export.export_all(exp_mesh, out_dir, stem, blender=job.blender_path or None, want_fbx=bool(p.get("want_fbx", True)))
    messages += msgs
    ctx = ValidateContext(asset_type=job.asset_type, attachment=job.attachment, target_studs=job.target_studs, approved_views=_approved(job), mannequin=mq,
                          forward_axis=job.forward_axis or "+Z", expect_slab=expect_slab, expect_hair_register=expect_hair, asset_id=job.asset_id,
                          code_built=code_built)
    hair_path = p.get("hair_mesh_path")
    if hair_path and Path(hair_path).is_file():
        from duoskin.mesh import load

        hl = load.load_gltf(hair_path)
        from duoskin.mesh.gltf_io import gltf_structure_facts

        hx = gltf_structure_facts(hair_path).get("extras", {})
        hl.mesh.meta["attachment_offset"] = hx.get("attachment_offset", [0, 0, 0])
        ctx.hair_mesh = hl.mesh
    vfacts, checks = validate_file(files["gltf"], ctx)
    checks = list(pre_checks or []) + checks
    if p.get("roundtrip"):
        checks.append(export.gltf_roundtrip(out_dir / "_m19"))
        checks.append(export.fbx_roundtrip(out_dir / "_m19", job.blender_path))
    if not (job.params.get("want_fbx", True)) or "fbx" not in files:
        vfacts["fbx_produced"] = "fbx" in files
    facts = _facts_summary(vfacts, extra_facts or {})
    facts["stem"] = stem
    facts["asset_id"] = job.asset_id
    facts["licence"] = job.licence
    facts["frame"] = {"units": "studs", "up": "+Y", "front": exp_mesh.meta.get("front", "+Z")}
    facts["attachment_offset"] = np.asarray(exp_mesh.meta.get("attachment_offset", [0, 0, 0])).tolist()
    if p.get("render", True):
        try:
            messages += _save_renders(mesh, job, out_dir, mq, files, int(p.get("view_size", 384)))
        except Exception as exc:  # noqa: BLE001
            messages.append(f"renders could not be made: {type(exc).__name__}: {exc}")
    return MeshResult(ok=True, files=files, facts=_json_safe(facts), checks=checks, messages=messages, degraded=degraded)


def _op_import(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import load
    from duoskin.mesh import repair as rep

    loaded = load.load_mesh(job.input_path, blender=job.blender_path or None, workdir=out_dir)
    topo = rep.topology(loaded.mesh)
    facts = {**loaded.facts, "topology": topo, "tris": topo["tris"], "bbox_extent": loaded.mesh.extents.round(5).tolist(),
             "has_texture": loaded.mesh.texture is not None, "sha256": load.sha256_file(job.input_path)}
    chk = CheckResult(check_id="CHK-M01", fm_ids=["SYS-04", "ACC-12", "MESH-14"], kind="hard", passed=True, metric="load_contract", threshold="parsed",
                      evidence=f"{loaded.facts.get('file_type', '?')} parsed: {topo['tris']} triangles, {topo['shells']} shells; "
                               + ("; ".join(loaded.messages) or "no conversion needed"))
    return MeshResult(ok=True, facts=_json_safe(facts), checks=[chk], messages=loaded.messages)


def _repair_pipeline(job: MeshJob, out_dir: Path, *, hair: bool) -> MeshResult:
    from duoskin.mesh import hair as hair_mod
    from duoskin.mesh import load
    from duoskin.mesh import repair as rep
    from duoskin.render import avatar

    p = job.params
    mq = avatar.load_mannequin(job.mannequin)
    loaded = load.load_mesh(job.input_path, blender=job.blender_path or None, workdir=out_dir)
    dec = pymeshlab_decimator()
    ropts = rep.RepairOptions(
        asset_type=job.asset_type, attachment=job.attachment, target_studs=None if hair else tuple(job.target_studs), tris_target=job.tris_target,
        texture_px=job.texture_px, approved_views=_approved(job), placement="keep" if hair else str(p.get("placement", "auto")),
        anchor_offset=tuple(p["anchor_offset"]) if p.get("anchor_offset") else None, scale_mode="none" if hair else str(p.get("scale_mode", "fit")),
        orient=bool(p.get("orient", True)), decimator=dec, mannequin=mq)
    if hair:
        ropts.tris_target = max(200, job.tris_target - 200)         # headroom for the faces the head cut creates
    rr = rep.repair_mesh(loaded.mesh, ropts)
    messages = list(loaded.messages) + rr.messages
    degraded = list(rr.degraded)
    mesh = rr.mesh
    extra: dict[str, Any] = {"repair": rr.report, "source": loaded.facts, "decimator": "pymeshlab" if dec else "builtin_uv_qem"}
    if rr.orientation is not None:
        extra["orientation"] = rr.orientation.facts()
        extra["mirrored"] = rr.orientation.mirrored
    if hair:
        hopts = _hair_options(job, mq)
        hr = hair_mod.register_hair(mesh, hopts)
        mesh = hr.mesh
        extra["hair_register"] = hr.facts
        messages += hr.messages
    res = _export_and_validate(mesh, job, out_dir, mq, expect_hair=hair, pre_checks=rr.checks, extra_facts=extra, messages=messages, degraded=degraded)
    return res


def _op_slab(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import repair as rep
    from duoskin.mesh import slab
    from duoskin.render import avatar

    p = job.params
    mq = avatar.load_mannequin(job.mannequin)
    with Image.open(p["art_path"]) as im:
        art = im.convert("RGBA")
    size = float(p.get("size_studs") or max(job.target_studs[:2]))
    sres = slab.build_slab(art, size_studs=size, thickness=float(p.get("thickness", 0.1)), back=str(p.get("back", "plain")),
                           tris_budget=int(p.get("tris_budget", min(job.tris_target, 2800))), kind=str(p.get("kind", "sticker_slab")),
                           back_rgb=tuple(p["back_rgb"]) if p.get("back_rgb") else None, texture_px=job.texture_px)
    ropts = rep.RepairOptions(asset_type=job.asset_type, attachment=job.attachment, target_studs=None, tris_target=job.tris_target, texture_px=job.texture_px,
                              approved_views={}, placement=str(p.get("placement", "auto")), scale_mode="none", orient=False, mannequin=mq)
    rr = rep.repair_mesh(sres.mesh, ropts)
    rr.mesh.meta.update({k: v for k, v in sres.mesh.meta.items() if k in ("kind", "slab")})
    return _export_and_validate(rr.mesh, job, out_dir, mq, expect_slab=True, code_built=True, pre_checks=rr.checks, extra_facts={"slab": sres.facts, "repair": rr.report},
                                messages=sres.messages + rr.messages, degraded=rr.degraded)


def _op_primitive(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import primitives
    from duoskin.mesh import repair as rep
    from duoskin.render import avatar

    p = job.params
    mq = avatar.load_mannequin(job.mannequin)
    pal = p.get("palette")
    if isinstance(pal, list):
        pal = tuple(int(c) for c in pal)
    elif isinstance(pal, dict):
        pal = {k: tuple(int(c) for c in v) for k, v in pal.items()}
    mesh = primitives.build(str(p.get("kind", "bead")), dict(p.get("params", {})), pal, texture_px=job.texture_px)
    ropts = rep.RepairOptions(asset_type=job.asset_type, attachment=job.attachment, target_studs=tuple(job.target_studs) if p.get("fit", True) else None,
                              tris_target=job.tris_target, texture_px=job.texture_px, approved_views={}, placement=str(p.get("placement", "auto")),
                              scale_mode="fit" if p.get("fit", True) else "none", orient=False, mannequin=mq)
    rr = rep.repair_mesh(mesh, ropts)
    rr.mesh.meta["primitive_kind"] = mesh.meta.get("primitive_kind", "")
    return _export_and_validate(rr.mesh, job, out_dir, mq, code_built=True, pre_checks=rr.checks, extra_facts={"primitive": mesh.meta.get("primitive_kind"), "repair": rr.report},
                                messages=rr.messages, degraded=rr.degraded)


def _op_validate(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import export, load
    from duoskin.mesh.gltf_io import gltf_structure_facts
    from duoskin.mesh.validate import ValidateContext, validate_file
    from duoskin.render import avatar

    p = job.params
    mq = avatar.load_mannequin(job.mannequin)
    ctx = ValidateContext(asset_type=job.asset_type, attachment=job.attachment, target_studs=job.target_studs, approved_views=_approved(job), mannequin=mq,
                          forward_axis=job.forward_axis or "+Z", expect_slab=bool(p.get("expect_slab", False)),
                          expect_hair_register=bool(p.get("expect_hair_register", False)), asset_id=job.asset_id)
    hair_path = p.get("hair_mesh_path")
    if hair_path and Path(hair_path).is_file():
        hl = load.load_gltf(hair_path)
        hl.mesh.meta["attachment_offset"] = gltf_structure_facts(hair_path).get("extras", {}).get("attachment_offset", [0, 0, 0])
        ctx.hair_mesh = hl.mesh
    facts, checks = validate_file(job.input_path, ctx)
    if p.get("roundtrip"):
        checks.append(export.gltf_roundtrip(out_dir / "_m19"))
        checks.append(export.fbx_roundtrip(out_dir / "_m19", job.blender_path))
    return MeshResult(ok=True, facts=_json_safe(_facts_summary(facts, {})), checks=checks)


def _op_render_views(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import load
    from duoskin.mesh.gltf_io import gltf_structure_facts
    from duoskin.render import avatar

    mq = avatar.load_mannequin(job.mannequin)
    loaded = load.load_gltf(job.input_path)
    ex = gltf_structure_facts(job.input_path).get("extras", {})
    loaded.mesh.meta["attachment_offset"] = ex.get("attachment_offset", [0, 0, 0])
    files: dict[str, str] = {}
    msgs = _save_renders(loaded.mesh, job, out_dir, mq, files, int(job.params.get("view_size", 512)))
    return MeshResult(ok=True, files=files, facts={"views": sorted(k for k in files if k.startswith("renders/")), "tris": loaded.mesh.n_tris},
                      checks=[], messages=msgs)


def _op_fit_hair(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import hair as hair_mod
    from duoskin.mesh import repair as rep
    from duoskin.render import avatar

    p = job.params
    mq = avatar.load_mannequin(job.mannequin)
    fit = hair_mod.fit_kit_hair(style_path=p.get("style_path") or job.input_path, module_paths=list(p.get("module_paths", [])),
                                palette=p.get("palette") or {}, adjustments=list(p.get("adjustments", [])), mannequin=mq)
    ropts = rep.RepairOptions(asset_type="Hair", attachment=job.attachment or "HairAttachment", target_studs=None, tris_target=job.tris_target,
                              texture_px=job.texture_px, approved_views=_approved(job), placement="keep", scale_mode="none", orient=False)
    rr = rep.repair_mesh(fit.mesh, ropts)
    pre = rr.checks + fit.checks
    return _export_and_validate(rr.mesh, job, out_dir, mq, expect_hair=True, code_built=not _approved(job), pre_checks=pre, extra_facts={"hair_fit": fit.facts, "repair": rr.report},
                                messages=fit.messages + rr.messages, degraded=rr.degraded)


def _op_flip_lr(job: MeshJob, out_dir: Path) -> MeshResult:
    from duoskin.mesh import load, orient
    from duoskin.mesh.gltf_io import gltf_structure_facts
    from duoskin.render import avatar

    mq = avatar.load_mannequin(job.mannequin)
    loaded = load.load_gltf(job.input_path)
    extras = gltf_structure_facts(job.input_path).get("extras", {}) or {}
    mesh = loaded.mesh
    front = extras.get("front", "+Z")
    att = np.asarray(extras.get("attachment_offset", [0.0, 0.0, 0.0]), float)
    if front not in ("+Z", "Z"):
        mesh.vertices = orient.from_export_frame(mesh.vertices, front)
        att = orient.from_export_frame(att[None, :], front)[0]
    mesh.meta["attachment_offset"] = att.tolist()
    for key in ("attachment", "asset_type", "kind", "slab", "hair_register"):
        if key in extras:
            mesh.meta[key] = extras[key]
    flipped = orient.flip_lr(mesh)
    return _export_and_validate(flipped, job, out_dir, mq, expect_slab=extras.get("kind") in ("sticker_slab", "hair_clip_slab"),
                                expect_hair=bool(extras.get("hair_register")), code_built=not _approved(job),
                                extra_facts={"flipped_lr": True}, messages=["flipped left/right at your request (the decision is logged)"])


_OPS = {
    "import": _op_import,
    "validate": _op_validate,
    "render_views": _op_render_views,
    "slab": _op_slab,
    "primitive": _op_primitive,
    "fit_hair": _op_fit_hair,
    "flip_lr": _op_flip_lr,
}


def execute(job: MeshJob) -> MeshResult:
    """Run one job in THIS process (the worker ``main`` calls it; tests may too). Never raises."""
    out_dir = Path(job.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if job.op in ("repair", "register_hair"):
            hair = job.asset_type == "Hair" and bool(job.params.get("hair_register", True)) or job.op == "register_hair"
            return _repair_pipeline(job, out_dir, hair=hair)
        return _OPS[job.op](job, out_dir)
    except MeshError as exc:
        return _failed(job, exc.code, exc.message)
    except MemoryError:
        return _failed(job, "out_of_memory", "The model is too large to process: re-export it with fewer triangles (a face limit of about 3000).")
    except Exception as exc:  # noqa: BLE001 - the job boundary: every failure becomes a failed result
        return MeshResult(ok=False, checks=[not_run("CHK-M01", "hard", f"{type(exc).__name__}: {exc}", fm_ids=["SYS-04"])], error="worker_exception",
                          messages=[f"The 3D worker hit an unexpected error: {type(exc).__name__}: {exc}", traceback.format_exc()[-1200:]])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m duoskin.mesh.worker job.json", file=sys.stderr)
        return 2
    faulthandler.enable()
    job_path = Path(argv[0])
    with open(job_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    job = MeshJob.model_validate(raw)
    result_path = Path(job.result_path) if job.result_path else Path(job.out_dir) / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    res = execute(job)
    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(res.model_dump_json(indent=1), encoding="utf-8")
    winplat.replace_with_retry(tmp, result_path)   # antivirus can hold the fresh file for a moment
    return 0


# --------------------------------------------------------------------------------------------------------------------
# parent side
# --------------------------------------------------------------------------------------------------------------------
def run_job(job: MeshJob, *, timeout_s: float = 300.0, python: str | None = None, max_rss_mb: float | None = 6144.0) -> MeshResult:
    """Run ``job`` in a child process and return its ``MeshResult``.

    The child gets a timeout (default 300 s), a memory ceiling and a psutil tree kill. A crash, a timeout or a missing result
    file never raises: the result has ``ok=False``, ``error`` in ``{"timeout", "out_of_memory", "worker_crashed"}`` and a plain
    sentence in ``messages``.
    """
    from duoskin.mesh.proc import run_process

    out_dir = Path(job.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = Path(job.result_path) if job.result_path else out_dir / "result.json"
    if result_path.exists():
        result_path.unlink()
    job_path = out_dir / "job.json"
    job_path.write_text(job.model_dump_json(indent=1), encoding="utf-8")
    app_root = str(Path(__file__).resolve().parents[2])
    env = {"PYTHONPATH": app_root + os.pathsep + os.environ.get("PYTHONPATH", ""), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    res = run_process([python or sys.executable, "-m", "duoskin.mesh.worker", str(job_path)], timeout_s=timeout_s, cwd=app_root, env=env,
                      max_rss_mb=max_rss_mb)
    if result_path.is_file():
        try:
            return MeshResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return _failed(job, "bad_result", f"The 3D worker wrote an unreadable result ({exc}).")
    if res.timed_out:
        return _failed(job, "timeout", f"The 3D worker took longer than {int(timeout_s)} seconds and was stopped. Try a smaller model.")
    if res.killed_for_memory:
        return _failed(job, "out_of_memory", "The 3D worker used too much memory and was stopped. Re-export the model with fewer triangles.")
    tail = (res.stderr or res.stdout)[-600:]
    return _failed(job, "worker_crashed", f"The 3D worker stopped unexpectedly (exit code {res.returncode}). {tail}".strip())


if __name__ == "__main__":
    sys.exit(main())
