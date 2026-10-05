"""Roblox rigid-accessory rules as pure functions of measured facts (FAILURE_MODES 5.5, 7.5 CHK-M01 ... M12).

``validate_accessory(mesh_facts, asset_type, attachment, scale="Classic")`` is the entry point other tracks call (it has the
signature APP_SPEC 5.4 gives for ``roblox.validators.validate_accessory``). It never touches a file or a mesh: the facts
come from ``duoskin.mesh.validate.compute_facts`` (run in the mesh worker). A fact that is missing makes its check
``ran=False`` (fail closed), never a pass.

Checks produced (the ``W`` siblings are the SOFT warning bands of the same measurement):

========== =========================================================== =====================================
CHK-M01    load: no extensions, bare file names, parsed                 HARD  SYS-04 ACC-12 MESH-14
CHK-M02    1 mesh node / primitive / material / UV set, UVs in 0-1      HARD  MESH-02
CHK-M03    triangles <= 3800 (``M03T`` soft: hair target 3600)          HARD  MESH-01 HAIR-07
CHK-M04    watertight, 2 faces per edge, no zero-area faces, normals    HARD  MESH-08
CHK-M05    shells: <= 10 [UNVERIFIED], no micro-islands (``M05W``: 8)   HARD  MESH-09
CHK-M06    texture: embedded PNG, opaque RGB, <= 2048, not flat         HARD  MESH-04 HAIR-01
CHK-M07    no COLOR_0, emissive, metal/rough/normal maps, metallic 0    HARD  MESH-03 MESH-05
CHK-M09    every vertex in the Classic box, Handle size, Classic scale  HARD  MESH-06 HAIR-02
CHK-M10    surface area <= 70 stud^2 [UNVERIFIED] (``M10W``: 60)        HARD  MESH-07
CHK-M11    coplanar <= 15%, centre <= 1 stud, scale >= 0.01 [UNVERIFIED] HARD  MESH-11
CHK-M12    sparse bounds: 6-view coverage, spike test                   HARD  MESH-10
========== =========================================================== =====================================

What Roblox's creator-docs (commit 0b817b5c, 2026-09-26) actually give: the 4000-triangle and 2048-pixel limits, the size boxes, the
attachment names (``avatar/rigid-accessories/specifications.md``), "alpha below 255 fails" for the colour map and the existence of the
sparse-geometry, centring, thin-axis, coplanar, surface-area and island checks (``marketplace/validation-system.md``). The docs give NO
number for the surface area, the coplanar share, the centring distance, the minimum scale or the shell count: those figures are marked
UNVERIFIED (``limits.json -> status_overrides``, shown as UNVERIFIED in every ``threshold`` text) and Studio's validator is the authority.
"""
from __future__ import annotations

import math
from typing import Any

from duoskin.checks.model import CheckResult, not_run
from duoskin.roblox import limits

Facts = dict[str, Any]


def _missing(check_id: str, kind: str, keys: list[str], facts: Facts, fm: list[str]) -> CheckResult | None:
    lacking = [k for k in keys if facts.get(k) is None]
    if lacking:
        return not_run(check_id, kind, "missing facts: " + ", ".join(lacking), fm_ids=fm)  # type: ignore[arg-type]
    return None


def _res(check_id: str, fm: list[str], kind: str, passed: bool, metric: str, value: float | None, threshold: str, evidence: str,
         fix: str = "none") -> CheckResult:
    return CheckResult(check_id=check_id, fm_ids=fm, kind=kind, passed=bool(passed), metric=metric, value=value,  # type: ignore[arg-type]
                       threshold=threshold, evidence=evidence, fix_hint=fix if not passed else "none")  # type: ignore[arg-type]


def check_load(f: Facts) -> CheckResult:
    fm = ["SYS-04", "ACC-12", "MESH-14"]
    m = _missing("CHK-M01", "hard", ["ext_required", "ext_used", "bad_uris"], f, fm)
    if m:
        return m
    problems = []
    if f["ext_required"]:
        problems.append("extensionsRequired=" + ",".join(f["ext_required"]))
    if f["ext_used"]:
        problems.append("extensionsUsed=" + ",".join(f["ext_used"]))
    if f["bad_uris"]:
        problems.append("uris not bare file names: " + ",".join(map(str, f["bad_uris"])))
    mb = f.get("bytes")
    cap = limits.threshold("mesh.max_upload_mb")
    if mb is not None and mb > cap * 1024 * 1024:
        problems.append(f"file larger than {cap} MB")
    mimes = f.get("image_mimes") or []
    bad_img = [x for x in mimes if "png" not in str(x).lower()]
    if bad_img:
        problems.append("texture is not PNG: " + ",".join(map(str, bad_img)))
    return _res("CHK-M01", fm, "hard", not problems, "load_contract", float(len(problems)), "extensions empty, bare file names, PNG",
                "; ".join(problems) or "no extensions, bare file names, PNG texture", "human")


def check_structure(f: Facts) -> CheckResult:
    fm = ["MESH-02"]
    m = _missing("CHK-M02", "hard", ["mesh_nodes", "primitives", "materials", "uv_sets"], f, fm)
    if m:
        return m
    problems = []
    for key, label in (("mesh_nodes", "mesh nodes"), ("primitives", "primitives"), ("materials", "materials"), ("uv_sets", "UV sets")):
        if f[key] != 1:
            problems.append(f"{label}={f[key]} (need 1)")
    if f.get("uv_in_01") is False:
        problems.append("UVs outside 0-1")
    if f.get("uv_in_01") is None:
        return not_run("CHK-M02", "hard", "missing facts: uv_in_01", fm_ids=fm)
    return _res("CHK-M02", fm, "hard", not problems, "single_mesh_material_uv", float(len(problems)),
                "1 node, 1 primitive, 1 material, 1 UV set, UVs in 0-1", "; ".join(problems) or "1/1/1/1 and UVs in 0-1", "regenerate")


def check_triangles(f: Facts, asset_type: str) -> list[CheckResult]:
    fm = ["MESH-01", "HAIR-07"]
    m = _missing("CHK-M03", "hard", ["tris"], f, fm)
    if m:
        return [m]
    mx = int(limits.threshold("mesh.tris_max"))
    target = int(limits.threshold("hair.tris_target")) if asset_type == "Hair" else mx
    tris = int(f["tris"])
    hard = _res("CHK-M03", fm, "hard", tris <= mx, "triangles", float(tris), limits.describe("mesh.tris_max", "<="),
                f"{tris} triangles (Roblox limit 4000, we export at most {mx})", "regenerate")
    soft = _res("CHK-M03T", fm, "soft", tris <= target, "triangles_vs_target", float(tris),
                (limits.describe("hair.tris_target", "<=") if asset_type == "Hair" else limits.describe("mesh.tris_max", "<=")),
                f"{tris} triangles against the {asset_type or 'accessory'} target {target}")
    return [hard, soft]


def check_topology(f: Facts) -> CheckResult:
    fm = ["MESH-08"]
    keys = ["watertight", "boundary_edges", "nonmanifold_edges", "winding_consistent", "zero_area_faces", "normals_out", "determinant"]
    m = _missing("CHK-M04", "hard", keys, f, fm)
    if m:
        return m
    problems = []
    if f["boundary_edges"]:
        problems.append(f"{f['boundary_edges']} boundary edges (holes)")
    if f["nonmanifold_edges"]:
        problems.append(f"{f['nonmanifold_edges']} non-manifold edges")
    if not f["winding_consistent"]:
        problems.append("inconsistent winding")
    if f["zero_area_faces"]:
        problems.append(f"{f['zero_area_faces']} zero-area faces")
    nmin = float(limits.threshold("mesh.normals_out_min"))
    if f["normals_out"] < nmin:
        problems.append(f"only {f['normals_out']:.1%} of normals point outward (need {nmin:.0%})")
    if f["determinant"] <= 0:
        problems.append("negative transform determinant (mirrored)")
    tmin = float(limits.threshold("mesh.thickness_min"))
    thin = f.get("thickness_p5")
    if f.get("bbox_min_extent") is not None and f["bbox_min_extent"] < tmin:
        problems.append(f"smallest bounding-box extent {f['bbox_min_extent']:.3f} stud < {tmin}")
    if thin is not None and thin < tmin and (f.get("thin_area_frac") or 0.0) > 0.05:
        problems.append(f"{f['thin_area_frac']:.0%} of the surface is thinner than {tmin} stud")
    ok = not problems and f["watertight"]
    if not f["watertight"] and not problems:
        problems.append("not watertight")
    return _res("CHK-M04", fm, "hard", ok, "watertight_welded_copy", 1.0 if f["watertight"] else 0.0,
                "watertight, 2 faces per edge, 0 zero-area, >=99% outward, thickness >= 0.05", "; ".join(problems) or "watertight, consistent, outward",
                "regenerate")


def check_shells(f: Facts) -> list[CheckResult]:
    fm = ["MESH-09"]
    m = _missing("CHK-M05", "hard", ["shells"], f, fm)
    if m:
        return [m]
    n = int(f["shells"])
    mx, warn = int(limits.threshold("mesh.components_max")), int(limits.threshold("mesh.shells_warn"))
    micro = int(f.get("micro_shells") or 0)
    problems = []
    if n > mx:
        problems.append(f"{n} shells (limit {mx})")
    if micro:
        problems.append(f"{micro} micro-islands (tiny floating pieces: Roblox's validator rejects them on bodies; we apply the same bar here, UNVERIFIED for accessories)")
    return [_res("CHK-M05", fm, "hard", not problems, "shells", float(n), limits.describe("mesh.components_max", "<=") + "; no micro-islands",
                 "; ".join(problems) or f"{n} shells ({f.get('closed_shells', '?')} closed)", "regenerate"),
            _res("CHK-M05W", fm, "soft", n <= warn, "shells_warn", float(n), limits.describe("mesh.shells_warn", "<="), f"{n} shells")]


def check_texture(f: Facts) -> list[CheckResult]:
    fm = ["MESH-04", "HAIR-01"]
    keys = ["tex_size", "tex_mode", "tex_min_alpha", "tex_std", "alpha_mode"]
    m = _missing("CHK-M06", "hard", keys, f, fm)
    if m:
        return [m]
    warn_px, fail_px = limits.threshold("mesh.tex_warn_hard")
    problems = []
    side = max(f["tex_size"])
    if side > fail_px:
        problems.append(f"texture {f['tex_size']} is above {fail_px}")
    if f["tex_min_alpha"] < 255:
        problems.append(f"texture alpha down to {f['tex_min_alpha']} (must be 255 everywhere)")
    if f["alpha_mode"] != "OPAQUE":
        problems.append(f"alphaMode {f['alpha_mode']} (must be OPAQUE)")
    if f["tex_std"] <= float(limits.threshold("mesh.flat_texture_std_min")):
        problems.append(f"flat texture (channel std {f['tex_std']:.2f})")
    if f["tex_mode"] not in ("RGB", "L", "RGBA"):
        problems.append(f"texture mode {f['tex_mode']}")
    return [_res("CHK-M06", fm, "hard", not problems, "texture_contract", float(len(problems)), "PNG, RGB alpha 255, <=2048, not flat, OPAQUE",
                 "; ".join(problems) or f"{f['tex_size']} {f['tex_mode']} alpha {f['tex_min_alpha']} std {f['tex_std']:.1f}", "regenerate"),
            _res("CHK-M06W", fm, "soft", side <= warn_px, "texture_px", float(side), limits.describe("mesh.tex_warn_hard", "<= first"),
                 f"texture is {side} px; we ship {warn_px} px")]


def check_materials(f: Facts) -> CheckResult:
    fm = ["MESH-03", "MESH-05"]
    keys = ["has_color0", "emissive", "metallic_factor", "mr_texture"]
    m = _missing("CHK-M07", "hard", keys, f, fm)
    if m:
        return m
    problems = []
    if f["has_color0"] and not f.get("color0_all_white", False):
        problems.append("COLOR_0 vertex colours present")
    if f["emissive"]:
        problems.append("emissive set")
    if f["metallic_factor"] != 0:
        problems.append(f"metallicFactor {f['metallic_factor']} (must be written as 0)")
    if f["mr_texture"]:
        problems.append("metallic-roughness texture")
    if f.get("normal_texture"):
        problems.append("normal map")
    return _res("CHK-M07", fm, "hard", not problems, "material_contract", float(len(problems)),
                "no COLOR_0, no emissive, metallicFactor 0, no maps", "; ".join(problems) or "plain base colour material", "regenerate")


def check_box(f: Facts, asset_type: str, attachment: str, scale: str) -> CheckResult:
    fm = ["MESH-06", "HAIR-02"]
    m = _missing("CHK-M09", "hard", ["bbox_studs", "box_margins"], f, fm)
    if m:
        return m
    try:
        box = limits.box_for(asset_type, attachment or None)
    except KeyError as exc:
        return _res("CHK-M09", fm, "hard", False, "box_fit", None, "inside the Classic box", str(exc.args[0]), "revise_plan")
    problems = []
    ext = f["bbox_studs"]
    for i, name in enumerate("XYZ"):
        if ext[i] > box.size[i] + 1e-6:
            problems.append(f"Handle size {name} {ext[i]:.3f} > box {box.size[i]}")
    worst = min(f["box_margins"])
    if worst < -1e-6:
        problems.append(f"{f.get('vertices_outside_box', '?')} vertices outside the box (worst {worst:.3f} stud)")
    if scale != "Classic":
        problems.append(f"AvatarPartScaleType {scale} (must be Classic)")
    return _res("CHK-M09", fm, "hard", not problems, "box_margin_min_stud", float(worst),
                f"inside {box.asset_type}/{box.attachment} {box.size[0]}x{box.size[1]}x{box.size[2]}, offset {box.offset_file}",
                "; ".join(problems) or f"fits {box.size}; smallest margin {worst:.3f} stud; Classic", "revise_plan")


def check_surface_area(f: Facts) -> list[CheckResult]:
    fm = ["MESH-07"]
    m = _missing("CHK-M10", "hard", ["surface_area"], f, fm)
    if m:
        return [m]
    a = float(f["surface_area"])
    mx, warn = float(limits.threshold("mesh.surface_area_max")), float(limits.threshold("mesh.surface_area_warn"))
    return [_res("CHK-M10", fm, "hard", a <= mx, "surface_area_stud2", a, limits.describe("mesh.surface_area_max", "<="),
                 f"{a:.2f} stud^2 against our limit (UNVERIFIED: Roblox's docs give no number); both faces of a thin slab count", "revise_plan"),
            _res("CHK-M10W", fm, "soft", a <= warn, "surface_area_warn", a, limits.describe("mesh.surface_area_warn", "<="), f"{a:.2f} stud^2")]


def check_validator_defaults(f: Facts) -> CheckResult:
    fm = ["MESH-11"]
    m = _missing("CHK-M11", "hard", ["tris", "coplanar_intersections", "centre_offset", "scale_min"], f, fm)
    if m:
        return m
    allowed = math.floor(float(limits.threshold("mesh.coplanar_max_frac")) * int(f["tris"]))
    problems = []
    if f["coplanar_intersections"] > allowed:
        problems.append(f"{f['coplanar_intersections']} coplanar intersecting triangles (our limit {allowed}, UNVERIFIED)")
    if f["centre_offset"] > float(limits.threshold("mesh.center_offset_max")):
        problems.append(f"bbox centre {f['centre_offset']:.2f} stud from the origin (our limit 1.0, UNVERIFIED)")
    if f["scale_min"] < float(limits.threshold("mesh.scale_min")):
        problems.append(f"mesh scale {f['scale_min']} < 0.01 (our limit, UNVERIFIED)")
    return _res("CHK-M11", fm, "hard", not problems, "coplanar_centre_scale", float(f["coplanar_intersections"]),
                "coplanar <= 15% of triangles, centre <= 1 stud, scale >= 0.01 (UNVERIFIED: Roblox's docs name these checks but give no numbers)",
                "; ".join(problems) or f"{f['coplanar_intersections']} coplanar (max {allowed}), centre {f['centre_offset']:.2f}", "regenerate")


def check_sparse(f: Facts) -> list[CheckResult]:
    fm = ["MESH-10"]
    m = _missing("CHK-M12", "hard", ["view_coverage", "spike_shrink"], f, fm)
    if m:
        return [m]
    warn_t, fail_t = limits.threshold("mesh.sparse_cover_warn_fail")
    cov = f["view_coverage"]
    worst = min(cov.values()) if cov else 0.0
    problems = []
    if worst < fail_t:
        problems.append(f"a view covers only {worst:.0%} of its bounding box (fail below {fail_t:.0%})")
    spike_max = float(limits.threshold("mesh.spike_bbox_shrink_max"))
    if f["spike_shrink"] > spike_max:
        problems.append(f"removing 1% of the surface shrinks the bounding box by {f['spike_shrink']:.0%} (spikes or floating parts)")
    return [_res("CHK-M12", fm, "hard", not problems, "min_view_coverage", float(worst), f">= {fail_t} (warn below {warn_t}); spike shrink <= {spike_max}",
                 "; ".join(problems) or "coverage " + ", ".join(f"{k} {v:.0%}" for k, v in cov.items()), "regenerate"),
            _res("CHK-M12W", fm, "soft", worst >= warn_t, "min_view_coverage_warn", float(worst), f">= {warn_t}", f"smallest view coverage {worst:.0%}")]


def validate_accessory(mesh_facts: Facts, asset_type: str, attachment: str, scale: str = "Classic") -> list[CheckResult]:
    """CHK-M01 ... M12 for one accessory from its measured facts. Never raises: any problem becomes a ``ran=False`` result."""
    out: list[CheckResult] = []
    runners = [
        ("CHK-M01", lambda: [check_load(mesh_facts)]),
        ("CHK-M02", lambda: [check_structure(mesh_facts)]),
        ("CHK-M03", lambda: check_triangles(mesh_facts, asset_type)),
        ("CHK-M04", lambda: [check_topology(mesh_facts)]),
        ("CHK-M05", lambda: check_shells(mesh_facts)),
        ("CHK-M06", lambda: check_texture(mesh_facts)),
        ("CHK-M07", lambda: [check_materials(mesh_facts)]),
        ("CHK-M09", lambda: [check_box(mesh_facts, asset_type, attachment, scale)]),
        ("CHK-M10", lambda: check_surface_area(mesh_facts)),
        ("CHK-M11", lambda: [check_validator_defaults(mesh_facts)]),
        ("CHK-M12", lambda: check_sparse(mesh_facts)),
    ]
    for cid, fn in runners:
        try:
            out.extend(fn())
        except Exception as exc:  # noqa: BLE001 - fail closed
            out.append(not_run(cid, "hard", f"{type(exc).__name__}: {exc}"))
    return out
