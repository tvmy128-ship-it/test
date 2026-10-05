"""Run a child process with a timeout, a memory ceiling and a psutil tree kill (APP_SPEC 5.1, 4.3 SYS-11).

Used for ``python -m duoskin.mesh.worker job.json`` and for headless Blender, so a native crash, a hang or a runaway
allocation can never take the server down. Output goes to temp files (never pipes: a full pipe would deadlock a chatty
child) and only the tail is returned.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

TAIL_CHARS = 6000


@dataclass
class ProcResult:
    returncode: int | None
    timed_out: bool
    killed_for_memory: bool
    stdout: str
    stderr: str
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.killed_for_memory


def kill_tree(pid: int, grace_s: float = 2.0) -> None:
    """Terminate ``pid`` and every descendant (psutil); escalate to kill after ``grace_s``."""
    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(procs, timeout=grace_s)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            continue
    psutil.wait_procs(alive, timeout=grace_s)


def _tree_rss_mb(pid: int) -> float:
    import psutil

    try:
        p = psutil.Process(pid)
        procs = [p] + p.children(recursive=True)
        return sum(q.memory_info().rss for q in procs if q.is_running()) / (1024 * 1024)
    except psutil.Error:
        return 0.0


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_CHARS * 4))
            return fh.read().decode("utf-8", errors="replace")[-TAIL_CHARS:]
    except OSError:
        return ""


def run_process(cmd: Sequence[str], *, timeout_s: float, cwd: str | Path | None = None, env: dict[str, str] | None = None,
                max_rss_mb: float | None = None, poll_s: float = 0.2) -> ProcResult:
    """Run ``cmd``; kill the whole process tree on timeout or when its memory exceeds ``max_rss_mb``."""
    full_env = dict(os.environ)
    full_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    if env:
        full_env.update(env)
    flags = 0
    kwargs: dict = {}
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="duoskin_proc_") as td:
        out_path, err_path = os.path.join(td, "out.txt"), os.path.join(td, "err.txt")
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            proc = subprocess.Popen(list(cmd), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, cwd=str(cwd) if cwd else None,  # noqa: S603
                                    env=full_env, **kwargs)
            timed_out = killed_mem = False
            while True:
                try:
                    proc.wait(timeout=poll_s)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() - start > timeout_s:
                    timed_out = True
                    kill_tree(proc.pid)
                    break
                if max_rss_mb is not None and _tree_rss_mb(proc.pid) > max_rss_mb:
                    killed_mem = True
                    kill_tree(proc.pid)
                    break
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill_tree already escalated
                kill_tree(proc.pid, grace_s=0.5)
        return ProcResult(proc.returncode, timed_out, killed_mem, _tail(out_path), _tail(err_path), time.monotonic() - start)
