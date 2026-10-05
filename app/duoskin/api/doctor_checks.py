"""``duoskin doctor``: the startup checks CHK-S01..S15 (FAILURE_MODES §7.0, APP_SPEC §15.3 and §4.4).

Each check returns an ``Outcome``; the runner turns it into a ``CheckResult`` (fail closed: an exception or a missing
dependency gives ``ran=False, passed=False``) and a human-readable ``DoctorItem``. The report says plainly what is wrong and
what to do, and decides three things:

* ``blocks_paid_features``: a HARD or ASSERT check failed or did not run (``passed=False``, which includes ``not_run``).
  Real-provider paid steps then do not run (mock steps still do). SOFT and ``not_applicable`` results never block.
* ``broken_install`` (exit code 3): the interpreter, the data folder or the package itself is broken.
* ``exit_code``: **0** ok (warnings allowed), **2** a blocking check failed or did not run, **3** broken install.

**A fresh install is never blocked** (APP_SPEC §2 S25). Blocking: S01-S08, S10, S12, S13 and the HARD part of S11 (the
manifest loads, the kit enums build, the colour dictionary is clean). Warnings only: S09 (a missing or failing Blender
switches on the no-Blender mode), S14 (a present-but-bad ``dreamsim.onnx`` is switched off and the clone check runs
degraded) and S15 (kit and component availability: head base, body base, house-style sheet, hair kit, DreamSim, Blender).

``quick=True`` skips the slow subprocess probes (native imports, OCR, TLS, Blender, the DreamSim load): used by tests and by
the setup run.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import sysconfig
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from duoskin import __version__, config, winplat
from duoskin.checks.model import CheckResult, not_applicable, not_run
from duoskin.logsetup import redact
from duoskin.models.common import iso_utc, utcnow
from duoskin.security import child_env

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.doctor")

Status = Literal["pass", "warn", "fail", "na"]
PROVIDER_HOSTS = {"anthropic": "api.anthropic.com", "openai": "api.openai.com", "recraft": "external.api.recraft.ai",
                  "tripo": "api.tripo3d.ai", "gemini": "generativelanguage.googleapis.com"}
MEDIA_FEATURE_PACK = "https://support.microsoft.com/windows/media-feature-pack-list-for-windows-n-editions-c1c6fffa-d052-8338-7a79-a4bb980dd467"
VCREDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
SUBPROCESS_TIMEOUT_S = 90


@dataclass
class Outcome:
    status: Status
    message: str
    fix: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorCtx:
    paths: config.DataPaths
    settings: Any
    rt: Runtime | None
    quick: bool = False
    setup: bool = False
    outcomes: dict[str, Outcome] = field(default_factory=dict)    # the outcomes of the checks that already ran (S15 reads S09/S14)

    def keys(self) -> Any:
        if self.rt is not None:
            return self.rt.keys
        from duoskin.keystore import KeyStore

        return KeyStore(self.paths)

    @property
    def started_via_module(self) -> bool:
        if self.rt is not None and self.rt.started_via_module:
            return True
        main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
        return getattr(main_spec, "name", "") == "duoskin.__main__"


@dataclass
class CheckDef:
    id: str
    title: str
    kind: Literal["hard", "soft", "assert"]
    fn: Callable[[DoctorCtx], Outcome]
    fm_ids: list[str] = field(default_factory=list)
    fatal: bool = False          # a failure means the install is broken (exit code 3)


_CHECKS: list[CheckDef] = []


def check(check_id: str, title: str, *, kind: Literal["hard", "soft", "assert"], fm: list[str] | None = None, fatal: bool = False):
    def deco(fn: Callable[[DoctorCtx], Outcome]) -> Callable[[DoctorCtx], Outcome]:
        _CHECKS.append(CheckDef(check_id, title, kind, fn, fm or [], fatal))
        return fn
    return deco


#: SYS-09: the app registers the venv's ``msvcp140*.dll`` folders before it imports onnxruntime (``winplat.fix_dll_directories``).
#: A probe child must do the same, or ``import onnxruntime`` fails there on a PC without the VC++ redistributable although the
#: app itself would work (and the doctor would block paid features for nothing).
_DLL_PRELUDE = (
    "import os as _os, sys as _sys\n"
    "if _sys.platform == 'win32':\n"
    "    for _d in (_sys.prefix, _os.path.join(_sys.prefix, 'Scripts')):\n"
    "        if _os.path.exists(_os.path.join(_d, 'msvcp140.dll')):\n"
    "            _os.add_dll_directory(_d)\n"
)


def run_py(code: str, *, timeout: int = SUBPROCESS_TIMEOUT_S, args: list[str] | None = None,
           cwd: Path | None = None) -> tuple[int, str, str]:
    """Run ``python -c code`` in a child process (native imports never run in the server process). ``cwd=APP_ROOT``
    lets the child ``import duoskin``."""
    env = child_env({"PYTHONUTF8": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", _DLL_PRELUDE + code, *(args or [])], check=False, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                              cwd=str(cwd) if cwd else None)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"
    except OSError as exc:
        return 127, "", str(exc)


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ------------------------------------------------------------------------------------------------------- S01
_CV2_UNICODE_SCRIPT = """
import json, sys
import numpy as np, cv2
data = np.fromfile(sys.argv[1], dtype=np.uint8)
img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
print(json.dumps({"ok": img is not None, "shape": list(img.shape) if img is not None else None}))
"""


@check("CHK-S01", "Unicode file paths work (OpenCV and Pillow)", kind="hard", fm=["SYS-05"])
def chk_s01(c: DoctorCtx) -> Outcome:
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="duoskin-", ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp) / "caf\u00e9_\u65e5\u672c\u8a9e"
        folder.mkdir()
        target = folder / "t\u00e9st \u2713.png"
        Image.new("RGBA", (4, 3), (10, 200, 30, 255)).save(target)
        with Image.open(target) as im:
            im.load()
            if im.size != (4, 3) or im.convert("RGBA").getpixel((0, 0)) != (10, 200, 30, 255):
                return Outcome("fail", "Pillow read a different image back from a non-ASCII path.", "Reinstall with setup.bat.")
        if c.quick:
            return Outcome("pass", "Pillow reads and writes non-ASCII paths (OpenCV probe skipped in quick mode).")
        rc, out, err = run_py(_CV2_UNICODE_SCRIPT, args=[str(target)])
        if rc != 0:
            return Outcome("fail", "OpenCV could not be imported or run.",
                           "Run setup.bat again. On Windows N/KN editions install the Media Feature Pack. " + _last_line(err)[:200])
        try:
            ok = bool(json.loads(_last_line(out)).get("ok"))
        except ValueError:
            ok = False
        if not ok:
            return Outcome("fail", "OpenCV returned nothing for a file in a non-ASCII folder.",
                           "The app reads images with np.fromfile + imdecode; reinstall with setup.bat.")
    return Outcome("pass", "Pillow and OpenCV both read files in folders with accents and non-Latin letters.")


# ------------------------------------------------------------------------------------------------------- S02
@check("CHK-S02", "UTF-8 text works (files and child processes)", kind="hard", fm=["SYS-06"])
def chk_s02(c: DoctorCtx) -> Outcome:
    sample = "caf\u00e9 \u65e5\u672c\u8a9e \u0645\u0631\u062d\u0628\u0627 \u2713"
    with tempfile.TemporaryDirectory(prefix="duoskin-", ignore_cleanup_errors=True) as tmp:
        p = Path(tmp) / "t\u00e9xt.txt"
        p.write_text(sample, encoding="utf-8")
        if p.read_text(encoding="utf-8") != sample:
            return Outcome("fail", "A UTF-8 text file did not read back the same.", "Check the disk and antivirus.")
    code = "import sys; sys.stdout.write(sys.argv[1])"
    env = child_env({"PYTHONUTF8": "1"})
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code, sample], check=False, capture_output=True, encoding="utf-8",
                              timeout=30, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Outcome("fail", f"Could not start a child process: {exc}", "Check that Python is installed correctly.")
    if proc.stdout != sample:
        return Outcome("fail", "Non-ASCII text got mangled passing through a child process.", "Start the app with start.bat (it sets PYTHONUTF8=1).")
    utf8 = bool(sys.flags.utf8_mode)
    note = "" if utf8 or not winplat.IS_WINDOWS else " (tip: start.bat sets PYTHONUTF8=1; the app also passes encoding='utf-8' everywhere)"
    return Outcome("pass", "UTF-8 files and child-process text round-trip correctly" + note + ".", detail={"utf8_mode": utf8})


# ------------------------------------------------------------------------------------------------------- S03
@check("CHK-S03", "Python is the right build", kind="hard", fm=["SYS-10"], fatal=True)
def chk_s03(c: DoctorCtx) -> Outcome:
    problems: list[str] = []
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        problems.append("it is not running inside the app's .venv")
    if "windowsapps" in sys.base_prefix.lower():
        problems.append("it is the Microsoft Store stub, not a real Python install")
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        problems.append("it is a free-threaded build (3.14t), which the locked packages do not support")
    version = sys.version_info[:2]
    if winplat.IS_WINDOWS:
        if sysconfig.get_platform() != "win-amd64":
            problems.append(f"it is a {sysconfig.get_platform()} build; 64-bit x64 Python is required")
        if version not in ((3, 14), (3, 13), (3, 12)):
            problems.append(f"Python {version[0]}.{version[1]} is not supported (use 3.14, 3.13 or 3.12)")
    elif version < (3, 11):
        problems.append(f"Python {version[0]}.{version[1]} is too old for development (3.11+)")
    detail = {"executable": sys.executable, "version": platform.python_version(), "platform": sysconfig.get_platform(),
              "venv": in_venv}
    if problems:
        return Outcome("fail", "Python problem: " + "; ".join(problems) + ".",
                       "Run setup.bat to build the .venv with 64-bit Python 3.14 (or 3.13 or 3.12) from python.org.", detail)
    note = "" if winplat.IS_WINDOWS else " (development host: the Windows-only platform rules are not applied)"
    return Outcome("pass", f"Python {platform.python_version()} ({sysconfig.get_platform()}) in a virtual environment{note}.", detail=detail)


# ------------------------------------------------------------------------------------------------------- S04
_NATIVE_PROBES: dict[str, str] = {
    "onnxruntime": "import onnxruntime; print(onnxruntime.__version__)",
    "cv2": "import cv2; print(cv2.__version__)",
    "resvg_py": "import resvg_py; print('ok')",
    "trimesh": "import trimesh; print(trimesh.__version__)",
    "pymeshlab": "import pymeshlab; print('ok')",
}
#: Native imports whose failure only switches on a documented fallback (mesh/worker.py: the built-in UV-aware decimator): a warning.
OPTIONAL_NATIVE = ("pymeshlab",)
_OCR_SCRIPT = r"""
import json, sys
from PIL import Image, ImageDraw, ImageFont
im = Image.new("RGB", (260, 90), "white")
d = ImageDraw.Draw(im)
try:
    font = ImageFont.load_default(size=56)
except TypeError:
    font = ImageFont.load_default()
d.text((20, 10), "TEST", fill="black", font=font)
import numpy as np
arr = np.array(im)
try:
    from rapidocr import RapidOCR
    out = RapidOCR()(arr)
    txts, scores = list(getattr(out, "txts", None) or []), list(getattr(out, "scores", None) or [])
except ImportError:
    from rapidocr_onnxruntime import RapidOCR
    res, _ = RapidOCR()(arr)
    txts = [r[1] for r in (res or [])]
    scores = [float(r[2]) for r in (res or [])]
best = max((s for t, s in zip(txts, scores) if "TEST" in str(t).upper()), default=0.0)
print(json.dumps({"ok": best >= 0.9, "best": float(best), "txts": [str(t) for t in txts]}))
"""
_DLL_SCRIPT = """
import ctypes, os, sys
for folder in (sys.prefix, os.path.join(sys.prefix, "Scripts")):
    if os.path.exists(os.path.join(folder, "msvcp140.dll")):
        os.add_dll_directory(folder)
ctypes.WinDLL("msvcp140.dll"); ctypes.WinDLL("msvcp140_1.dll"); print("ok")
"""


def _windows_edition_is_n() -> bool:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:   # type: ignore[attr-defined]
            edition = str(winreg.QueryValueEx(key, "EditionID")[0])   # type: ignore[attr-defined]
        return edition.endswith("N")
    except Exception:  # noqa: BLE001
        return False


@check("CHK-S04", "Native libraries load (OpenCV, OCR, SVG, 3D)", kind="hard", fm=["SYS-08", "SYS-09", "SYS-21"])
def chk_s04(c: DoctorCtx) -> Outcome:
    if c.quick:
        return not_applicable_outcome("skipped in quick mode (runs in the full doctor)")
    results: dict[str, str] = {}
    failures: list[str] = []
    fixes: list[str] = []
    if winplat.IS_WINDOWS:
        rc, _out, err = run_py(_DLL_SCRIPT, timeout=30)
        if rc != 0:
            failures.append("msvcp140.dll / msvcp140_1.dll")
            fixes.append(f"Install the Microsoft Visual C++ 2015-2022 x64 redistributable: {VCREDIST}")
        results["vc_runtime"] = "ok" if rc == 0 else "missing"
    for name, code in _NATIVE_PROBES.items():
        rc, _out, err = run_py(code, timeout=60)
        results[name] = "ok" if rc == 0 else _last_line(err)[:120] or "failed"
        if rc != 0:
            failures.append(name)
            if name == "cv2" and winplat.IS_WINDOWS and _windows_edition_is_n():
                fixes.append(f"This is a Windows N/KN edition: install the Media Feature Pack ({MEDIA_FEATURE_PACK})")
    rc, out, err = run_py(_OCR_SCRIPT, timeout=120)
    ocr_ok = False
    if rc == 0:
        try:
            ocr_ok = bool(json.loads(_last_line(out)).get("ok"))
        except ValueError:
            ocr_ok = False
    results["ocr"] = "ok" if ocr_ok else (_last_line(err)[:120] or "did not read the fixture word TEST")
    if not ocr_ok:
        failures.append("OCR")
        fixes.append("Text checks need OCR. Every OCR rule fails closed (no text can be approved) until this is fixed. "
                     "Run setup.bat again.")
    optional_failed = [f for f in failures if f in OPTIONAL_NATIVE]
    failures = [f for f in failures if f not in OPTIONAL_NATIVE]
    if failures:
        return Outcome("fail", "These native components did not load: " + ", ".join(failures) + ".", " ".join(fixes) or "Run setup.bat again.",
                       {"results": results})
    if optional_failed:
        return Outcome("warn", "pymeshlab did not load, so heavy 3D models are decimated with the built-in method (slower, same limits).",
                       "Run setup.bat again if you want it; nothing is blocked.", {"results": results})
    return Outcome("pass", "OpenCV, OCR, SVG, 3D and the C++ runtime all load.", detail={"results": results})


# ------------------------------------------------------------------------------------------------------- S05
@check("CHK-S05", "HTTPS works through the Windows certificate store", kind="hard", fm=["SYS-07"])
def chk_s05(c: DoctorCtx) -> Outcome:
    injected = ssl.SSLContext.__module__.startswith("truststore")
    if not injected and not c.started_via_module:
        return not_applicable_outcome("not started through python -m duoskin, so the certificate-store hook is not installed here")
    if not injected:
        return Outcome("fail", "The Windows certificate store hook (truststore) was not installed before the network clients.",
                       "This is a bug: start the app with start.bat.")
    if c.quick or c.setup:
        return Outcome("pass", "The certificate-store hook is installed (handshakes are tested in the full doctor).")
    keys = c.keys()
    hosts = {p: h for p, h in PROVIDER_HOSTS.items() if keys.get_key(p) and c.settings.mode_of(p).value == "real"}
    if not hosts:
        return Outcome("pass", "The certificate-store hook is installed; no provider key is set, so no handshake was needed.")
    bad: list[str] = []
    stacks = 0
    for provider, host in hosts.items():
        for stack in ("httpx", "requests"):
            try:
                mod = importlib.import_module(stack)
            except ImportError:
                continue
            stacks += 1
            try:
                mod.get(f"https://{host}/", timeout=8)   # any HTTP answer, even 401/404, proves the TLS handshake worked
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "SSL" in name or "Certificate" in name or "certificate" in str(exc).lower():
                    bad.append(f"{provider} via {stack}")
    if bad:
        return Outcome("fail", "The secure connection was refused for: " + ", ".join(bad) + ".",
                       "Antivirus HTTPS scanning or a company proxy is probably re-signing traffic. Allow DuoSkin Studio or "
                       "add the proxy certificate to the Windows store.")
    return Outcome("pass", f"TLS handshakes succeeded for {len(hosts)} provider host(s).", detail={"stacks_tried": stacks})


# ------------------------------------------------------------------------------------------------------- S06
def _selftest_app() -> tuple[Any, Any]:
    from duoskin.app import create_app
    from duoskin.engine.testkit import make_client

    tmp = tempfile.TemporaryDirectory(prefix="duoskin-doctor-", ignore_cleanup_errors=True)
    app = create_app(Path(tmp.name), providers_mode="mock")
    return make_client(app, authed=False), (tmp, app)


def _close_selftest(handle: Any) -> None:
    tmp, app = handle
    with contextlib.suppress(Exception):
        app.state.rt.shutdown()
    with contextlib.suppress(Exception):
        tmp.cleanup()


@check("CHK-S06", "The server only listens on this PC and rejects strangers", kind="assert", fm=["SYS-02", "SYS-12"])
def chk_s06(c: DoctorCtx) -> Outcome:
    host = c.rt.bound_host if c.rt is not None and c.rt.port else None
    if host is not None and host != "127.0.0.1":
        return Outcome("fail", f"The server is bound to {host}, not 127.0.0.1.", "This is a bug; do not use the app on a network.")
    try:
        probe = winplat.bind_socket(0)
        probe.close()
    except OSError as exc:
        return Outcome("fail", f"Could not bind a loopback socket: {exc}", "Check the firewall or reserved port ranges (Hyper-V/WSL).")
    client, handle = _selftest_app()
    try:
        bad_host = client.get("/api/health", headers={"Host": "evil.example"})
        no_token = client.post("/api/shutdown")
        token = handle[1].state.rt.token
        bad_origin = client.post("/api/shutdown", headers={"X-DuoSkin-Token": token, "Origin": "http://evil.example"})
        ok_health = client.get("/api/health")
    finally:
        _close_selftest(handle)
    problems = []
    if bad_host.status_code != 400:
        problems.append(f"a foreign Host header got {bad_host.status_code}, not 400")
    if no_token.status_code != 403:
        problems.append(f"a POST without the token got {no_token.status_code}, not 403")
    if bad_origin.status_code != 403:
        problems.append(f"a POST from a foreign Origin got {bad_origin.status_code}, not 403")
    if ok_health.status_code != 200:
        problems.append("the local health check failed")
    if problems:
        return Outcome("fail", "Security self-test failed: " + "; ".join(problems) + ".", "This is a bug. Do not use the app until it is fixed.")
    return Outcome("pass", "Bound to 127.0.0.1 only; wrong Host, missing token and wrong Origin are all refused.",
                   detail={"exclusive_addr_use": winplat.IS_WINDOWS})


# ------------------------------------------------------------------------------------------------------- S07
@check("CHK-S07", "The page files are served correctly", kind="assert", fm=["SYS-13", "ENG-11"])
def chk_s07(c: DoctorCtx) -> Outcome:
    import mimetypes

    web = config.APP_ROOT / "duoskin" / "web"
    js_type = mimetypes.guess_type("x.js")[0]
    if js_type not in ("text/javascript", "application/javascript"):
        return Outcome("fail", f".js files would be served as '{js_type}', so the page would stay blank.", "This is a bug: the MIME fix did not run.")
    if not (web / "index.html").exists():
        return not_applicable_outcome("the web page files are not part of this build")
    client, handle = _selftest_app()
    try:
        index = client.get("/")
        js = client.get("/web/app.js") if (web / "app.js").exists() else None
    finally:
        _close_selftest(handle)
    problems = []
    if index.status_code != 200 or "no-store" not in index.headers.get("cache-control", ""):
        problems.append("index.html is not served with Cache-Control: no-store")
    if 'name="duoskin-token"' not in index.text:
        problems.append("index.html carries no token tag")
    if js is not None and not js.headers.get("content-type", "").startswith("text/javascript"):
        problems.append(f"app.js is served as '{js.headers.get('content-type')}'")
    if problems:
        return Outcome("fail", "; ".join(problems) + ".", "This is a bug: reinstall or report it.")
    return Outcome("pass", ".js is served as text/javascript and index.html as no-store with a fresh token.")


# ------------------------------------------------------------------------------------------------------- S08
@check("CHK-S08", "The data folder is writable and safe", kind="hard", fm=["SYS-14", "SYS-15"], fatal=True)
def chk_s08(c: DoctorCtx) -> Outcome:
    home = c.paths.home
    problems = winplat.lint_path(home)
    try:
        home.mkdir(parents=True, exist_ok=True)
        src = home / f".doctor-{os.getpid()}.tmp"
        dst = home / f".doctor-{os.getpid()}.ok"
        src.write_text("x", encoding="utf-8")
        winplat.replace_with_retry(src, dst)
        dst.unlink()
    except OSError as exc:
        return Outcome("fail", f"Cannot write to the data folder {home}: {exc}",
                       "Pick another folder (set DUOSKIN_HOME) or allow DuoSkin in Windows Security > Controlled folder access.")
    warnings = []
    if winplat.is_onedrive_path(home):
        warnings.append("the data folder is inside OneDrive, which can lock files and damage the database")
    exports = config.exports_root(c.settings)
    try:
        exports.mkdir(parents=True, exist_ok=True)
        probe = exports / f".doctor-{os.getpid()}.tmp"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError:
        warnings.append(f"the exports folder {exports} is not writable (Controlled Folder Access?)")
    if winplat.is_onedrive_path(exports):
        warnings.append("the exports folder is inside OneDrive")
    if problems:
        return Outcome("fail", "The data path is unsafe: " + "; ".join(problems) + ".", "Use a shorter folder such as C:\\DuoSkin.")
    if warnings:
        return Outcome("warn", "Writes work, but " + "; ".join(warnings) + ".",
                       "Move the data folder out of OneDrive (DUOSKIN_HOME) and allow DuoSkin through Controlled Folder Access.",
                       {"home": str(home), "exports": str(exports)})
    return Outcome("pass", "Write + rename test passed in the data folder; the path is short and safe.", detail={"home": str(home)})


# ------------------------------------------------------------------------------------------------------- S09
def detect_blender(settings: Any) -> Path | None:
    """Settings path, ``DUOSKIN_BLENDER``, PATH, the registry, Program Files, Steam (APP_SPEC §15.4)."""
    try:
        from duoskin.mesh import blender as mesh_blender  # the 3D track's own detector, if present

        found = getattr(mesh_blender, "detect_blender", None) or getattr(mesh_blender, "detect", None)
        if callable(found):
            result = found()
            if result:
                return Path(str(result))
    except Exception:  # noqa: BLE001, S110
        pass
    from duoskin.mesh.blender import _looks_like_blender

    typed = settings.three_d.blender_path                    # user text: only a file that is named like Blender may be run
    candidates: list[str | None] = [typed if typed and _looks_like_blender(Path(typed)) else None, os.environ.get("DUOSKIN_BLENDER"), shutil.which("blender")]
    for cand in candidates:
        if cand and Path(cand).exists():
            return Path(cand)
    if winplat.IS_WINDOWS:
        roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation",
                 Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steamapps" / "common" / "Blender"]
        for root in roots:
            exes = sorted(root.glob("Blender */blender.exe"), reverse=True) or ([root / "blender.exe"] if (root / "blender.exe").exists() else [])
            if exes:
                return exes[0]
    return None


NO_BLENDER_NOTE = "The app runs in its no-Blender mode: glTF files only, no FBX backup (APP_SPEC §10.9.1)."


def _blender(present: bool, status: Status, message: str, fix: str = "", **detail: Any) -> Outcome:
    return Outcome(status, message, fix, {"blender_present": present, **detail})


@check("CHK-S09", "Blender (optional) works the way the app needs", kind="soft", fm=["SYS-11"])
def chk_s09(c: DoctorCtx) -> Outcome:
    """SOFT (APP_SPEC §15.4): no Blender configured is ``not_applicable`` (``no_blender``); a configured Blender that fails
    is switched off (``blender_present`` False) and shown as a warning. It never blocks paid features."""
    exe = detect_blender(c.settings)
    if exe is None:
        out = not_applicable_outcome(
            "Blender is not installed (optional). " + NO_BLENDER_NOTE + " Install Blender 4.2+ "
            "(winget install BlenderFoundation.Blender) if you want FBX files and hair polish packs.")
        out.detail["blender_present"] = False
        return out
    if c.quick:
        out = not_applicable_outcome(f"Blender found at {exe}; version checks skipped in quick mode")
        out.detail["blender_present"] = True
        return out
    off = "Blender is switched off. " + NO_BLENDER_NOTE
    try:
        proc = subprocess.run([str(exe), "--version"], check=False, capture_output=True, encoding="utf-8", errors="replace", timeout=60,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _blender(False, "warn", f"Blender at {exe} did not run: {exc}. {off}",
                        "Reinstall Blender or clear the Blender path in Settings.", path=str(exe))
    m = re.search(r"Blender\s+(\d+)\.(\d+)", proc.stdout or "")
    version = f"{m.group(1)}.{m.group(2)}" if m else "unknown"
    tested = c.settings.three_d.blender_tested_versions
    if version not in tested:
        return _blender(False, "warn", f"Blender {version} is not in the tested set ({', '.join(tested)}). {off}",
                        "Install a tested Blender (4.2 LTS or newer) or leave Blender unset.", path=str(exe), version=version)
    try:
        bad = subprocess.run([str(exe), "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "3",
                              "--python-expr", "raise RuntimeError('doctor')"], check=False, capture_output=True, timeout=120,
                             stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _blender(False, "warn", f"Blender could not run a script: {exc}. {off}", "Reinstall Blender.", path=str(exe))
    if bad.returncode != 3:
        return _blender(False, "warn", f"Blender exited {bad.returncode} when its script failed (expected 3). {off}",
                        "Use blender.exe (not blender-launcher.exe) in Settings.", path=str(exe), version=version)
    return _blender(True, "pass", f"Blender {version} runs scripts and reports failures. (The colour-chart check is run by the 3D track.)",
                    path=str(exe), version=version)


# ------------------------------------------------------------------------------------------------------- S10
@check("CHK-S10", "Claude is reachable and the planner schema compiles", kind="hard", fm=["LLM-03", "LLM-04"])
def chk_s10(c: DoctorCtx) -> Outcome:
    if c.settings.mode_of("anthropic").value != "real":
        return not_applicable_outcome("the Anthropic provider is in mock or disabled mode")
    if not c.keys().get_key("anthropic"):
        return not_applicable_outcome("no Anthropic key is set yet (add it in Settings > Keys); real planning needs it")
    if c.quick or c.setup:
        return not_applicable_outcome("network probe skipped in quick/setup mode")
    try:
        from duoskin.providers import registry as provider_registry

        adapter = provider_registry.get("anthropic")
        probe = getattr(adapter, "startup_probe", None)
    except ImportError:
        return Outcome("warn", "The provider layer is not installed in this build, so Claude could not be probed.", "")
    except Exception as exc:  # noqa: BLE001
        return Outcome("fail", f"Could not build the Claude adapter: {redact(str(exc))[:200]}", "Check the key in Settings.")
    if probe is None:
        return Outcome("warn", "The Claude adapter has no startup probe yet; model access and the schema smoke test were not checked.", "")
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001
        return Outcome("fail", f"The Claude probe failed: {redact(str(exc))[:200]}", "Check the key and your internet connection.")
    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    msg = str(result.get("message", "")) if isinstance(result, dict) else ""
    return Outcome("pass" if ok else "fail", msg or ("Claude answered and the schemas compile." if ok else "The Claude probe reported a problem."),
                   "" if ok else "Check the key and the model ids in Settings.")


# ------------------------------------------------------------------------------------------------------- S11 (split)
def _flatten_strings(node: Any) -> list[str]:
    """Every string *value* inside lists and dict values (dict keys are labels, not terms)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _flatten_strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _flatten_strings(v)]
    return []


def _banned_terms(data: Any) -> list[str]:
    """``banned_terms.json`` is ``{"version", "groups": {group: [terms]}}``; a plain list or ``{"terms": [...]}`` also works."""
    if isinstance(data, dict) and isinstance(data.get("groups"), dict):
        data = data["groups"]
    elif isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in ("version", "note")}
    return [s for s in _flatten_strings(data) if len(s.strip()) > 1]


def _colour_names(data: Any) -> list[str]:
    """``colour_names.json`` is ``{"version", "note", "colours": {name: hex}}``; a list of names also works."""
    colours = data.get("colours", data) if isinstance(data, dict) else data
    if isinstance(colours, dict):
        return [k for k in colours if isinstance(k, str)]
    return _flatten_strings(colours)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def kit_flags(paths: config.DataPaths) -> dict[str, Any]:
    """Availability flags for the kits (APP_SPEC §5.3): the manifest's flags if ``kits/manifest.json`` has them, then what
    is actually on disk. These flags route the app to its reduced modes; none of them blocks anything."""
    flags: dict[str, Any] = {"head_base_present": False, "house_style_present": False, "body_base_present": False,
                             "hair_kit_empty": True}
    manifest = paths.kits_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            given = data.get("flags", data) if isinstance(data, dict) else {}
            for key in flags:
                if isinstance(given, dict) and isinstance(given.get(key), bool):
                    flags[key] = given[key]
        except (OSError, ValueError):
            pass
    kits = paths.kits_dir
    if list((kits / "head_base").glob("*/zones.json")) or (kits / "head_base" / "zones.json").exists():
        flags["head_base_present"] = True
    if list((kits / "style").glob("house_style_v*.png")):
        flags["house_style_present"] = True
    if (kits / "body_base" / "body.fbx").exists():
        flags["body_base_present"] = True
    if list((kits / "hair").glob("*/style.json")):
        flags["hair_kit_empty"] = False
    return flags


def not_applicable_outcome(reason: str) -> Outcome:
    return Outcome("na", reason)


@check("CHK-S11", "Kits load and the colour dictionary is clean", kind="hard", fm=["PLN-01", "PRM-06"])
def chk_s11(c: DoctorCtx) -> Outcome:
    """HARD part only: manifest loads, kit enums import, colour names contain no banned term. Missing kits never fail."""
    notes: list[str] = []
    ran_any = False
    manifest = c.paths.kits_dir / "manifest.json"
    if manifest.exists():
        ran_any = True
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("the manifest is not a JSON object")
            notes.append("kit manifest loads")
        except (OSError, ValueError, TypeError) as exc:
            return Outcome("fail", f"The kit manifest is damaged: {exc}", "Run: python -m duoskin build-kit-manifest")
    if importlib.util.find_spec("duoskin.models.kitenums") is not None:
        ran_any = True
        try:
            kitenums = importlib.import_module("duoskin.models.kitenums")
            # EyeShapeKit and MouthKit come from the head base, or from builtin_kits/face_canvas_default.json when there is none
            values = kitenums.enum_values(kitenums.load_inventory(manifest if manifest.exists() else None))
            empty = sorted(k for k in ("EyeShapeKit", "MouthKit") if not values.get(k))
            if empty:
                raise ValueError("no values for " + ", ".join(empty))
            notes.append("kit enums build")
        except Exception as exc:  # noqa: BLE001
            return Outcome("fail", f"The kit enums could not be built: {redact(str(exc))[:200]}", "Run: python -m duoskin build-kit-manifest")
    data_dir = config.APP_ROOT / "duoskin" / "data"
    colour_file, banned_file = data_dir / "colour_names.json", data_dir / "banned_terms.json"
    if colour_file.exists() and banned_file.exists():
        ran_any = True
        try:
            colours = [_norm(s) for s in _colour_names(json.loads(colour_file.read_text(encoding="utf-8")))]
            banned = [_norm(s) for s in _banned_terms(json.loads(banned_file.read_text(encoding="utf-8")))]
            extra = c.paths.user_data_dir / "banned_terms.extra.json"
            if extra.exists():
                banned += [_norm(s) for s in _banned_terms(json.loads(extra.read_text(encoding="utf-8")))]
        except (OSError, ValueError) as exc:
            return Outcome("fail", f"A data file could not be read: {exc}", "Reinstall the app files.")
        hits = sorted({b for b in banned for name in colours if re.search(rf"(?<!\w){re.escape(b)}(?!\w)", name)})
        if hits:
            return Outcome("fail", f"{len(hits)} colour name(s) contain a banned term.", "Edit data/colour_names.json (or your banned_terms.extra.json).",
                           {"count": len(hits)})
        notes.append("colour names are clean")
    if not ran_any:
        return not_applicable_outcome("no kits yet and the kit/colour data modules are not part of this build; built-in kits apply")
    return Outcome("pass", "; ".join(notes).capitalize() + ".")


# ------------------------------------------------------------------------------------------------------- S12
@check("CHK-S12", "Secrets never reach logs", kind="assert", fm=["SYS-01", "SYS-20"])
def chk_s12(c: DoctorCtx) -> Outcome:
    from duoskin.keystore import MAX_KEY_LEN, KeyRejected, _clean

    problems: list[str] = []
    samples = ["sk-ant-api03-" + "A1b2C3d4" * 4, "tsk_" + "Z9y8X7w6" * 3, "AIza" + "Q1w2E3r4T5y6U7i8O9p0", "Bearer abcdef1234567890"]
    stored = list(c.keys().stored_values().values())   # never printed
    for secret in [*samples, *stored]:
        line = redact(f"calling provider with key {secret} now")
        if secret in line:
            problems.append("a key shape was not masked")
            break
    try:
        _clean("x" * (MAX_KEY_LEN + 1))
        problems.append("a secret longer than 1280 characters was accepted")
    except KeyRejected:
        pass
    if problems:
        return Outcome("fail", "; ".join(problems) + ".", "This is a bug: do not paste keys until it is fixed.")
    return Outcome("pass", f"The log redactor masked every test pattern and {len(stored)} stored key(s); over-long secrets are refused.")


# ------------------------------------------------------------------------------------------------------- S13
@check("CHK-S13", "No file shadows a standard-library module", kind="assert", fm=["SYS-19"], fatal=True)
def chk_s13(c: DoctorCtx) -> Outcome:
    """A file named like a stdlib module breaks imports when its folder is on ``sys.path`` (a stray ``inspect.py`` once broke
    numpy and anthropic). Folders that can be on ``sys.path``: the app root (the working directory of ``-m duoskin``), ``tools``,
    ``tests`` and the package folder itself (script-mode runs). Those are a FAILURE. A deeper module such as
    ``duoskin/engine/gc.py`` is namespaced (``duoskin.engine.gc``) and cannot shadow ``gc`` with ``-m``; it is only listed."""
    root = config.APP_ROOT
    pkg = root / "duoskin"
    risky: list[str] = []
    namespaced: list[str] = []
    for path in pkg.rglob("*"):
        if "__pycache__" in path.parts:
            continue
        is_mod = path.suffix == ".py" and path.stem not in ("__init__", "__main__")
        is_pkg = path.is_dir() and (path / "__init__.py").exists()
        if (is_mod or is_pkg) and (path.stem if is_mod else path.name) in sys.stdlib_module_names:
            rel = str(path.relative_to(root)).replace("\\", "/")
            (risky if path.parent == pkg else namespaced).append(rel)
    for folder in (root, root / "tools", root / "tests"):
        for path in folder.glob("*.py") if folder.exists() else []:
            if path.stem in sys.stdlib_module_names:
                risky.append(str(path.relative_to(root)).replace("\\", "/"))
    if risky:
        return Outcome("fail", "These files have the same name as a standard Python module and break imports: " + ", ".join(sorted(risky)) + ".",
                       "Rename or delete them.", {"files": sorted(risky)})
    notes = []
    if not c.started_via_module:
        notes.append("not started with -m duoskin (fine for tests and embedding)")
    modules = ("anthropic", "openai", "numpy", "PIL")
    if c.quick:
        missing = [m for m in modules if importlib.util.find_spec(m) is None]
    else:
        rc, _out, err = run_py("import anthropic, openai, numpy, PIL; print('ok')", timeout=120)
        missing = [] if rc == 0 else [_last_line(err)[:160] or "import failed"]
    if missing:
        return Outcome("fail", "Core packages are missing or broken: " + ", ".join(missing) + ".", "Run setup.bat again.")
    if namespaced:
        notes.append(f"{len(namespaced)} package module(s) share a stdlib name but are namespaced and safe with -m: " + ", ".join(sorted(namespaced)))
    return Outcome("pass", "No file can shadow a standard-library module; anthropic, openai, numpy and Pillow import" +
                   (f" ({'; '.join(notes)})" if notes else "") + ".", detail={"namespaced_stdlib_names": sorted(namespaced)})


# ------------------------------------------------------------------------------------------------------- S14
OPTIONAL_COMPONENTS_FILE = "optional_components.json"
DREAMSIM_FILE = "dreamsim.onnx"
SHA_HEX = re.compile(r"^[0-9a-f]{64}$")
FIXTURE_TOL_FALLBACK = 0.01      # dreamsim.fixture_tol in checks/thresholds.py [DES]


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_optional_components() -> list[dict[str, Any]]:
    """The rows of ``duoskin/data/optional_components.json`` (APP_SPEC §4.4): ``id``, ``path`` (under DATA) and ``sha256``.
    Accepts a list, ``{"components": [...]}`` or ``{id: {...}}``; a missing or unreadable file gives ``[]``."""
    try:
        data = json.loads((config.APP_ROOT / "duoskin" / "data" / OPTIONAL_COMPONENTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows: Any = data.get("components", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = [{"id": k, **v} for k, v in rows.items() if isinstance(v, dict)]
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def pinned_sha(component_id: str) -> str | None:
    """The sha256 pinned in the manifest for a component, or None when none is pinned yet ([UNVERIFIED] until the first export)."""
    for row in load_optional_components():
        if component_id in str(row.get("id", row.get("name", ""))).lower():
            sha = str(row.get("sha256") or "").lower()
            return sha if SHA_HEX.match(sha) else None
    return None


_DREAMSIM_PROBE = """
import json, sys
from pathlib import Path
from PIL import Image
from duoskin.imaging import similarity as S
model = S.load_dreamsim(sys.argv[1])
if model is None:
    print(json.dumps({"loaded": False})); raise SystemExit(0)
out = []
for a, b, expected in json.loads(sys.argv[2]):
    with Image.open(a) as ia, Image.open(b) as ib:
        d = float(model.distance(ia.convert("RGBA"), ib.convert("RGBA")))
    out.append({"expected": expected, "distance": d})
print(json.dumps({"loaded": True, "pairs": out}))
"""


def _fixture_pairs(sidecar: Path) -> tuple[list[tuple[str, str, float]], str]:
    """The fixture pairs of ``dreamsim.onnx.json`` (``fixture_expectations: [{pair: [a, b], distance}]``) whose images exist
    under ``tests/fixtures/dreamsim/``; ``(pairs, problem)``."""
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        rows = meta["fixture_expectations"]
    except (OSError, ValueError, KeyError, TypeError):
        return [], f"{sidecar.name} is missing or has no fixture_expectations"
    base = config.APP_ROOT / "tests" / "fixtures" / "dreamsim"
    pairs: list[tuple[str, str, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            a, b = row["pair"]
            pa, pb = base / str(a), base / str(b)
            if pa.is_file() and pb.is_file():
                pairs.append((str(pa), str(pb), float(row["distance"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pairs, "" if pairs else f"no fixture pair of {sidecar.name} was found in tests/fixtures/dreamsim"


def _dreamsim_degraded(message: str, fix: str = "", **detail: Any) -> Outcome:
    return Outcome("warn", message + " The clone check will run degraded (pHash + palette overlap + plan distance); "
                   "Gate 3 shows \"clone check degraded\" before you pick.",
                   fix or "Install or repair it from Settings > Optional components.",
                   {"clone_check_mode": "degraded", "dreamsim_present": False, **detail})


def probe_dreamsim(model: Path, pairs: list[tuple[str, str, float]]) -> tuple[bool, list[dict[str, float]], str]:
    """Load the model and measure the fixture pairs in a child process (a native crash never reaches the server).
    Returns ``(loaded, [{expected, distance}], problem)``. Tests replace this function."""
    rc, out, err = run_py(_DREAMSIM_PROBE, timeout=180, args=[str(model), json.dumps(pairs)], cwd=config.APP_ROOT)
    if rc != 0:
        return False, [], _last_line(err)[:200] or f"the probe exited {rc}"
    try:
        data = json.loads(_last_line(out))
    except ValueError:
        return False, [], "the probe printed nothing usable"
    return bool(data.get("loaded")), list(data.get("pairs", [])), "" if data.get("loaded") else "onnxruntime could not load the file"


@check("CHK-S14", "DreamSim model for the clone check (optional)", kind="soft", fm=["DUO-01", "IMG-15", "FACE-11", "DUO-10"])
def chk_s14(c: DoctorCtx) -> Outcome:
    """SOFT (§4.4): ``dreamsim.onnx`` exists with the pinned sha256, loads on onnxruntime, and a fixture pair returns the
    expected distance within ``dreamsim.fixture_tol``. Any failure switches it off (``dreamsim_present`` False); never blocks."""
    model = c.paths.models_dir / DREAMSIM_FILE
    if not model.is_file():
        return _dreamsim_degraded("dreamsim.onnx is not installed.", "Install it from Settings > Optional components when it is available.")
    expected_sha = pinned_sha("dreamsim")
    note = "" if expected_sha else " (no sha256 is pinned in optional_components.json yet)"
    if c.quick:
        return Outcome("pass", "dreamsim.onnx is present (hash, load and fixture checks skipped in quick mode).",
                       detail={"clone_check_mode": "full", "dreamsim_present": True})
    try:
        actual = sha256_file(model)
    except OSError as exc:
        return _dreamsim_degraded(f"dreamsim.onnx could not be read ({exc}).")
    if expected_sha and actual != expected_sha:
        return _dreamsim_degraded("dreamsim.onnx does not match the sha256 in optional_components.json, so it is switched off.",
                                  "Delete the file and install it again from Settings > Optional components.", sha256=actual)
    pairs, problem = _fixture_pairs(model.with_name(DREAMSIM_FILE + ".json"))
    if not pairs:
        return _dreamsim_degraded(f"The fixture check could not run: {problem}.", sha256=actual)
    try:
        from duoskin.checks import thresholds

        tol = float(thresholds.get("dreamsim.fixture_tol"))
    except Exception:  # noqa: BLE001
        tol = FIXTURE_TOL_FALLBACK
    loaded, measured, problem = probe_dreamsim(model, pairs)
    if not loaded:
        return _dreamsim_degraded(f"dreamsim.onnx did not load ({problem}).", sha256=actual)
    off = [m for m in measured if abs(float(m["distance"]) - float(m["expected"])) > tol]
    if off or not measured:
        return _dreamsim_degraded(f"dreamsim.onnx loads but the fixture pair is off by more than {tol} "
                                  f"(got {off[0]['distance']:.4f}, expected {off[0]['expected']:.4f})." if off else
                                  "dreamsim.onnx loads but returned no fixture distances.", sha256=actual)
    return Outcome("pass", f"dreamsim.onnx loads and {len(measured)} fixture pair(s) match within {tol}{note}.",
                   detail={"clone_check_mode": "full", "dreamsim_present": True, "sha256": actual, "pairs": len(measured)})


# ------------------------------------------------------------------------------------------------------- S15
def _outcome_flag(c: DoctorCtx, check_id: str, flag: str, fallback: bool) -> bool:
    out = c.outcomes.get(check_id)
    value = out.detail.get(flag) if out is not None else None
    return bool(value) if isinstance(value, bool) else fallback


def write_manifest_flags(paths: config.DataPaths, flags: dict[str, Any]) -> bool:
    """Merge ``flags`` into an existing, readable ``kits/manifest.json`` (never creates one). True when the file changed."""
    manifest = paths.kits_dir / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    current = data.get("flags") if isinstance(data.get("flags"), dict) else {}
    merged = {**current, **flags}
    if merged == current:
        return False
    data["flags"] = merged
    winplat.atomic_write(manifest, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return True


def switched_off_components(paths: config.DataPaths) -> list[str]:
    """Installed optional files (matting, OCR) whose sha256 differs from ``optional_components.json``. A missing file just
    means the feature is absent; a component with no pinned sha is not judged."""
    bad: list[str] = []
    for row in load_optional_components():
        cid = str(row.get("id", row.get("name", "")))
        if "dreamsim" in cid.lower() or not row.get("path"):
            continue
        sha = str(row.get("sha256") or "").lower()
        target = paths.home / str(row["path"]).replace("\\", "/")
        if SHA_HEX.match(sha) and target.is_file():
            try:
                if sha256_file(target) != sha:
                    bad.append(cid)
            except OSError:
                bad.append(cid)
    return sorted(bad)


@check("CHK-S15", "Kits and optional components: what is available", kind="soft", fm=["PLN-01", "FACE-02", "BODY-01", "PRM-07", "ENG-08"])
def chk_s15(c: DoctorCtx) -> Outcome:
    """Availability of the kits and components, shown as warnings with the route taken (§0.5, §2 S25). Writes the flags
    into the kit manifest (when there is one) and into the report. Nothing here ever blocks a paid feature."""
    flags = kit_flags(c.paths)
    flags["makeup"] = "unavailable"
    flags["dreamsim_present"] = _outcome_flag(c, "CHK-S14", "dreamsim_present", (c.paths.models_dir / DREAMSIM_FILE).is_file())
    flags["blender_present"] = _outcome_flag(c, "CHK-S09", "blender_present", detect_blender(c.settings) is not None)
    routes: list[str] = []
    if not flags["head_base_present"]:
        routes.append("no head base: 2D face previews, no Head item")
    if not flags["body_base_present"]:
        routes.append("no body base: the standard Block body, body_colors.json only")
    if not flags["house_style_present"]:
        routes.append("no house style sheet: run S0 (setup wizard step 6); other image calls use the STYLE text block only")
    if flags["hair_kit_empty"]:
        routes.append("no hair kit: every hair is hair_custom (Tripo or a model of your own)")
    if not flags["dreamsim_present"]:
        routes.append("no DreamSim: the clone check runs degraded")
    if not flags["blender_present"]:
        routes.append("no Blender: the no-Blender mode (glTF only, no FBX)")
    off = switched_off_components(c.paths)
    if off:
        routes.append("switched off (the file does not match its sha256): " + ", ".join(off))
    try:
        wrote = write_manifest_flags(c.paths, flags)
    except OSError as exc:
        log.warning("could not write the kit flags into the manifest: %s", exc)
        wrote = False
    detail = {"flags": flags, "routes": routes, "switched_off": off, "manifest_updated": wrote}
    if routes:
        return Outcome("warn", "Some optional parts are missing, which is normal on a fresh install. Routes taken: " + "; ".join(routes) + ".",
                       "Nothing here blocks you. Add the parts later from the Library and Settings pages.", detail)
    return Outcome("pass", "Every kit and optional component is available.", detail=detail)


# ------------------------------------------------------------------------------------------------------- runner
def _to_check_result(d: CheckDef, o: Outcome) -> CheckResult:
    if o.status == "na":
        return not_applicable(d.id, d.kind, o.message[:300] or "not applicable", fm_ids=d.fm_ids)
    passed = o.status == "pass" or (o.status == "warn" and d.kind != "soft")   # a SOFT warning is "not passed", never blocking
    return CheckResult(check_id=d.id, fm_ids=d.fm_ids, kind=d.kind, passed=passed, evidence=o.message[:500], ran=True,
                       thresholds_version="v1")


def run_doctor(rt: Runtime | None = None, *, home: Path | None = None, setup: bool = False, quick: bool = False,
               only: set[str] | None = None) -> dict[str, Any]:
    """Run the checks and return the report dict (also what ``/api/doctor`` serves). Never raises."""
    paths = rt.paths if rt is not None else config.paths(home)
    settings = rt.effective_settings() if rt is not None else config.load_settings(paths.home)
    ctx = DoctorCtx(paths=paths, settings=settings, rt=rt, quick=quick, setup=setup)
    items: list[dict[str, Any]] = []
    for d in _CHECKS:
        if only is not None and d.id not in only:
            continue
        started = time.monotonic()
        try:
            outcome = d.fn(ctx)
            ctx.outcomes[d.id] = outcome
            result = _to_check_result(d, outcome)
        except Exception as exc:
            log.exception("doctor check %s crashed", d.id)
            outcome = Outcome("fail", f"The check itself crashed: {type(exc).__name__}: {redact(str(exc))[:200]}", "This is a bug; export diagnostics.")
            result = not_run(d.id, d.kind, f"{type(exc).__name__}", fm_ids=d.fm_ids)
        status = outcome.status
        # a SOFT check can only warn; HARD/ASSERT failures block
        if status == "fail" and d.kind == "soft":
            status = "warn"
        # blocking = a HARD or ASSERT result with passed=False (a crash is ``not_run``, which counts); SOFT and N/A never block
        blocking = d.kind in ("hard", "assert") and not result.passed
        if blocking:
            status = "fail"
        items.append({"id": d.id, "title": d.title, "kind": d.kind, "status": status, "message": outcome.message, "fix": outcome.fix,
                      "fm_ids": d.fm_ids, "blocking": blocking, "fatal": bool(d.fatal and status == "fail"),
                      "detail": outcome.detail, "duration_ms": int((time.monotonic() - started) * 1000),
                      "check": result.model_dump(mode="json")})
    summary = {"passed": sum(1 for i in items if i["status"] == "pass"), "warnings": sum(1 for i in items if i["status"] == "warn"),
               "failed": sum(1 for i in items if i["status"] == "fail"), "not_applicable": sum(1 for i in items if i["status"] == "na")}
    blocks = any(i["blocking"] for i in items)
    broken = any(i["fatal"] for i in items)
    exit_code = 3 if broken else (2 if blocks else 0)       # warnings alone give 0 (APP_SPEC §15.3)
    s15 = next((i["detail"].get("flags") for i in items if i["id"] == "CHK-S15" and i["detail"].get("flags")), None)
    flags = dict(s15) if s15 else {**kit_flags(paths), "makeup": "unavailable",
                                     "dreamsim_present": (paths.models_dir / DREAMSIM_FILE).is_file(),
                                     "blender_present": detect_blender(settings) is not None}
    flags["clone_check_mode"] = "full" if flags.get("dreamsim_present") else "degraded"
    flags["degraded_clone_check"] = flags["clone_check_mode"] == "degraded"
    flags["blender"] = (str(detect_blender(settings)) if flags.get("blender_present") else None) if not quick else None
    report = {"ts": iso_utc(utcnow()), "version": __version__, "python": platform.python_version(), "platform": platform.platform(),
              "setup": setup, "quick": quick, "checks": items, "summary": summary, "blocks_paid_features": blocks,
              "broken_install": broken, "exit_code": exit_code, "flags": flags}
    return report


def format_report(report: dict[str, Any]) -> str:
    """Plain-text report for the console and ``doctor.bat``."""
    icon = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "na": "[ -- ]"}
    lines = [f"DuoSkin Studio doctor, version {report['version']}, Python {report['python']}", ""]
    for i in report["checks"]:
        lines.append(f"{icon[i['status']]} {i['id']}  {i['title']}")
        lines.append(f"         {i['message']}")
        if i["fix"] and i["status"] in ("fail", "warn"):
            lines.append(f"         What to do: {i['fix']}")
    s = report["summary"]
    lines += ["", f"{s['passed']} passed, {s['warnings']} warnings, {s['failed']} failed, {s['not_applicable']} not applicable."]
    if report["broken_install"]:
        lines.append("The install is broken: run setup.bat again.")
    elif report["blocks_paid_features"]:
        lines.append("Paid features are blocked until the FAIL items above are fixed. Demo mode still works.")
    elif s["warnings"]:
        lines.append("Nothing blocks you. The warnings only mean some optional parts are missing (the app uses its reduced modes).")
    else:
        lines.append("All good.")
    return "\n".join(lines)

