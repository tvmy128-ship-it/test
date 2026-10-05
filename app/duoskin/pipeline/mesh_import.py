"""Manual-mode import of 3D files (APP_SPEC 11.4, bible 15.2; FAILURE_MODES ACC-09 ... ACC-12, SYS-04, CHK-M16). Pure functions.

``import_file`` takes ONE file the user dropped on a tile or saved into ``TripoPacks\\inbox\\`` (or a polish pack's ``return\\``) and
decides what it is and whether it can enter the mesh gate:

* the type comes from the magic bytes, never from the suffix (a ``.glb`` that is really an FBX is called an FBX);
* partial downloads (``.crdownload``, ``.part``, ``.tmp`` ...) are refused, and ``ready_to_import`` implements the inbox rule
  "size stable for 2 polls and the file opens exclusively";
* 50 MB cap; ``.zip`` is extracted safely (no absolute or ``..`` paths, no links, no encrypted members, at most 200 files and
  500 MB in total, counted while streaming, not from the headers);
* glTF/GLB are only SCANNED as JSON here (``extensionsRequired`` such as meshopt, Draco without DracoPy, KTX2 are refused with
  "re-export without compression"); meshes are only parsed in the mesh worker subprocess;
* FBX, ``.blend`` and zipped OBJ need headless Blender: without it the answer is "Blender not installed: export GLB instead";
* the licence flag (free Tripo plan = public, CC BY 4.0 label, no commercial rights) travels with the file and is turned into a banner.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from duoskin.checks.model import CheckResult
from duoskin.mesh import gltf_io, load
from duoskin.mesh.types import Licence
from duoskin.roblox import limits

PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".partial", ".download", ".opdownload", ".filepart")
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MODEL_PRIORITY = ("glb", "gltf", "fbx", "obj", "blend")
BLENDER_MISSING = load.BLENDER_MISSING

FREE_PLAN_BANNER = ("This model came from Tripo's FREE plan: it is public (labelled CC BY 4.0) and gives NO commercial-use rights. "
                    "It cannot be marked sell-ready. Use a paid plan for anything you may sell.")

Plan = Literal["api", "paid", "free", "not_tripo"]
Kind = Literal["glb", "gltf", "fbx", "obj", "blend", "zip", "unknown"]


def licence_from_plan(plan: Plan) -> Licence:
    """The import wizard's answer to "Which Tripo plan made this file?" (APP_SPEC 11.1)."""
    return {"api": "tripo_api_private_commercial", "paid": "tripo_paid_private_commercial", "free": "tripo_free_public_ccby_noncommercial",
            "not_tripo": "user_made"}[plan]


def licence_banner(licence: str) -> str | None:
    """The warning to show next to a file, or None. Free-plan files are public and never sell-ready."""
    if licence == "tripo_free_public_ccby_noncommercial":
        return FREE_PLAN_BANNER
    if licence == "unknown":
        return "The licence of this file is unknown: say which Tripo plan made it, or that you made it yourself."
    return None


def sell_ready_allowed(licence: str) -> bool:
    """ACC-10: a free-plan file can never carry a sell-ready flag."""
    return licence != "tripo_free_public_ccby_noncommercial"


def provenance_source(licence: str) -> str:
    return {"tripo_api_private_commercial": "tripo_api", "user_made": "user_made"}.get(licence, "tripo_manual")


# --------------------------------------------------------------------------------------------------------------------
# inbox rules
# --------------------------------------------------------------------------------------------------------------------
def is_partial_name(name: str) -> bool:
    """``.crdownload``/``.part``/``.tmp`` and friends, Office lock files and hidden temp names are never imported."""
    low = name.lower()
    return low.endswith(PARTIAL_SUFFIXES) or low.startswith(("~$", ".")) or low.endswith("~")


def open_exclusive(path: str | Path) -> bool:
    """True when the file can be opened for writing (a browser or Explorer still holding it makes this fail on Windows)."""
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        if os.name == "nt":                       # pragma: no cover - Windows only
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                return False
        return True
    finally:
        os.close(fd)


def ready_to_import(path: str | Path, size_history: list[int], *, stable_polls: int = 2) -> bool:
    """Inbox watcher rule (ACC-11): not a partial name, size unchanged over the last ``stable_polls`` polls, opens exclusively.

    ``size_history`` holds the sizes seen on previous polls (the caller appends the current one before calling).
    """
    p = Path(path)
    if is_partial_name(p.name) or not p.is_file():
        return False
    if len(size_history) < stable_polls or len(set(size_history[-stable_polls:])) != 1 or size_history[-1] <= 0:
        return False
    return open_exclusive(p)


# --------------------------------------------------------------------------------------------------------------------
# safe zip extraction
# --------------------------------------------------------------------------------------------------------------------
class UnsafeArchive(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_member(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise UnsafeArchive("unsafe_path", "the ZIP contains a file name with a NUL character")
    norm = name.replace("\\", "/")
    if norm.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", norm):
        raise UnsafeArchive("unsafe_path", f"the ZIP contains an absolute path ({name!r})")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchive("unsafe_path", f"the ZIP tries to write outside its folder ({name!r})")
    for p in parts:
        if p.split(".")[0].upper() in RESERVED_NAMES or p.endswith((" ", ".")) or ":" in p:
            raise UnsafeArchive("unsafe_path", f"the ZIP contains a name Windows does not allow ({name!r})")
    return PurePosixPath(*parts) if parts else PurePosixPath("")


def safe_extract_zip(zip_path: str | Path, dest: str | Path, *, max_total_bytes: int | None = None, max_files: int | None = None,
                     max_member_bytes: int | None = None) -> list[Path]:
    """Extract ``zip_path`` into ``dest`` or raise ``UnsafeArchive``. Nothing is written when a header check fails."""
    mb = 1024 * 1024
    max_total = max_total_bytes if max_total_bytes is not None else int(limits.threshold("upload.zip_max_total_mb")) * mb
    max_n = max_files if max_files is not None else int(limits.threshold("upload.zip_max_files"))
    max_member = max_member_bytes if max_member_bytes is not None else max_total
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    root_res = root.resolve()
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > max_n:
            raise UnsafeArchive("too_many_files", f"the ZIP has {len(infos)} files (limit {max_n})")
        if sum(i.file_size for i in infos) > max_total:
            raise UnsafeArchive("zip_too_big", f"the ZIP would expand to more than {max_total // mb} MB")
        plan: list[tuple[zipfile.ZipInfo, Path]] = []
        for i in infos:
            if i.flag_bits & 0x1:
                raise UnsafeArchive("encrypted", "the ZIP is password protected")
            mode = i.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise UnsafeArchive("unsafe_path", f"the ZIP contains a link ({i.filename!r})")
            rel = _safe_member(i.filename)
            if not rel.parts:
                continue
            target = (root / Path(*rel.parts)).resolve()
            if not target.is_relative_to(root_res):
                raise UnsafeArchive("unsafe_path", f"the ZIP tries to write outside its folder ({i.filename!r})")
            plan.append((i, target))
        total = 0
        for info, target in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > max_member or total > max_total:
                        dst.close()
                        shutil.rmtree(root, ignore_errors=True)
                        raise UnsafeArchive("zip_too_big", "the ZIP expands to more data than its headers claimed")
                    dst.write(chunk)
            out.append(target)
    return out


# --------------------------------------------------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class ImportIssue:
    code: str
    message: str


@dataclass
class ImportResult:
    ok: bool
    kind: str = "unknown"
    work_path: str | None = None                 # the file to hand to the mesh worker (a .glb/.gltf/.fbx/.obj/.blend)
    original_name: str = ""
    size: int = 0
    sha256: str = ""
    needs_blender: bool = False
    issues: list[ImportIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extracted: list[str] = field(default_factory=list)
    licence: str = "unknown"
    banner: str | None = None
    assigned_pack_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        return " ".join(i.message for i in self.issues)


_PACK_ID_RE = re.compile(r"(DS-[a-z0-9][a-z0-9_-]*-[a-z0-9_]+-[0-9a-f]{6})", re.IGNORECASE)


def pack_id_from_name(name: str) -> str | None:
    """The ``DS-<project>-<part>-<6hex>`` id a user was told to rename the download to (case-insensitive), if the name carries one."""
    m = _PACK_ID_RE.search(name)
    return m.group(1) if m else None


def _scan_gltf(path: Path, kind: str, issues: list[ImportIssue], notes: list[str]) -> None:
    try:
        doc, _ = gltf_io.read_gltf_json(path)
    except Exception as exc:  # noqa: BLE001 - corrupt file
        issues.append(ImportIssue("corrupt", f"the file is damaged or not a valid glTF ({type(exc).__name__}); download or export it again"))
        return
    for problem in load.check_extensions(doc):
        issues.append(ImportIssue("unsupported_extension", problem))
    refs = gltf_io.external_references(doc, glb=kind == "glb")
    if refs:
        issues.append(ImportIssue("external_reference", "The model points at files outside itself (" + "; ".join(refs[:3]) + "). Export a single .glb with the texture inside it."))
    for problem in gltf_io.complexity_problems(doc):
        issues.append(ImportIssue("too_complex", "The model is too heavy to read safely: " + problem + ". Export it with fewer triangles."))
    if kind == "gltf":
        bad = gltf_io.check_gltf_files(path)
        if bad:
            issues.append(ImportIssue("missing_files", "The .gltf needs files that are not next to it (" + "; ".join(bad) + "). Export a single .glb instead."))
    facts = gltf_io.gltf_structure_facts(path)
    if facts["materials"] > 1 or facts["primitives"] > 1:
        notes.append("several materials or parts: they are merged into one during repair")


def _single_file(src: Path, work: Path, notes: list[str], issues: list[ImportIssue], blender_available: bool | None) -> tuple[str, Path | None, bool]:
    """Classify one non-zip file. Returns ``(kind, path to hand on, needs_blender)``."""
    kind = load.sniff(src)
    suffix_kind = {".glb": "glb", ".gltf": "gltf", ".fbx": "fbx", ".obj": "obj", ".blend": "blend", ".zip": "zip"}.get(src.suffix.lower())
    if suffix_kind and suffix_kind != kind and kind != "unknown":
        notes.append(f"the file is named like a {suffix_kind.upper()} but its content is {kind.upper()}: treated as {kind.upper()}")
    if kind in ("glb", "gltf"):
        dst = work / (src.name if src.suffix.lower() == f".{kind}" else f"{src.stem}.{kind}")
        if dst != src:
            shutil.copy2(src, dst)
        _scan_gltf(dst, kind, issues, notes)
        return kind, dst, False
    if kind in ("fbx", "blend", "obj"):
        dst = work / (src.name if src.suffix.lower() == f".{kind}" else f"{src.stem}.{kind}")
        if dst != src:
            shutil.copy2(src, dst)
        if kind in ("fbx", "blend"):
            if blender_available is False:
                issues.append(ImportIssue("blender_missing", BLENDER_MISSING))
            return kind, dst, True
        notes.append("OBJ carries no PBR data: it is read with Blender when present, else with the built-in reader")
        return kind, dst, blender_available is not False
    if kind in ("stl", "ply"):
        issues.append(ImportIssue("no_texture_format", f"{kind.upper()} files carry no texture: export a GLB with the texture from your 3D tool"))
        return kind, None, False
    if kind in ("png", "jpeg", "webp"):
        issues.append(ImportIssue("not_a_model", "this is an image, not a 3D model: drop the downloaded .glb file"))
        return kind, None, False
    issues.append(ImportIssue("unknown_format", "this file type is not a 3D model we can read: use a .glb (preferred), a .gltf with its files, a .fbx (needs Blender) or a .zip of those"))
    return "unknown", None, False


def import_file(src: str | Path, work_dir: str | Path, *, licence: Licence | str = "unknown", expected_pack_id: str | None = None,
                blender_available: bool | None = None, task_link: str = "", tripo_plan: str = "", max_bytes: int | None = None) -> ImportResult:
    """Classify, size-check, safely unpack and scan one dropped file. Never parses a mesh and never raises for a bad file.

    ``work_dir`` receives the (copied or extracted) files; ``blender_available`` is the app's detection result (None = unknown,
    treated as available). ``expected_pack_id`` is the pack of the tile the file was dropped on (``DS-...``).
    """
    src = Path(src)
    work = Path(work_dir)
    res = ImportResult(ok=False, original_name=src.name, licence=str(licence), banner=licence_banner(str(licence)))
    cap = (max_bytes if max_bytes is not None else int(limits.threshold("upload.max_mb")) * 1024 * 1024)
    if is_partial_name(src.name):
        res.issues.append(ImportIssue("partial_download", "this looks like an unfinished download: wait for the browser to finish, then try again"))
        return res
    if not src.is_file():
        res.issues.append(ImportIssue("not_found", "the file is not there any more"))
        return res
    res.size = src.stat().st_size
    if res.size == 0:
        res.issues.append(ImportIssue("empty", "the file is empty"))
        return res
    if res.size > cap:
        res.issues.append(ImportIssue("too_big", f"the file is {res.size / 1048576:.0f} MB; the limit is {cap // 1048576} MB. Export it with fewer triangles and a 1024 px texture."))
        return res
    res.sha256 = load.sha256_file(src)
    work.mkdir(parents=True, exist_ok=True)
    kind = load.sniff(src)
    primary: Path | None = None
    if kind == "zip":
        dest = work / f"unzipped-{res.sha256[:10]}"
        try:
            members = safe_extract_zip(src, dest)
        except UnsafeArchive as exc:
            res.kind = "zip"
            res.issues.append(ImportIssue(exc.code, exc.message))
            return res
        except zipfile.BadZipFile:
            res.kind = "zip"
            res.issues.append(ImportIssue("corrupt", "the ZIP file is damaged: download it again"))
            return res
        res.extracted = [str(m) for m in members]
        candidates: dict[str, list[Path]] = {}
        for m in members:
            k = load.sniff(m) if m.suffix.lower() in (".glb", ".gltf", ".fbx", ".obj", ".blend", ".zip") else "other"
            if k in MODEL_PRIORITY:
                candidates.setdefault(k, []).append(m)
            elif k == "zip":
                res.notes.append("a ZIP inside the ZIP was ignored")
        for k in MODEL_PRIORITY:
            pool = candidates.get(k, [])
            if len(pool) == 1:
                primary = pool[0]
                break
            if len(pool) > 1:
                named = [m for m in pool if expected_pack_id and expected_pack_id.lower() in m.name.lower()]
                if len(named) == 1:
                    primary = named[0]
                    break
                res.kind = "zip"
                res.issues.append(ImportIssue("ambiguous_zip", f"the ZIP holds {len(pool)} {k.upper()} models: put only the one you want in the ZIP"))
                return res
        if primary is None:
            res.kind = "zip"
            res.issues.append(ImportIssue("no_model", "the ZIP contains no .glb, .gltf, .fbx, .obj or .blend model"))
            return res
        res.notes.append(f"unpacked {len(members)} files; the model is {primary.name}")
        res.kind, path_, res.needs_blender = _single_file(primary, primary.parent, res.notes, res.issues, blender_available)
        res.work_path = str(path_) if path_ else None
        if res.kind == "obj" and blender_available is False:
            res.needs_blender = False
    else:
        res.kind, path_, res.needs_blender = _single_file(src, work, res.notes, res.issues, blender_available)
        res.work_path = str(path_) if path_ else None
    res.assigned_pack_id = pack_id_from_name(src.name) or (pack_id_from_name(primary.name) if primary else None)
    res.ok = not res.issues and res.work_path is not None
    res.provenance = {"source": provenance_source(str(licence)), "licence": str(licence), "sha256": res.sha256, "bytes": res.size,
                      "original_name": src.name, "format": res.kind, "tripo_plan": tripo_plan, "task_link": task_link,
                      "pack_id": res.assigned_pack_id or expected_pack_id or ""}
    return res


def check_assignment(res: ImportResult, expected_pack_id: str | None, *, known_pack_ids: set[str] | None = None) -> CheckResult:
    """CHK-M16 (HARD): the pack id in the file name matches the tile; the Tripo plan and licence are recorded; partials never arrive here."""
    fm = ["ACC-09", "ACC-10", "ACC-11"]
    problems = []
    if res.assigned_pack_id and expected_pack_id and res.assigned_pack_id.lower() != expected_pack_id.lower():
        problems.append(f"the file carries pack id {res.assigned_pack_id} but was dropped on {expected_pack_id}")
    if known_pack_ids is not None and res.assigned_pack_id and res.assigned_pack_id.lower() not in {k.lower() for k in known_pack_ids}:
        problems.append(f"no tile has pack id {res.assigned_pack_id}")
    if res.licence in ("", "unknown"):
        problems.append("the Tripo plan (free or paid) or 'made by me' has not been recorded")
    if not res.sha256:
        problems.append("the file was not hashed")
    if res.licence == "tripo_free_public_ccby_noncommercial" and not res.banner:
        problems.append("the free-plan banner is missing")
    ok = not problems
    return CheckResult(check_id="CHK-M16", fm_ids=fm, kind="hard", passed=ok, metric="assignment_and_licence", value=float(len(problems)),
                       threshold="pack id matches, licence recorded", evidence="; ".join(problems) or f"pack {res.assigned_pack_id or expected_pack_id or '(assigned by the user)'}, licence {res.licence}",
                       fix_hint="none" if ok else "human")


def write_provenance(res: ImportResult, path: str | Path) -> None:
    Path(path).write_text(json.dumps(res.provenance, indent=1, sort_keys=True), encoding="utf-8")


def file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
