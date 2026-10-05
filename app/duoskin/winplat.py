"""Windows integration and the small platform rules of APP_SPEC §4.3.

Everything here also runs on Linux/macOS (the app is developed there): Windows-only modules (``msvcrt``, ``ctypes.windll``)
are imported inside guarded functions only, and each function degrades to a documented no-op.

Public interface: ``bind_socket``, ``keep_awake``, ``console_ctrl_handler``, ``quickedit_off``, ``single_instance``,
``dpapi_protect`` / ``dpapi_unprotect``, ``fix_mimetypes``, ``fix_dll_directories``, ``replace_with_retry``,
``atomic_write``, ``lint_path``.
"""
from __future__ import annotations

import errno
import logging
import mimetypes
import os
import re
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Self

log = logging.getLogger("duoskin.winplat")

IS_WINDOWS = sys.platform == "win32"
LOOPBACK = "127.0.0.1"
FALLBACK_PORTS = (8765, *range(8766, 8800))

# WinError 10013 (WSAEACCES: port reserved by Hyper-V/WSL/Docker) and 10048 (WSAEADDRINUSE) mean "try the next port".
_BIND_RETRY_ERRNOS = {errno.EADDRINUSE, errno.EACCES, 10013, 10048}
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)}, *{f"lpt{i}" for i in range(1, 10)}}


# ---------------------------------------------------------------------------------------------------------------- sockets
class PortBindError(OSError):
    """No port in the sticky -> 8765 -> 8766..8799 sequence could be bound."""


def bind_socket(preferred: int = 8765, *, host: str = LOOPBACK, ports: Iterable[int] | None = None) -> socket.socket:
    """Bind (not listen) a TCP socket on ``host`` ourselves, so uvicorn never ``sys.exit``s on a bind failure.

    Order: ``preferred`` (the sticky port), 8765, 8766..8799. ``preferred == 0`` asks the OS for any free port (tests).
    On win32 the socket gets ``SO_EXCLUSIVEADDRUSE`` (no other process, even the same user, can share the port);
    elsewhere ``SO_REUSEADDR`` is set so a quick restart does not hit TIME_WAIT. The host must be loopback.
    """
    if host not in (LOOPBACK, "localhost"):
        raise ValueError("DuoSkin only binds 127.0.0.1")
    if preferred == 0:
        candidates = [0]
    else:
        candidates = [preferred, *[p for p in (ports if ports is not None else FALLBACK_PORTS) if p != preferred]]
    last_err: OSError | None = None
    for port in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if IS_WINDOWS:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # type: ignore[attr-defined]
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LOOPBACK, port))
            sock.set_inheritable(True)
            return sock
        except OSError as exc:
            sock.close()
            last_err = exc
            code = getattr(exc, "winerror", None) or exc.errno
            if code in _BIND_RETRY_ERRNOS:
                log.info("port %s unavailable (%s); trying the next one", port, code)
                continue
            raise
    raise PortBindError(f"could not bind any port in {candidates[:3]}...{candidates[-1:]}: {last_err}")


# ---------------------------------------------------------------------------------------------------------------- console
def quickedit_off() -> bool:
    """Turn console QuickEdit off (a click in the console freezes output). Returns True when changed (win32 only)."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32   # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)   # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = (mode.value & ~0x0040) | 0x0080   # clear ENABLE_QUICK_EDIT_MODE, set ENABLE_EXTENDED_FLAGS
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        log.exception("could not disable QuickEdit")
        return False


class CtrlHandler:
    """Handle returned by ``console_ctrl_handler``; ``remove()`` unregisters it."""

    def __init__(self, remove: Callable[[], None]) -> None:
        self._remove = remove

    def remove(self) -> None:
        try:
            self._remove()
        except Exception:
            log.exception("could not remove the console handler")


def console_ctrl_handler(cb: Callable[[], None]) -> CtrlHandler:
    """Call ``cb()`` on console close / logoff / shutdown / Ctrl+C / Ctrl+Break (win32); SIGINT/SIGTERM elsewhere.

    The callback must be fast (Windows ends the process about 5 s after a close event). It runs on a system thread.
    """
    if IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32   # type: ignore[attr-defined]
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)   # type: ignore[attr-defined]

        def _handler(event: int) -> int:
            # 0 CTRL_C, 1 CTRL_BREAK, 2 CLOSE, 5 LOGOFF, 6 SHUTDOWN
            if event in (0, 1, 2, 5, 6):
                try:
                    cb()
                except Exception:  # noqa: BLE001, S110
                    pass
                return 1
            return 0

        c_handler = handler_type(_handler)
        kernel32.SetConsoleCtrlHandler(c_handler, 1)
        keep_alive = [c_handler]   # the callback must stay referenced for the life of the registration

        def _remove() -> None:
            kernel32.SetConsoleCtrlHandler(keep_alive[0], 0)

        return CtrlHandler(_remove)

    import signal

    previous: dict[int, Any] = {}

    def _signal(_signum: int, _frame: Any) -> None:
        cb()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _signal)
        except ValueError:   # not the main thread
            pass

    def _restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except ValueError:
                pass

    return CtrlHandler(_restore)


_keep_awake_state = {"on": False}


def keep_awake(on: bool) -> bool:
    """``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`` for the *calling thread* (the flag is per thread).

    Call it from the long-lived scheduler thread. Returns True when the OS call was made (win32 only).
    """
    changed = _keep_awake_state["on"] != on
    _keep_awake_state["on"] = on
    if changed:
        log.info("keep-awake %s", "on" if on else "off")
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        flags = es_continuous | (es_system_required if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)   # type: ignore[attr-defined]
        return True
    except Exception:
        log.exception("SetThreadExecutionState failed")
        return False


# ---------------------------------------------------------------------------------------------------------------- instance
class InstanceLock:
    """An exclusive lock on ``run/instance.lock``; held until ``release()`` or process exit."""

    def __init__(self, path: Path, fh: Any) -> None:
        self.path = path
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)   # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def single_instance(path: Path | None = None) -> InstanceLock | None:
    """Take the single-instance lock. Returns None when another copy already holds it.

    win32: ``msvcrt.locking`` on the first byte; elsewhere ``fcntl.flock``. ``path`` defaults to ``DATA/run/instance.lock``.
    """
    if path is None:
        from duoskin import config

        path = config.paths().run_dir / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")  # noqa: SIM115
    try:
        if IS_WINDOWS:
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)   # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return InstanceLock(path, fh)


# ---------------------------------------------------------------------------------------------------------------- DPAPI
class DpapiUnavailable(OSError):
    pass


def _dpapi(data: bytes, protect: bool) -> bytes:
    if not IS_WINDOWS:
        raise DpapiUnavailable("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32   # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32   # type: ignore[attr-defined]
    in_buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CryptProtectData(in, descr, entropy, reserved, prompt, flags, out) and CryptUnprotectData share this shape
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error() or -1, "DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt for the current Windows user (``CryptProtectData``, current-user scope)."""
    return _dpapi(data, True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi(data, False)


# ---------------------------------------------------------------------------------------------------------------- process env
def fix_mimetypes() -> None:
    """SYS-13: Windows registry entries can map ``.js`` to ``text/plain`` and ES modules then refuse to load."""
    for ext, mime in (
        (".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"), (".svg", "image/svg+xml"),
        (".webp", "image/webp"), (".glb", "model/gltf-binary"), (".gltf", "model/gltf+json"),
        (".wasm", "application/wasm"), (".json", "application/json"), (".map", "application/json"),
        (".woff2", "font/woff2"), (".ico", "image/x-icon"),
    ):
        mimetypes.add_type(mime, ext)


def fix_dll_directories() -> list[str]:
    """SYS-09: onnxruntime needs msvcp140*.dll, which ``msvc-runtime`` puts in the venv root and ``Scripts``."""
    added: list[str] = []
    if not IS_WINDOWS:
        return added
    prefix = Path(sys.prefix)
    for folder in (prefix, prefix / "Scripts"):
        try:
            if (folder / "msvcp140.dll").exists():
                os.add_dll_directory(str(folder))   # type: ignore[attr-defined]
                added.append(str(folder))
        except OSError:
            log.exception("os.add_dll_directory(%s) failed", folder)
    return added


# ---------------------------------------------------------------------------------------------------------------- files
def replace_with_retry(src: str | os.PathLike[str], dst: str | os.PathLike[str], *, timeout_s: float = 2.0,
                       sleep: Callable[[float], None] = time.sleep) -> None:
    """``os.replace`` that retries ``PermissionError`` for up to ``timeout_s`` (antivirus / OneDrive locks, WinError 5/32)."""
    deadline = time.monotonic() + timeout_s
    delay = 0.02
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            sleep(delay)
            delay = min(delay * 2, 0.25)


def atomic_write(path: str | os.PathLike[str], data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to a temp file in the same folder, fsync, then ``os.replace`` (with retries)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if mode is not None and not IS_WINDOWS:
            os.chmod(tmp, mode)
        replace_with_retry(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def lint_path(path: str | os.PathLike[str], *, max_len: int = 240) -> list[str]:
    """SYS-15 path linter: length < 240, no reserved Windows names, no trailing dots or spaces. Returns the problems.

    Splits on both ``\\`` and ``/`` so a Windows path is judged the same way on a development machine."""
    p = str(path)
    problems: list[str] = []
    if len(p) >= max_len:
        problems.append(f"path is {len(p)} characters long (limit {max_len - 1})")
    for part in (x for x in re.split(r"[\\/]+", p) if x):
        if re.fullmatch(r"[A-Za-z]:", part):
            continue
        stem = part.split(".")[0].strip().lower()
        if stem in _RESERVED_NAMES:
            problems.append(f"'{part}' is a reserved Windows name")
        if part.endswith((".", " ")) and part not in (".", ".."):
            problems.append(f"'{part}' ends with a dot or space")
    return problems


def is_slug(value: str) -> bool:
    return bool(_SLUG_RE.match(value)) and value.split(".")[0] not in _RESERVED_NAMES


def is_onedrive_path(path: str | os.PathLike[str]) -> bool:
    return "onedrive" in str(path).lower()
