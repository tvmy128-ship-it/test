"""``duoskin doctor``: the startup checks CHK-S01..S14 (FAILURE_MODES §7.0, with the issue-file fixes).

Each check returns an ``Outcome``; the runner turns it into a ``CheckResult`` (fail closed: an exception or a missing
dependency gives ``ran=False, passed=False``) and a human-readable ``DoctorItem``. The report says plainly what is wrong and
what to do, and decides three things:

* ``blocks_paid_features``: a HARD or ASSERT check failed. Real-provider paid steps then do not run (mock steps still do).
* ``broken_install`` (exit code 3): the interpreter, the data folder or the package itself is broken.
* ``exit_code``: 0 all good, 2 warnings or failures that block paid features, 3 broken install.

**A fresh install is never blocked.** CHK-S11 is split (issue file #2): only the kit manifest load, the kit enums and the
colour-name dictionary are HARD. A missing house-style sheet (``CHK-S11b``), a missing head base (``CHK-S11c``) and a
missing ``dreamsim.onnx`` (``CHK-S14``) only *warn* and route to the reduced modes (S0 bootstrap allowed with no sheet; the
2D face canvas "no head base"; the degraded clone check).

``quick=True`` skips the slow subprocess probes (native imports, OCR, TLS, Blender): used by tests and by the setup run.
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


def run_py(code: str, *, timeout: int = SUBPROCESS_TIMEOUT_S, args: list[str] | None = None) -> tuple[int, str, str]:
    """Run ``python -c code`` in a child process (native imports never run in the server process)."""
    env = {**os.environ, "PYTHONUTF8": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code, *(args or [])], capture_output=True,   # noqa: S603
                              encoding="utf-8", errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL)
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

    with tempfile.TemporaryDirectory(prefix="duoskin-") as tmp:
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
    with tempfile.TemporaryDirectory(prefix="duoskin-") as tmp:
        p = Path(tmp) / "t\u00e9xt.txt"
        p.write_text(sample, encoding="utf-8")
        if p.read_text(encoding="utf-8") != sample:
            return Outcome("fail", "A UTF-8 text file did not read back the same.", "Check the disk and antivirus.")
    code = "import sys; sys.stdout.write(sys.argv[1])"
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code, sample], capture_output=True, encoding="utf-8",   # noqa: S603
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
        if version not in ((3, 14), (3, 13)):
            problems.append(f"Python {version[0]}.{version[1]} is not supported (use 3.14 or 3.13)")
    elif version < (3, 11):
        problems.append(f"Python {version[0]}.{version[1]} is too old for development (3.11+)")
    detail = {"executable": sys.executable, "version": platform.python_version(), "platform": sysconfig.get_platform(),
              "venv": in_venv}
    if problems:
        return Outcome("fail", "Python problem: " + "; ".join(problems) + ".",
                       "Run setup.bat to build the .venv with 64-bit Python 3.14 (or 3.13) from python.org.", detail)
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
        import winreg   # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:   # type: ignore[attr-defined]
            edition = str(winreg.QueryValueEx(key, "EditionID")[0])   # type: ignore[attr-defined]
        return edition.endswith("N")
    except Exception:   # noqa: BLE001
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
    if failures:
        return Outcome("fail", "These native components did not load: " + ", ".join(failures) + ".", " ".join(fixes) or "Run setup.bat again.",
                       {"results": results})
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
            except Exception as exc:   # noqa: BLE001
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

    tmp = tempfile.TemporaryDirectory(prefix="duoskin-doctor-")
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
        from duoskin.mesh import blender as mesh_blender   # the 3D track's own detector, if present

        found = getattr(mesh_blender, "detect_blender", None) or getattr(mesh_blender, "detect", None)
        if callable(found):
            result = found()
            if result:
                return Path(str(result))
    except Exception:   # noqa: BLE001
        pass
    candidates: list[str | None] = [settings.three_d.blender_path, os.environ.get("DUOSKIN_BLENDER"), shutil.which("blender")]
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


@check("CHK-S09", "Blender (optional) works the way the app needs", kind="hard", fm=["SYS-11"])
def chk_s09(c: DoctorCtx) -> Outcome:
    exe = detect_blender(c.settings)
    if exe is None:
        return not_applicable_outcome(
            "Blender is not installed (optional). FBX export and hair polish packs are unavailable; the app uses glTF. "
            "Install Blender 4.2+ (winget install BlenderFoundation.Blender) if you want them.")
    if c.quick:
        return not_applicable_outcome(f"Blender found at {exe}; version checks skipped in quick mode")
    try:
        proc = subprocess.run([str(exe), "--version"], capture_output=True, encoding="utf-8", errors="replace", timeout=60,   # noqa: S603
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Outcome("fail", f"Blender at {exe} did not run: {exc}", "Reinstall Blender or clear the Blender path in Settings.")
    m = re.search(r"Blender\s+(\d+)\.(\d+)", proc.stdout or "")
    version = f"{m.group(1)}.{m.group(2)}" if m else "unknown"
    tested = c.settings.three_d.blender_tested_versions
    if version not in tested:
        return Outcome("fail", f"Blender {version} is not in the tested set ({', '.join(tested)}).",
                       "Install a tested Blender (4.2 LTS or newer) or leave Blender unset.", {"path": str(exe)})
    try:
        bad = subprocess.run([str(exe), "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "3",   # noqa: S603
                              "--python-expr", "raise RuntimeError('doctor')"], capture_output=True, timeout=120,
                             stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Outcome("fail", f"Blender could not run a script: {exc}", "Reinstall Blender.")
    if bad.returncode != 3:
        return Outcome("fail", f"Blender exited {bad.returncode} when its script failed (expected 3).",
                       "Use blender.exe (not blender-launcher.exe) in Settings.", {"path": str(exe)})
    return Outcome("pass", f"Blender {version} runs scripts and reports failures. (Colour-chart check is done by the 3D track.)",
                   detail={"path": str(exe), "version": version})


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
    except Exception as exc:   # noqa: BLE001
        return Outcome("fail", f"Could not build the Claude adapter: {redact(str(exc))[:200]}", "Check the key in Settings.")
    if probe is None:
        return Outcome("warn", "The Claude adapter has no startup probe yet; model access and the schema smoke test were not checked.", "")
    try:
        result = probe()
    except Exception as exc:   # noqa: BLE001
        return Outcome("fail", f"The Claude probe failed: {redact(str(exc))[:200]}", "Check the key and your internet connection.")
    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    msg = str(result.get("message", "")) if isinstance(result, dict) else ""
    return Outcome("pass" if ok else "fail", msg or ("Claude answered and the schemas compile." if ok else "The Claude probe reported a problem."),
                   "" if ok else "Check the key and the model ids in Settings.")


# ------------------------------------------------------------------------------------------------------- S11 (split)
def _flatten_strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        out = [k for k in node if isinstance(k, str)]
        for v in node.values():
            out += _flatten_strings(v)
        return out
    if isinstance(node, list):
        return [s for v in node for s in _flatten_strings(v)]
    return []


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
                raise ValueError("the manifest is not a JSON object")
            notes.append("kit manifest loads")
        except (OSError, ValueError) as exc:
            return Outcome("fail", f"The kit manifest is damaged: {exc}", "Run: python -m duoskin build-kit-manifest")
    if importlib.util.find_spec("duoskin.models.kitenums") is not None:
        ran_any = True
        try:
            importlib.import_module("duoskin.models.kitenums")
            notes.append("kit enums build")
        except Exception as exc:   # noqa: BLE001
            return Outcome("fail", f"The kit enums could not be built: {redact(str(exc))[:200]}", "Run: python -m duoskin build-kit-manifest")
    data_dir = config.APP_ROOT / "duoskin" / "data"
    colour_file, banned_file = data_dir / "colour_names.json", data_dir / "banned_terms.json"
    if colour_file.exists() and banned_file.exists():
        ran_any = True
        try:
            colours = [_norm(s) for s in _flatten_strings(json.loads(colour_file.read_text(encoding="utf-8")))]
            banned = [_norm(s) for s in _flatten_strings(json.loads(banned_file.read_text(encoding="utf-8"))) if len(s.strip()) > 1]
            extra = c.paths.user_data_dir / "banned_terms.extra.json"
            if extra.exists():
                banned += [_norm(s) for s in _flatten_strings(json.loads(extra.read_text(encoding="utf-8"))) if len(s.strip()) > 1]
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


@check("CHK-S11b", "House style sheet (needed for the best results; optional to start)", kind="soft", fm=["PRM-06"])
def chk_s11b(c: DoctorCtx) -> Outcome:
    flags = kit_flags(c.paths)
    if flags["house_style_present"]:
        return Outcome("pass", "A house style sheet is in place.", detail={"flags": flags})
    return Outcome("warn", "No house style sheet yet. That is normal on a fresh install: the first-time setup (S0) builds it, and plans work without one.",
                   "Open the setup wizard and run the house-style step when you are ready.", {"flags": flags})


@check("CHK-S11c", "Head base (optional)", kind="soft", fm=["FACE-02"])
def chk_s11c(c: DoctorCtx) -> Outcome:
    flags = kit_flags(c.paths)
    if flags["head_base_present"]:
        return Outcome("pass", "A head base with zones.json is installed.", detail={"flags": flags})
    return Outcome("warn", "No head base yet. Faces use the 2D face canvas and show \"2D preview - no head base\"; the Head item is left out of the upload kit.",
                   "Add a head base kit later (Library) to get faces on the real head.", {"flags": flags})


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
@check("CHK-S14", "DreamSim model for the clone check (optional)", kind="soft", fm=["X19"])
def chk_s14(c: DoctorCtx) -> Outcome:
    model = c.paths.models_dir / "dreamsim.onnx"
    if not model.exists():
        return Outcome("warn", "dreamsim.onnx is not installed, so the clone check runs in DEGRADED mode (pHash + palette overlap + plan "
                               "distance). Gate 3 will say \"clone check degraded\" before you pick.",
                       "Install it from Settings > Optional components when it is available.", {"clone_check_mode": "degraded"})
    if c.quick:
        return Outcome("pass", "dreamsim.onnx is present (load test skipped in quick mode).", detail={"clone_check_mode": "full"})
    code = ("import sys, onnxruntime as ort\n"
            "s = ort.InferenceSession(sys.argv[1], providers=['CPUExecutionProvider'])\n"
            "print(len(s.get_inputs()), len(s.get_outputs()))")
    rc, _out, err = run_py(code, timeout=120, args=[str(model)])
    if rc != 0:
        return Outcome("warn", "dreamsim.onnx is present but did not load; the clone check stays in degraded mode.",
                       "Re-download the model. " + _last_line(err)[:160], {"clone_check_mode": "degraded"})
    return Outcome("pass", "dreamsim.onnx loads. (The fixture-pair distance check ships with the model export tool.)",
                   detail={"clone_check_mode": "full"})


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
            result = _to_check_result(d, outcome)
        except Exception as exc:   # noqa: BLE001 - fail closed
            log.exception("doctor check %s crashed", d.id)
            outcome = Outcome("fail", f"The check itself crashed: {type(exc).__name__}: {redact(str(exc))[:200]}", "This is a bug; export diagnostics.")
            result = not_run(d.id, d.kind, f"{type(exc).__name__}", fm_ids=d.fm_ids)
        status = outcome.status
        # a SOFT check can only warn; HARD/ASSERT failures block
        if status == "fail" and d.kind == "soft":
            status = "warn"
        blocking = status == "fail" and d.kind in ("hard", "assert")
        items.append({"id": d.id, "title": d.title, "kind": d.kind, "status": status, "message": outcome.message, "fix": outcome.fix,
                      "fm_ids": d.fm_ids, "blocking": blocking, "fatal": bool(d.fatal and status == "fail"),
                      "detail": outcome.detail, "duration_ms": int((time.monotonic() - started) * 1000),
                      "check": result.model_dump(mode="json")})
    summary = {"passed": sum(1 for i in items if i["status"] == "pass"), "warnings": sum(1 for i in items if i["status"] == "warn"),
               "failed": sum(1 for i in items if i["status"] == "fail"), "not_applicable": sum(1 for i in items if i["status"] == "na")}
    blocks = any(i["blocking"] for i in items)
    broken = any(i["fatal"] for i in items)
    exit_code = 3 if broken else (2 if (blocks or summary["warnings"] or summary["failed"]) else 0)
    flags = kit_flags(paths)
    flags["clone_check_mode"] = next((i["detail"].get("clone_check_mode") for i in items if i["id"] == "CHK-S14" and i["detail"].get("clone_check_mode")), "degraded")
    flags["degraded_clone_check"] = flags["clone_check_mode"] == "degraded"
    flags["blender"] = str(detect_blender(settings)) if not quick else None
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

