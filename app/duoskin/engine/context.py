"""``StepContext``: everything a step handler may touch while it runs (APP_SPEC §8.2). See ``engine.registry`` for the
handler contract; this module is the exact list of what the context offers.

One context object is created per attempt of a step. It is only valid while the step runs (a stale context raises
``Cancelled`` from ``check_cancel`` once the step was cancelled or its lease was lost).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from duoskin.checks.model import CheckResult
from duoskin.engine.errors import Cancelled, StepFailure
from duoskin.models.asset import Asset, AssetLink, Provenance
from duoskin.models.common import new_id, utcnow
from duoskin.models.cost import CostEntry, CostUnit
from duoskin.models.gate import Gate
from duoskin.models.job import Step
from duoskin.models.project import Project

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.context")

LEASE_RECHECK_S = 2.0
TAIL_CHARS = 4000


@dataclass(frozen=True)
class LocalCallCtx:
    """Fallback for ``providers.base.CallCtx`` when the provider layer is not importable (same four fields)."""

    step_id: str | None
    heartbeat: Callable[[], None]
    check_cancel: Callable[[], None]
    progress: Callable[[float, str], None]


class StepContext:
    def __init__(self, rt: Runtime, step: Step, project: Project | None, params: Any, inputs: list[Asset],
                 cancel_event: threading.Event, hints: dict[str, Any] | None = None) -> None:
        self.rt = rt
        self.step = step
        self.project = project
        self.params = params
        self.inputs = inputs
        self.hints: dict[str, Any] = dict(hints or {})
        self._cancel = cancel_event
        self._last_lease_check = time.monotonic()
        self.put_shas: list[str] = []
        self.gate_opened = False
        self.gate_id: str | None = None
        self.cost_recorded = False

    # ------------------------------------------------------------------------------------------------ identity
    @property
    def attempt(self) -> int:
        return self.step.attempt

    @property
    def project_id(self) -> str | None:
        return self.step.project_id

    # ------------------------------------------------------------------------------------------------ cancel/progress
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set() or self.rt.scheduler.stopping

    def check_cancel(self) -> None:
        """Raise ``Cancelled`` when the step was cancelled, lost its lease, or the app is shutting down."""
        if self.cancelled:
            raise Cancelled(self.step.id)
        now = time.monotonic()
        if now - self._last_lease_check >= LEASE_RECHECK_S:
            self._last_lease_check = now
            row = self.rt.db.conn().execute(
                "SELECT state, json_extract(json,'$.attempt') AS attempt FROM steps WHERE id=?", (self.step.id,)).fetchone()
            if row is None or row["state"] != "running" or int(row["attempt"]) != self.step.attempt:
                self._cancel.set()
                raise Cancelled(self.step.id)

    def progress(self, frac: float, msg: str = "") -> None:
        frac = max(0.0, min(1.0, float(frac)))
        if self.rt.bus.emit_progress(self.step.id, {"progress": frac, "message": msg, "kind": self.step.kind,
                                                    "job_id": self.step.job_id}, self.step.project_id) is not None:
            with self.rt.db.tx() as c:
                c.execute("UPDATE steps SET json=json_set(json,'$.progress',?,'$.message',?) WHERE id=? AND state='running'",
                          (frac, msg[:300], self.step.id))

    def heartbeat(self) -> None:
        """Extend the lease now. Long blocking SDK calls never call ``progress()``; the heartbeat thread covers them."""
        self.rt.ops.extend_lease(self.step.id, self.step.attempt, self.rt.instance_id)

    # ------------------------------------------------------------------------------------------------ assets
    def put_asset(self, data: bytes, ext: str, *, role: str, prov: Provenance, part_id: str | None = None,
                  status: Literal["candidate", "rejected", "chosen", "final", "approved", "superseded", "export"] = "candidate",
                  rank: int | None = None) -> Asset:
        """Store ``data`` in the CAS and link it to this step. Provenance gets the step id/kind/version/nonce filled in."""
        fill: dict[str, Any] = {}
        if prov.step_id is None:
            fill["step_id"] = self.step.id
        if prov.step_kind is None:
            fill["step_kind"] = self.step.kind
        if prov.handler_version is None:
            fill["handler_version"] = self.step.handler_version
        if not prov.nonce and self.step.nonce:
            fill["nonce"] = self.step.nonce
        prov = prov.model_copy(update=fill) if fill else prov
        link = AssetLink(id=new_id("lnk"), asset_sha="0" * 64, project_id=self.step.project_id,
                         part_id=part_id or self.step.part_id, step_id=self.step.id, role=role, status=status, rank=rank,
                         provenance=prov)   # type: ignore[arg-type]
        asset = self.rt.cas.put(data, ext, link=link, prov=prov)
        if asset.sha256 not in self.put_shas:
            self.put_shas.append(asset.sha256)
        return asset

    def get_asset(self, sha: str) -> Asset:
        return self.rt.cas.get_asset(sha)

    def read_asset(self, sha: str) -> bytes:
        return self.rt.cas.get(sha)

    # ------------------------------------------------------------------------------------------------ remote
    def set_remote_ref(self, ref: str, state: Literal["submitted", "submission_uncertain", "polling", "slow", "done"] = "submitted") -> None:
        """Commit the remote task id *now*, in its own transaction, before returning ``Pending`` (never pay twice)."""
        def apply(s: Step) -> None:
            s.remote_ref = ref
            s.remote_state = state

        updated = self.rt.repo.mutate_step(
            self.step.id, apply, guard=lambda s: s.state.value == "running" and s.attempt == self.step.attempt)
        if updated is None:
            raise Cancelled(self.step.id)
        self.step = updated

    # ------------------------------------------------------------------------------------------------ costs and checks
    def add_cost(self, entry: CostEntry) -> CostEntry | None:
        """Record an actual cost for this step. The step, attempt and project are filled in. Idempotent per
        ``(step, attempt, operation)``: give every billed call of one attempt a distinct ``operation``.
        Returns the stored entry, or None when it was a duplicate."""
        update: dict[str, Any] = {"step_id": self.step.id, "attempt": self.step.attempt}
        if entry.project_id is None:
            update["project_id"] = self.step.project_id
        if entry.part_id is None:
            update["part_id"] = self.step.part_id
        stored = self.rt.budget.add_entry(entry.model_copy(update=update))
        self.cost_recorded = True
        return stored

    def cost(self, provider: str, model: str, operation: str, usd: float, *, units: list[CostUnit] | None = None,
             basis: Literal["estimate", "usage", "credits", "orphan"] = "usage", credits: float | None = None,
             request_id: str | None = None, remote_task_id: str | None = None, price_table: str = "",
             balance_before: float | None = None, balance_after: float | None = None, batch: bool = False) -> CostEntry | None:
        return self.add_cost(CostEntry(ts=utcnow(), provider=provider, model=model, operation=operation, usd=usd,   # type: ignore[arg-type]
                                       units=units or [], basis=basis, credits=credits, request_id=request_id,
                                       remote_task_id=remote_task_id, price_table=price_table,
                                       balance_before=balance_before, balance_after=balance_after, batch=batch))

    def record_checks(self, results: list[CheckResult]) -> list[str]:
        """Store check results with this step; returns their row ids (put them in ``Provenance.check_ids``)."""
        return self.rt.repo.insert_checks(results, project_id=self.step.project_id, step_id=self.step.id)

    # ------------------------------------------------------------------------------------------------ events
    def emit(self, type: str, payload: dict[str, Any] | None = None) -> int:   # noqa: A002
        return self.rt.bus.emit(type, {"step_id": self.step.id, **(payload or {})}, self.step.project_id)

    # ------------------------------------------------------------------------------------------------ graph and gates
    def open_gate(self, gate: Gate) -> Gate:
        """Open ``gate`` and park this step in WAITING_USER (what the handler returns afterwards is ignored)."""
        opened = self.rt.gates.open_gate(gate, step=self.step)
        self.gate_opened = True
        self.gate_id = opened.id
        return opened

    def spawn(self, steps: list[Step]) -> list[Step]:
        """Add steps to this step's job (dependencies on existing steps are allowed)."""
        for s in steps:
            s.job_id = self.step.job_id
            if s.project_id is None:
                s.project_id = self.step.project_id
        return self.rt.scheduler.spawn(self.step.job_id, steps)

    # ------------------------------------------------------------------------------------------------ providers
    def provider(self, name: str) -> Any:
        """The provider adapter (``real | mock | disabled`` as configured): ``providers.registry.get(name)``, imported lazily."""
        try:
            from duoskin.providers import registry as provider_registry
        except ImportError as exc:
            raise StepFailure("the provider layer is not installed in this build", kind="other") from exc
        return provider_registry.get(name)

    def call_ctx(self) -> Any:
        """A ``providers.base.CallCtx`` (step id, heartbeat, check_cancel, progress) for provider calls."""
        try:
            from duoskin.providers.base import CallCtx

            return CallCtx(step_id=self.step.id, heartbeat=self.heartbeat, check_cancel=self.check_cancel,
                           progress=self.progress)
        except (ImportError, TypeError):
            return LocalCallCtx(self.step.id, self.heartbeat, self.check_cancel, self.progress)

    # ------------------------------------------------------------------------------------------------ subprocess
    def run_subprocess(self, argv: list[str], *, timeout_s: int | float, env: dict[str, str] | None = None,
                       cwd: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Run a child process (mesh worker, Blender) with a timeout and cancellation. Results come back through a JSON
        file, never stdout: ``{result_json}`` in ``argv`` and the ``DUOSKIN_RESULT_JSON`` environment variable hold its path.

        The child's pid and creation time are recorded in ``child_procs`` (and on the step) so a crash cannot leave an
        orphan: recovery kills them. The whole process tree is killed on timeout or cancel.

        Returns ``{"ok": bool, "returncode": int, "result": dict | None, "stdout": str, "stderr": str}`` where ``ok`` means
        exit code 0 *and* a readable result file (Blender exits 0 even when its script fails).
        Raises ``Cancelled`` or ``StepFailure(kind="timeout")``.
        """
        import psutil

        self.check_cancel()
        result_path = self.rt.paths.tmp_dir / f"{self.step.id}-{uuid.uuid4().hex[:8]}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        full_argv = [a.replace("{result_json}", str(result_path)) for a in argv]
        child_env = {**os.environ, **(env or {}), "DUOSKIN_RESULT_JSON": str(result_path), "PYTHONUTF8": "1"}
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(full_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,   # noqa: S603
                                env=child_env, cwd=cwd, encoding="utf-8", errors="replace", **kwargs)
        try:
            create_time = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            create_time = 0.0
        self.rt.repo.add_child_proc(proc.pid, create_time, self.step.id)
        self.rt.repo.mutate_step(self.step.id, lambda s: (setattr(s, "child_pid", proc.pid),
                                                          setattr(s, "child_create_time", create_time)))
        deadline = time.monotonic() + float(timeout_s)
        stdout = stderr = ""
        try:
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    pass
                self.heartbeat()
                try:
                    self.check_cancel()
                except Cancelled:
                    kill_process_tree(proc.pid)
                    raise
                if time.monotonic() > deadline:
                    kill_process_tree(proc.pid)
                    proc.communicate()
                    raise StepFailure(f"subprocess timed out after {timeout_s} s", kind="timeout", retryable=False,
                                      billed="no", user_hint="The 3D worker took too long and was stopped.")
        finally:
            if proc.poll() is None:
                kill_process_tree(proc.pid)
            self.rt.repo.remove_child_proc(proc.pid, create_time)
        if self.cancelled:
            raise Cancelled(self.step.id)
        result: dict[str, Any] | None = None
        try:
            parsed = json.loads(Path(result_path).read_text(encoding="utf-8"))
            result = parsed if isinstance(parsed, dict) else {"value": parsed}
        except (OSError, ValueError):
            result = None
        finally:
            try:
                result_path.unlink()
            except OSError:
                pass
        return {"ok": proc.returncode == 0 and result is not None, "returncode": proc.returncode, "result": result,
                "stdout": (stdout or "")[-TAIL_CHARS:], "stderr": (stderr or "")[-TAIL_CHARS:]}


def kill_process_tree(pid: int, create_time: float | None = None) -> bool:
    """Kill ``pid`` and its children with psutil. When ``create_time`` is given the process must match it (pids are reused)."""
    import psutil

    try:
        parent = psutil.Process(pid)
        if create_time is not None and abs(parent.create_time() - create_time) > 1.0:
            return False
        victims = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return False
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=3)
    return True
