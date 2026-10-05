"""Headless Blender bridge (APP_SPEC 4.3 SYS-11, 10.9; FAILURE_MODES MESH-14, MESH-19).

Blender is optional. It is used for FBX / ``.blend`` / zipped-OBJ imports, the FBX backup export, bake-to-atlas and the
hair polish pack. Rules baked in here:

* call ``blender.exe`` (never ``blender-launcher.exe``, which returns at once) as
  ``blender --background --factory-startup --disable-autoexec --python-exit-code 3 --python <script> -- <args.json> <result.json>``;
* Blender exits 0 even when a script fails unless ``--python-exit-code`` is given, and stdout is noisy, so the real result
  always comes back through a JSON file;
* ``CREATE_NO_WINDOW`` on Windows and a psutil tree kill on timeout (``duoskin.mesh.proc``).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duoskin.mesh.proc import run_process

SCRIPT_DIR = Path(__file__).with_name("blender_scripts")
BLENDER_MISSING = "Blender not installed: export GLB instead"
_version_cache: dict[str, str | None] = {}


@dataclass
class BlenderResult:
    ok: bool
    files: dict[str, str] = field(default_factory=dict)
    error: str = ""
    version: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    log: str = ""


def _candidates() -> list[str]:
    out: list[str] = []
    env = os.environ.get("DUOSKIN_BLENDER") or os.environ.get("BLENDER_PATH")
    if env:
        out.append(env)
    which = shutil.which("blender")
    if which:
        out.append(which)
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        for r in roots:
            if not r:
                continue
            hits = sorted(glob.glob(os.path.join(r, "Blender Foundation", "Blender *", "blender.exe")), reverse=True)
            out.extend(hits)
    elif sys.platform == "darwin":
        out.append("/Applications/Blender.app/Contents/MacOS/Blender")
    else:
        out.extend(["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"])
        out.extend(sorted(glob.glob("/opt/blender*/blender"), reverse=True))
    return out


def find_blender(explicit: str = "") -> str | None:
    """Path of a usable ``blender`` executable, or None. ``explicit`` (a setting) wins, then ``DUOSKIN_BLENDER``, PATH, defaults."""
    cands = ([explicit] if explicit else []) + _candidates()
    for c in cands:
        if not c:
            continue
        p = Path(c)
        if p.name.lower().startswith("blender-launcher"):
            p = p.with_name("blender.exe" if p.suffix.lower() == ".exe" else "blender")
        if p.is_file():
            return str(p)
    return None


def blender_version(exe: str, timeout_s: float = 30) -> str | None:
    """``"4.2.3"`` style version string from ``blender --version`` (cached), None when it cannot be run."""
    if exe in _version_cache:
        return _version_cache[exe]
    res = run_process([exe, "--version"], timeout_s=timeout_s)
    m = re.search(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", res.stdout)
    ver = m.group(1) if (res.ok and m) else None
    _version_cache[exe] = ver
    return ver


def build_command(exe: str, script: str | Path, args_json: str | Path, result_json: str | Path) -> list[str]:
    """The exact command line (kept in one place so a test can assert it)."""
    return [exe, "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "3",
            "--python", str(script), "--", str(args_json), str(result_json)]


def run_script(exe: str, script_name: str, args: dict[str, Any], *, timeout_s: float = 180, max_rss_mb: float | None = None) -> BlenderResult:
    """Run ``blender_scripts/<script_name>`` with ``args`` (JSON) and return what it wrote to its result file."""
    script = SCRIPT_DIR / script_name
    if not script.is_file():
        return BlenderResult(False, error=f"missing Blender script {script_name}")
    with tempfile.TemporaryDirectory(prefix="duoskin_blender_") as td:
        args_path, result_path = Path(td) / "args.json", Path(td) / "result.json"
        args_path.write_text(json.dumps(args), encoding="utf-8")
        res = run_process(build_command(exe, script, args_path, result_path), timeout_s=timeout_s, cwd=td, max_rss_mb=max_rss_mb)
        log = (res.stdout + "\n" + res.stderr).strip()[-2000:]
        if res.timed_out:
            return BlenderResult(False, error=f"Blender timed out after {int(timeout_s)} s", log=log)
        data: dict[str, Any] = {}
        if result_path.is_file():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if res.returncode != 0 or not data.get("ok"):
            err = data.get("error") or f"Blender exited with code {res.returncode}"
            return BlenderResult(False, error=str(err)[:600], data=data, log=log)
        return BlenderResult(True, files={k: str(v) for k, v in (data.get("files") or {}).items()}, version=str(data.get("blender_version", "")),
                             data=data, log=log)


def import_to_glb(exe: str, src: str | Path, dst_glb: str | Path, *, kind: str = "", timeout_s: float = 180) -> BlenderResult:
    """FBX / OBJ / ``.blend`` / glTF -> a clean GLB (triangulated, armatures removed, transforms applied, Y up)."""
    src = Path(src)
    kind = kind or {".fbx": "fbx", ".obj": "obj", ".blend": "blend", ".glb": "glb", ".gltf": "glb"}.get(src.suffix.lower(), "fbx")
    Path(dst_glb).parent.mkdir(parents=True, exist_ok=True)
    res = run_script(exe, "fbx_import.py", {"src": str(src), "dst": str(dst_glb), "kind": kind}, timeout_s=timeout_s)
    if res.ok and "glb" not in res.files:
        res.files["glb"] = str(dst_glb)
    return res


def export_fbx(exe: str, src_glb: str | Path, dst_fbx: str | Path, *, timeout_s: float = 180) -> BlenderResult:
    """The FBX backup: texture embedded, Path Mode Copy, Apply Scalings = FBX Unit Scale, Y up, front +Z (MESH-14, MESH-19)."""
    Path(dst_fbx).parent.mkdir(parents=True, exist_ok=True)
    res = run_script(exe, "fbx_export.py", {"src": str(src_glb), "dst": str(dst_fbx)}, timeout_s=timeout_s)
    if res.ok and "fbx" not in res.files:
        res.files["fbx"] = str(dst_fbx)
    return res


def bake_atlas(exe: str, src: str | Path, dst_glb: str | Path, *, size: int = 1024, timeout_s: float = 300) -> BlenderResult:
    """Bake several materials (or tiled UVs) of ``src`` into one 0-1 atlas texture and write a GLB."""
    res = run_script(exe, "bake_atlas.py", {"src": str(src), "dst": str(dst_glb), "size": int(size)}, timeout_s=timeout_s)
    if res.ok and "glb" not in res.files:
        res.files["glb"] = str(dst_glb)
    return res


def polish_pack(exe: str, fitted_glb: str | Path, out_dir: str | Path, *, head_glb: str | Path | None = None,
                timeout_s: float = 240) -> BlenderResult:
    """``hair_fitted.fbx`` + ``hair_fitted.blend`` (+ ``head_guide.fbx``) for the hair polish pack (APP_SPEC 10.7)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return run_script(exe, "polish_pack.py", {"fitted": str(fitted_glb), "head": str(head_glb) if head_glb else "", "out_dir": str(out_dir)},
                      timeout_s=timeout_s)


def status(explicit: str = "") -> dict[str, Any]:
    """What ``doctor`` and the setup wizard show about Blender (FAILURE_MODES MESH-19, issue #29): where it is, which version, and
    exactly when it becomes REQUIRED. Blender is optional: without it the glTF set is the upload format and FBX is skipped."""
    exe = find_blender(explicit)
    ver = blender_version(exe) if exe else None
    return {
        "found": exe is not None, "path": exe, "version": ver, "usable": bool(exe and ver),
        "optional": True,
        "required_when": ["Studio rejects the glTF set (calibration test T5)", "the hair polish pack should contain .fbx/.blend files",
                          "importing an FBX, .blend or zipped OBJ from Tripo or your own tool"],
        "without_blender": "FBX export is skipped (the manifest says 'fbx: not produced'), the checklist uses the glTF set, and FBX imports "
                           "ask you to export a GLB instead",
        "guidance": None if exe else BLENDER_MISSING + " (install Blender 4.x from blender.org or with winget install BlenderFoundation.Blender, then set its path in Settings)",
    }
