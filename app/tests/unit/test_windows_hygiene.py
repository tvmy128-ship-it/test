"""Windows hygiene (APP_SPEC 4.3, FAILURE_MODES SYS-05/06/09/10/12/13/14/15/16/19): the app is developed on Linux and
runs on Windows 10/11, so this test reads every ``.py`` file under ``duoskin/`` and ``tools/`` with ``ast`` and fails on
code that behaves differently, or crashes, on Windows. Nothing here needs a Windows machine.

Rules (ids appear in the failure messages):

  W01  text-mode ``open`` / ``Path.open`` / ``read_text`` / ``write_text`` / ``fdopen`` / temp files without ``encoding=``
       (the default is cp1252 on Windows); ``subprocess`` with ``text=True`` and no ``encoding=``.
  W02  ``shell=True``, ``os.system``, ``os.popen``, ``shlex`` (POSIX quoting), or a command given as one string.
  W03  POSIX-only modules (fcntl, resource, pwd, grp, termios, ...) or Windows-only modules (msvcrt, winreg, ...)
       imported at module level without a platform guard.
  W04  POSIX-only calls or constants (``os.fork``, ``os.getuid``, ``signal.SIGKILL``, ``AF_UNIX``, ``os.X_OK`` ...)
       outside a platform guard.
  W05  Windows-only names (``ctypes.windll``, ``os.startfile``, ``subprocess.STARTUPINFO``, ``signal.CTRL_C_EVENT`` ...)
       at module level or in a function that has no platform guard.
  W06  hard-coded POSIX paths (``/tmp``, ``/var/...``, ``/dev/null``, ``~/``) and ``HOME``/``USER``/``TMPDIR`` variables.
  W07  ``os.rename`` / ``Path.rename`` (fail over an existing file on Windows; use ``os.replace`` or
       ``winplat.replace_with_retry``), ``os.kill`` (``os.kill(pid, 0)`` TERMINATES the process on Windows; use psutil),
       ``os.symlink`` / ``os.link`` (need privileges on Windows).
  W08  ``TemporaryDirectory`` without ``ignore_cleanup_errors=True``, ``NamedTemporaryFile`` that cannot be reopened,
       ``shutil.rmtree`` that does not tolerate read-only or locked files.
  W09  ``multiprocessing`` / ``ProcessPoolExecutor`` (spawn on Windows re-imports ``__main__``), and top-level calls that
       start something outside ``if __name__ == "__main__"``.
  W10  file paths built with ``+ "/"`` or ``f"{a}/{b}"`` instead of ``pathlib``.
  W11  ``strftime`` codes that only exist on POSIX (``%-d``, ``%e``, ``%k`` ...).
  W12  asyncio APIs the Windows event loop does not support (``add_signal_handler``, ``add_reader``, ``add_writer``).
  W13  ``sqlite3.connect(..., uri=True)`` (a Windows path is not a valid SQLite URI without conversion).
  W14  a bare ``os.replace`` outside ``winplat.py`` (antivirus and OneDrive hold new files; use ``winplat.replace_with_retry``).
  W15  ``cv2.imread`` / ``imwrite`` / ``VideoCapture`` ... outside ``imaging/files.py`` (OpenCV fails on non-ASCII Windows paths, SYS-05).

CI also runs ``ruff check --preview --select PLW1514 duoskin tools`` (APP_SPEC 4.3): ``test_ruff_encoding_rule_is_clean`` does it here.
A line can opt out with a trailing ``# win-ok: <reason>`` comment (for example a zip member name, which must use "/").

and, for the files in the repository: no reserved Windows names (CON, NUL, AUX ...), no names that differ only by case,
no characters Windows forbids, no trailing dot or space, and a path length that fits ``C:\\DuoSkin\\app\\`` + path < 240.
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from duoskin import config, winplat

ROOT = config.APP_ROOT
SCAN_DIRS = ("duoskin", "tools")
INSTALL_PREFIX = "C:\\DuoSkin\\app\\"
MAX_PATH_BUDGET = 240

POSIX_ONLY_MODULES = {"fcntl", "resource", "pwd", "grp", "termios", "tty", "pty", "syslog", "posix", "crypt", "spwd", "nis",
                      "ossaudiodev", "curses", "readline"}
WINDOWS_ONLY_MODULES = {"msvcrt", "winreg", "_winapi", "winsound", "nt", "_winreg", "win32api", "win32con", "win32file",
                        "win32gui", "win32process", "pythoncom", "pywintypes"}
POSIX_ONLY_ATTRS = {
    "os": {"fork", "forkpty", "getuid", "geteuid", "getgid", "getegid", "getpgid", "getpgrp", "setsid", "setpgrp", "setpgid", "killpg",
           "uname", "chown", "lchown", "fchown", "mkfifo", "mknod", "nice", "getloadavg", "sync", "wait", "wait3", "wait4",
           "sched_getaffinity", "sched_setaffinity", "sysconf", "statvfs", "getpriority", "setpriority", "chroot", "lockf",
           "ttyname", "openpty", "X_OK", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC", "getgroups", "setuid", "setgid", "umask_"},
    "signal": {"SIGKILL", "SIGALRM", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGPIPE", "SIGCHLD", "SIGQUIT", "SIGCONT", "SIGSTOP", "SIGTSTP",
               "SIGTTIN", "SIGTTOU", "SIGWINCH", "SIGPROF", "SIGVTALRM", "SIGXCPU", "SIGXFSZ", "SIGSYS", "SIGBUS", "alarm", "pause",
               "setitimer", "getitimer", "siginterrupt", "pthread_kill", "pthread_sigmask", "sigwait", "sigtimedwait",
               "sigpending", "sigwaitinfo"},
    "socket": {"AF_UNIX", "SO_REUSEPORT", "SOCK_NONBLOCK", "SOCK_CLOEXEC", "fromfd", "socketpair_"},
    "subprocess": set(),
}
WINDOWS_ONLY_ATTRS = {
    "ctypes": {"windll", "oledll", "WinDLL", "OleDLL", "WINFUNCTYPE", "WinError", "FormatError", "GetLastError", "get_last_error",
               "set_last_error", "HRESULT", "wintypes"},
    "os": {"startfile", "add_dll_directory"},
    "subprocess": {"STARTUPINFO", "STARTF_USESHOWWINDOW", "STARTF_USESTDHANDLES", "SW_HIDE", "CREATE_NO_WINDOW", "CREATE_NEW_CONSOLE",
                   "CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_DEFAULT_ERROR_MODE", "ABOVE_NORMAL_PRIORITY_CLASS",
                   "BELOW_NORMAL_PRIORITY_CLASS", "HIGH_PRIORITY_CLASS", "IDLE_PRIORITY_CLASS", "NORMAL_PRIORITY_CLASS",
                   "REALTIME_PRIORITY_CLASS"},
    "signal": {"CTRL_C_EVENT", "CTRL_BREAK_EVENT", "SIGBREAK"},
    "socket": {"SO_EXCLUSIVEADDRUSE", "SIO_RCVALL", "SIO_KEEPALIVE_VALS", "ioctl_"},
    "sys": {"getwindowsversion", "winver", "dllhandle"},
    "asyncio": {"WindowsSelectorEventLoopPolicy", "WindowsProactorEventLoopPolicy", "ProactorEventLoop"},
    "mimetypes": {"read_windows_registry_"},
}
POSIX_PATH_RE = re.compile(r"^(/(tmp|var|usr|etc|home|dev|proc|sys|opt|root|run|mnt|srv|bin|sbin|Users|Library|snap)(/|$)|~[/\\])")
POSIX_ENV = {"HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"}
STRFTIME_BAD_RE = re.compile(r"%[-_^#0eEkKlPsZ]|%-")
GUARD_RE = re.compile(r"sys\.platform|os\.name|IS_WINDOWS|IS_POSIX|platform\.system|hasattr\(|win32|nt\b|_IS_WIN|is_windows|"
                      r"TYPE_CHECKING", re.IGNORECASE)
SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call", "getoutput", "getstatusoutput"}
TEXT_FILE_FUNCS = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
START_CALL_NAMES = {"main", "run", "serve", "cli"}
RESERVED = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)}, *{f"lpt{i}" for i in range(1, 10)}}
ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
PATHY_RE = re.compile(r"path|dir|folder|root|file|home|cwd|base|parent|stem|name", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.rule} {self.message}"


# ------------------------------------------------------------------------------------------------------------ helpers
def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for Name/Attribute chains, else ``''``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


SCRIPT_ONLY_DIRS = ("duoskin/mesh/blender_scripts/",)      # run by Blender (``blender --python``), never imported or spawned


class _FileScan:
    def __init__(self, relpath: str, source: str) -> None:
        self.rel = relpath
        self.lines = source.splitlines()
        self.tree = ast.parse(source, filename=relpath)
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.violations: list[Violation] = []

    def add(self, rule: str, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        if 0 < line <= len(self.lines) and re.search(r"#\s*win-ok:\s*\S", self.lines[line - 1]):
            return
        self.violations.append(Violation(rule, self.rel, line, message))

    # ---- scopes and guards
    def ancestors(self, node: ast.AST):
        cur = self.parents.get(node)
        while cur is not None:
            yield cur
            cur = self.parents.get(cur)

    def enclosing_function(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        for a in self.ancestors(node):
            if isinstance(a, ast.FunctionDef | ast.AsyncFunctionDef):
                return a
        return None

    def at_module_level(self, node: ast.AST) -> bool:
        return not any(isinstance(a, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda) for a in self.ancestors(node))

    def _in_branch_guard(self, node: ast.AST) -> bool:
        child = node
        for a in self.ancestors(node):
            if isinstance(a, ast.If) and child is not a.test and GUARD_RE.search(ast.unparse(a.test)):
                return True
            if isinstance(a, ast.IfExp) and child is not a.test and GUARD_RE.search(ast.unparse(a.test)):
                return True
            if isinstance(a, ast.Try) and child in a.body and any(
                    h.type is None or re.search(r"ImportError|AttributeError|ModuleNotFoundError|\bException\b|BaseException",
                                                ast.unparse(h.type)) for h in a.handlers):
                return True
            if isinstance(a, ast.BoolOp) and GUARD_RE.search(ast.unparse(a)):
                return True
            child = a
        return False

    def _early_guard(self, node: ast.AST) -> bool:
        """The enclosing function starts with ``if <platform test>: return/raise`` before the statement holding ``node``."""
        fn = self.enclosing_function(node)
        if fn is None:
            return False
        holder = node
        while self.parents.get(holder) is not fn and holder in self.parents:
            holder = self.parents[holder]
        for stmt in fn.body:
            if stmt is holder:
                break
            if isinstance(stmt, ast.If) and GUARD_RE.search(ast.unparse(stmt.test)) and stmt.body and isinstance(
                    stmt.body[-1], ast.Return | ast.Raise):
                return True
        return False

    def guarded(self, node: ast.AST) -> bool:
        return self._in_branch_guard(node) or self._early_guard(node)

    # ---- the rules
    def run(self) -> list[Violation]:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                self.check_import(node)
            elif isinstance(node, ast.Call):
                self.check_call(node)
            elif isinstance(node, ast.Attribute):
                self.check_attribute(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.check_string(node)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                self.check_concat(node)
            elif isinstance(node, ast.JoinedStr):
                self.check_fstring_path(node)
            elif isinstance(node, ast.Subscript):
                self.check_environ_subscript(node)
        self.check_top_level_calls()
        return sorted(self.violations, key=lambda v: (v.line, v.rule))

    def check_import(self, node: ast.Import | ast.ImportFrom) -> None:
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        for name in names:
            top = name.split(".")[0]
            if top == "multiprocessing":
                self.add("W09", node, "multiprocessing: spawn on Windows re-imports __main__; use subprocess workers (mesh/proc.py)")
            if self.at_module_level(node) and not self.guarded(node):
                if top in POSIX_ONLY_MODULES:
                    self.add("W03", node, f"'{top}' is POSIX-only; import it inside a function behind a platform guard")
                if top in WINDOWS_ONLY_MODULES:
                    self.add("W03", node, f"'{top}' is Windows-only; import it inside a function behind a platform guard")
            elif not self.at_module_level(node) and top in POSIX_ONLY_MODULES | WINDOWS_ONLY_MODULES and not self.guarded(node):
                self.add("W03", node, f"'{top}' is platform-specific; guard the import (sys.platform / IS_WINDOWS)")
            if top == "shlex":
                self.add("W02", node, "shlex quotes for POSIX shells; pass an argument list to subprocess instead")

    def check_attribute(self, node: ast.Attribute) -> None:
        base = _dotted(node.value)
        if base in POSIX_ONLY_ATTRS and node.attr in POSIX_ONLY_ATTRS[base] and not self.guarded(node):
            self.add("W04", node, f"{base}.{node.attr} does not exist on Windows (guard it with sys.platform / hasattr)")
        if base in WINDOWS_ONLY_ATTRS and node.attr in WINDOWS_ONLY_ATTRS[base]:
            if self.at_module_level(node) and not self.guarded(node):
                self.add("W05", node, f"{base}.{node.attr} exists only on Windows: use it inside a guarded function")
            elif not self.at_module_level(node) and not self.guarded(node):
                self.add("W05", node, f"{base}.{node.attr} exists only on Windows: add 'if not IS_WINDOWS: return' first or use getattr")
        if base == "ctypes" and node.attr == "wintypes" and not self.guarded(node):
            self.add("W05", node, "ctypes.wintypes import is Windows-flavoured; guard it")

    def check_string(self, node: ast.Constant) -> None:
        value = node.value
        parent = self.parents.get(node)
        if isinstance(parent, ast.Expr):
            return                                               # a docstring or a bare comment string
        # URL paths such as "/api/health" are fine: only well-known POSIX directories match.
        is_route = isinstance(parent, ast.Call) and _dotted(parent.func).endswith(("get", "post", "route", "mount", "add_api_route"))
        if len(value) < 400 and POSIX_PATH_RE.match(value) and not self.guarded(node) and not is_route:
            self.add("W06", node, f"hard-coded POSIX path {value[:40]!r}: use pathlib, tempfile and platformdirs")
        if STRFTIME_BAD_RE.search(value) and isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) \
                and parent.func.attr in ("strftime", "strptime"):
            self.add("W11", node, f"strftime format {value!r} has a POSIX-only code")

    def check_environ_subscript(self, node: ast.Subscript) -> None:
        if _dotted(node.value) in ("os.environ", "environ") and isinstance(node.slice, ast.Constant) and node.slice.value in POSIX_ENV:
            self.add("W06", node, f"os.environ[{node.slice.value!r}] is not set on Windows (use pathlib.Path.home / tempfile / platformdirs)")

    def check_concat(self, node: ast.BinOp) -> None:
        for side, other in ((node.left, node.right), (node.right, node.left)):
            text = _const_str(side)
            if text is not None and re.fullmatch(r"[\\/][\w.\-]*|[\w.\-]*[\\/]", text) and text not in ("/", "") or text == "/":
                other_src = ast.unparse(other)
                if PATHY_RE.search(other_src) and not re.search(r"url|uri|href|http|endpoint|route", other_src, re.IGNORECASE):
                    self.add("W10", node, f"path built by string concatenation ({ast.unparse(node)[:60]}); use pathlib")

    def check_fstring_path(self, node: ast.JoinedStr) -> None:
        values = node.values
        for i in range(1, len(values) - 1):
            mid = values[i]
            if isinstance(mid, ast.Constant) and mid.value == "/" and isinstance(values[i - 1], ast.FormattedValue) \
                    and isinstance(values[i + 1], ast.FormattedValue):
                left = ast.unparse(values[i - 1].value)
                if PATHY_RE.search(left) and re.search(r"path|dir|folder|root|home|cwd", left, re.IGNORECASE) \
                        and not re.search(r"url|uri|http|host|endpoint", ast.unparse(node), re.IGNORECASE):
                    self.add("W10", node, f"path built in an f-string ({ast.unparse(node)[:60]}); use pathlib")

    def check_call(self, node: ast.Call) -> None:
        func = _dotted(node.func)
        # the called attribute or name, also when the receiver is a call: Path("a").read_text()
        short = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
        # W01 encoding
        mode_node: ast.AST | None = None
        is_open = False
        if func in ("open", "io.open", "codecs.open"):
            is_open = True
            mode_node = node.args[1] if len(node.args) > 1 else (_kw(node, "mode").value if _kw(node, "mode") else None)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "open" and func.split(".")[0] not in (
                "webbrowser", "os", "Image", "zipfile", "tarfile", "zf", "z", "urllib", "request", "Draco", "tempfile", "trimesh",
                "gzip", "bz2", "lzma", "wave", "shelve", "dbm", "ZipFile", "PIL"):
            first = _const_str(node.args[0]) if node.args else None
            if not node.args or (first is not None and re.fullmatch(r"[rwxabt+]{1,4}", first)):
                is_open = True
                mode_node = node.args[0] if node.args else (_kw(node, "mode").value if _kw(node, "mode") else None)
        elif func in ("os.fdopen",):
            is_open = True
            mode_node = node.args[1] if len(node.args) > 1 else (_kw(node, "mode").value if _kw(node, "mode") else None)
        elif short in TEXT_FILE_FUNCS:
            is_open = True
            mode_node = node.args[0] if node.args else (_kw(node, "mode").value if _kw(node, "mode") else ast.Constant("w+b"))
        if is_open:
            mode = _const_str(mode_node) if mode_node is not None else "r"
            binary = mode is not None and "b" in mode
            if not binary and _kw(node, "encoding") is None:
                self.add("W01", node, f"{func or short}() in text mode without encoding= (cp1252 on Windows)")
        if short in ("read_text", "write_text") and isinstance(node.func, ast.Attribute) and _kw(node, "encoding") is None \
                and len(node.args) < (2 if short == "read_text" else 3):
            self.add("W01", node, f"{short}() without encoding= (cp1252 on Windows)")
        if (func.startswith("subprocess.") or func in SUBPROCESS_FUNCS) and short in SUBPROCESS_FUNCS:
            text = _kw(node, "text") or _kw(node, "universal_newlines")
            if text is not None and not (isinstance(text.value, ast.Constant) and text.value.value in (False, None)) \
                    and _kw(node, "encoding") is None:
                self.add("W01", node, f"subprocess.{short}(text=True) without encoding= (cp1252 on Windows)")
            if _is_true((_kw(node, "shell") or ast.keyword()).value if _kw(node, "shell") else None):
                self.add("W02", node, "shell=True: cmd.exe quoting differs from sh; pass an argument list")
            if node.args and _const_str(node.args[0]) is not None and short != "getoutput":
                self.add("W02", node, "subprocess command given as one string: pass a list (Windows parses strings differently)")
            if _kw(node, "preexec_fn") is not None and not self.guarded(node):
                self.add("W04", node, "preexec_fn raises ValueError on Windows")
            if _kw(node, "start_new_session") is not None and not self.guarded(node):
                self.add("W04", node, "start_new_session is POSIX-only: guard it (use CREATE_NEW_PROCESS_GROUP on Windows)")
            if _kw(node, "restore_signals") is not None and not self.guarded(node):
                self.add("W04", node, "restore_signals is POSIX-only")
        if func in ("os.system", "os.popen"):
            self.add("W02", node, f"{func}: use subprocess with an argument list")
        # W06 environment
        if func in ("os.getenv", "os.environ.get", "environ.get", "getenv") and node.args and _const_str(node.args[0]) in POSIX_ENV:
            self.add("W06", node, f"{func}({_const_str(node.args[0])!r}) is unset on Windows (use pathlib.Path.home / tempfile / platformdirs)")
        # W07
        if func == "os.rename" or (isinstance(node.func, ast.Attribute) and node.func.attr == "rename" and len(node.args) == 1
                                   and func.split(".")[0] not in ("self", "session", "conn")):
            self.add("W07", node, "rename() fails over an existing file on Windows: use os.replace / winplat.replace_with_retry")
        if func == "os.kill":
            self.add("W07", node, "os.kill(pid, 0) TERMINATES the process on Windows: use psutil (pid_exists, Process.kill)")
        if func in ("os.symlink", "os.link", "os.readlink") or (isinstance(node.func, ast.Attribute) and node.func.attr in (
                "symlink_to", "hardlink_to") and len(node.args) >= 1):
            self.add("W07", node, "symlinks need Developer Mode or admin rights on Windows: copy instead")
        if func in ("cv2.imread", "cv2.imwrite", "cv2.imreadmulti", "cv2.imwritemulti", "cv2.VideoCapture", "cv2.VideoWriter",
                    "cv2.FileStorage") and not self.rel.endswith("duoskin/imaging/files.py"):
            self.add("W15", node, f"{func} fails on non-ASCII Windows paths: use imaging/files.py (Pillow, or np.fromfile + cv2.imdecode)")
        if func == "os.replace" and not self.rel.endswith("duoskin/winplat.py"):
            self.add("W14", node, "bare os.replace: use winplat.replace_with_retry (retries WinError 5/32 for 2 s)")
        # W08
        if short == "TemporaryDirectory" and not _is_true(_kw(node, "ignore_cleanup_errors").value if _kw(node, "ignore_cleanup_errors") else None):
            self.add("W08", node, "TemporaryDirectory(...) needs ignore_cleanup_errors=True (antivirus holds files for a moment)")
        if short == "NamedTemporaryFile":
            delete = _kw(node, "delete")
            dod = _kw(node, "delete_on_close")
            ok = (delete is not None and isinstance(delete.value, ast.Constant) and delete.value.value is False) or (
                dod is not None and isinstance(dod.value, ast.Constant) and dod.value.value is False)
            if not ok:
                self.add("W08", node, "NamedTemporaryFile cannot be reopened by name on Windows while open: use delete=False (or delete_on_close=False)")
        if func == "shutil.rmtree" and not any(_kw(node, k) is not None for k in ("ignore_errors", "onerror", "onexc")):
            self.add("W08", node, "shutil.rmtree without ignore_errors/onexc: read-only and locked files raise PermissionError on Windows")
        # W09
        if func.startswith("multiprocessing.") or short in ("ProcessPoolExecutor", "Pool", "freeze_support") and "pool" in func.lower() \
                or short == "ProcessPoolExecutor":
            self.add("W09", node, f"{func}: use subprocess workers, not multiprocessing (spawn semantics on Windows)")
        # W11 / W12 / W13
        if short in ("add_signal_handler", "add_reader", "add_writer", "remove_reader", "remove_writer") and isinstance(node.func, ast.Attribute):
            self.add("W12", node, f"loop.{short}() raises NotImplementedError on the Windows event loop")
        if func == "sqlite3.connect" and _is_true(_kw(node, "uri").value if _kw(node, "uri") else None):
            self.add("W13", node, "sqlite3.connect(uri=True): convert the Windows path with Path.as_uri() or avoid URIs")

    def check_top_level_calls(self) -> None:
        if self.rel.startswith(SCRIPT_ONLY_DIRS):
            return
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                name = _dotted(stmt.value.func)
                if name in START_CALL_NAMES or name in ("sys.exit", "uvicorn.run", "asyncio.run", "app.run", "multiprocessing.freeze_support"):
                    self.add("W09", stmt, f"top-level call {name}() runs on import: put it under if __name__ == '__main__'")


def scan_source(relpath: str, source: str) -> list[Violation]:
    return _FileScan(relpath, source).run()


def python_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        out += [p for p in sorted((ROOT / d).rglob("*.py")) if "__pycache__" not in p.parts]
    return out


def scan_repository() -> list[Violation]:
    found: list[Violation] = []
    for path in python_files():
        rel = path.relative_to(ROOT).as_posix()
        found += scan_source(rel, path.read_text(encoding="utf-8"))
    return found


_ALL = None


def all_violations() -> list[Violation]:
    global _ALL
    if _ALL is None:
        _ALL = scan_repository()
    return _ALL


RULES = ["W01", "W02", "W03", "W04", "W05", "W06", "W07", "W08", "W09", "W10", "W11", "W12", "W13", "W14", "W15"]


@pytest.mark.parametrize("rule", RULES)
def test_repository_has_no_windows_hostile_code(rule):
    bad = [str(v) for v in all_violations() if v.rule == rule]
    assert not bad, f"{rule}: Windows-hostile code (see the module docstring):\n  " + "\n  ".join(bad)


def test_ruff_encoding_rule_is_clean():
    proc = subprocess.run([sys.executable, "-m", "ruff", "check", "--preview", "--select", "PLW1514", "--no-cache", "--output-format",
                           "concise", "duoskin", "tools"], cwd=str(ROOT), capture_output=True, encoding="utf-8", errors="replace",
                          check=False, stdin=subprocess.DEVNULL, timeout=120)
    if "No module named ruff" in proc.stderr:
        pytest.skip("ruff is not installed")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_python_file_parses_on_the_oldest_supported_python():
    # Syntax newer than 3.11 (PEP 695 generics, f-string nesting, ...) would break a PC running 3.12/3.13 only partly; the
    # AST must at least parse with feature_version=(3, 11) so the code stays portable.
    for path in python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))
        except SyntaxError as exc:                               # pragma: no cover - failure path
            pytest.fail(f"{path.relative_to(ROOT)}: not valid on Python 3.11: {exc}")


# ------------------------------------------------------------------------------------------------- the scanner itself
@pytest.mark.parametrize("rule,snippet", [
    ("W01", "open('a.txt')"),
    ("W01", "open('a.txt', 'w')"),
    ("W01", "from pathlib import Path\nPath('a').read_text()"),
    ("W01", "from pathlib import Path\nPath('a').write_text('x')"),
    ("W01", "import subprocess\nsubprocess.run(['a'], text=True)"),
    ("W01", "import tempfile\ntempfile.NamedTemporaryFile('w', delete=False)"),
    ("W02", "import subprocess\nsubprocess.run('dir', shell=True)"),
    ("W02", "import subprocess\nsubprocess.run('python -V')"),
    ("W02", "import os\nos.system('dir')"),
    ("W02", "import shlex"),
    ("W03", "import fcntl"),
    ("W03", "import msvcrt"),
    ("W03", "from resource import setrlimit"),
    ("W04", "import os\ndef f():\n    return os.getuid()"),
    ("W04", "import signal\nsignal.SIGKILL"),
    ("W04", "import subprocess\nsubprocess.Popen(['a'], start_new_session=True)"),
    ("W05", "import ctypes\nx = ctypes.windll.kernel32"),
    ("W05", "import os\ndef f(p):\n    os.startfile(p)"),
    ("W06", "p = '/tmp/x'"),
    ("W06", "import os\nos.environ['HOME']"),
    ("W06", "import os\nos.getenv('TMPDIR')"),
    ("W07", "import os\nos.rename('a', 'b')"),
    ("W07", "from pathlib import Path\nPath('a').rename('b')"),
    ("W07", "import os\nos.kill(1, 0)"),
    ("W07", "import os\nos.symlink('a', 'b')"),
    ("W08", "import tempfile\ntempfile.TemporaryDirectory()"),
    ("W08", "import tempfile\ntempfile.NamedTemporaryFile('w', encoding='utf-8')"),
    ("W08", "import shutil\nshutil.rmtree('a')"),
    ("W09", "import multiprocessing"),
    ("W09", "from concurrent.futures import ProcessPoolExecutor\nProcessPoolExecutor()"),
    ("W09", "def main(): pass\nmain()"),
    ("W10", "def f(base_dir):\n    return base_dir + '/cache'"),
    ("W10", "def f(root, name):\n    return f'{root}/{name}'"),
    ("W11", "import datetime\ndatetime.datetime.now().strftime('%-d %B')"),
    ("W12", "def f(loop, cb):\n    loop.add_reader(1, cb)"),
    ("W13", "import sqlite3\nsqlite3.connect('file:x', uri=True)"),
    ("W14", "import os\nos.replace('a', 'b')"),
    ("W15", "import cv2\ncv2.imread('a.png')"),
])
def test_scanner_catches(rule, snippet):
    assert any(v.rule == rule for v in scan_source("snippet.py", snippet)), f"{rule} did not fire for:\n{snippet}"


@pytest.mark.parametrize("snippet", [
    "open('a.bin', 'rb')",
    "open('a.txt', 'w', encoding='utf-8')",
    "from pathlib import Path\nPath('a').read_text(encoding='utf-8')",
    "from pathlib import Path\nPath('a').write_bytes(b'x')",
    "import subprocess\nsubprocess.run(['a'], capture_output=True)",
    "import subprocess\nsubprocess.run(['a'], text=True, encoding='utf-8')",
    "import sys\nif sys.platform == 'win32':\n    import msvcrt",
    "import sys\ndef f():\n    if sys.platform == 'win32':\n        import msvcrt\n    else:\n        import fcntl",
    "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None",
    "import os, sys\nif sys.platform != 'win32':\n    x = os.getuid()",
    "import ctypes\nIS_WINDOWS = True\ndef f():\n    if not IS_WINDOWS:\n        return\n    ctypes.windll.kernel32",
    "import subprocess\nflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)",
    "import tempfile\ntempfile.TemporaryDirectory(ignore_cleanup_errors=True)",
    "import tempfile\ntempfile.NamedTemporaryFile(delete=False)",
    "import shutil\nshutil.rmtree('a', ignore_errors=True)",
    "import winplat\nwinplat.replace_with_retry('a', 'b')",
    "def f(root):\n    return 'http://127.0.0.1:1' + '/api'",
    "url = f'http://127.0.0.1:{port}/api'",
    "def main(): pass\nif __name__ == '__main__':\n    main()",
    "x = '/api/health'",
    "from pathlib import Path\nPath('a') / 'b'",
])
def test_scanner_accepts(snippet):
    assert scan_source("snippet.py", snippet) == [], scan_source("snippet.py", snippet)


# ------------------------------------------------------------------------------------------------------ the repository
def _repo_files() -> list[Path]:
    skip = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "data", ".git", "node_modules"}
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if any(part in skip for part in rel.parts) or not p.is_file():
            continue
        out.append(p)
    return out


def test_no_reserved_names_illegal_characters_or_case_collisions_in_the_repository():
    problems: list[str] = []
    seen: dict[str, str] = {}
    for p in _repo_files():
        rel = p.relative_to(ROOT)
        for part in rel.parts:
            stem = part.split(".")[0].strip().lower()
            if stem in RESERVED:
                problems.append(f"{rel}: '{part}' is a reserved Windows device name")
            if ILLEGAL_CHARS_RE.search(part):
                problems.append(f"{rel}: '{part}' has a character Windows forbids")
            if part.endswith((".", " ")):
                problems.append(f"{rel}: '{part}' ends with a dot or a space")
        key = rel.as_posix().lower()
        if key in seen and seen[key] != rel.as_posix():
            problems.append(f"{rel} and {seen[key]} differ only by case (one overwrites the other on Windows)")
        seen[key] = rel.as_posix()
    assert not problems, "\n".join(problems)


def test_a_directory_and_a_file_never_differ_only_by_case():
    dirs = {p.relative_to(ROOT).as_posix().lower() for p in ROOT.rglob("*") if p.is_dir() and ".venv" not in p.parts}
    files = {p.relative_to(ROOT).as_posix().lower() for p in _repo_files()}
    assert not dirs & files


def test_repository_paths_fit_in_max_path_when_installed_at_the_default_folder():
    too_long = [f"{len(INSTALL_PREFIX) + len(str(p.relative_to(ROOT)))}: {p.relative_to(ROOT)}"
                for p in _repo_files() if len(INSTALL_PREFIX) + len(str(p.relative_to(ROOT))) >= MAX_PATH_BUDGET]
    assert not too_long, "paths too long for C:\\DuoSkin\\app\\ (MAX_PATH 260, budget 240):\n" + "\n".join(too_long)


def test_the_repository_paths_pass_the_apps_own_path_linter():
    bad = []
    for p in _repo_files():
        problems = winplat.lint_path(INSTALL_PREFIX + str(p.relative_to(ROOT)))
        bad += [f"{p.relative_to(ROOT)}: {x}" for x in problems]
    assert not bad, "\n".join(bad)


def test_python_modules_do_not_shadow_the_standard_library():
    # SYS-19 / CHK-S13: a stray inspect.py / secrets.py breaks numpy and anthropic imports when its folder is on sys.path.
    # Folders that can be: the app root (cwd of ``-m duoskin``), tools, tests, the package folder (script runs) and the
    # Blender scripts folder. A deeper module such as duoskin/engine/gc.py is namespaced and safe with ``-m``.
    std = set(sys.stdlib_module_names)
    folders = [ROOT, ROOT / "tools", ROOT / "tests", ROOT / "duoskin", ROOT / "duoskin" / "mesh" / "blender_scripts"]
    clash = [str(p.relative_to(ROOT)) for f in folders if f.exists() for p in f.glob("*.py") if p.stem in std and p.stem != "__init__"]
    clash += [str(p.relative_to(ROOT)) for p in (ROOT / "duoskin").iterdir() if p.is_dir() and p.name in std]
    assert not clash, f"modules named like stdlib modules: {clash}"


# ------------------------------------------------------------------------------------------------------ stdio encoding
def test_configure_stdio_makes_non_cp1252_output_safe():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    with pytest.raises(UnicodeEncodeError):
        stream.write("arrow \u2192")
    assert winplat.configure_stdio(streams=(stream,)) is True
    stream.write("arrow \u2192 ok")
    stream.flush()
    assert "ok" in raw.getvalue().decode("utf-8", errors="replace")


def test_configure_stdio_ignores_streams_that_cannot_be_reconfigured():
    class Plain:
        pass

    assert winplat.configure_stdio(streams=(Plain(), None)) is False
