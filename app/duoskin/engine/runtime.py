"""``Runtime``: everything one running app instance owns (paths, settings, DB, CAS, cache, bus, budget, keys, scheduler).

``create_app`` builds one and stores it at ``app.state.rt``; tests build one directly with ``Runtime.create(tmp_path)``.
Handlers reach it as ``ctx.rt``; routers as ``request.app.state.rt``.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from duoskin import config, keystore
from duoskin.config import DataPaths
from duoskin.db.db import Database, set_default
from duoskin.db.repo import Repo
from duoskin.engine.budget import BudgetService
from duoskin.engine.bus import EventBus
from duoskin.engine.cache import StepCache
from duoskin.engine.cas import Cas
from duoskin.engine.gates import GateService
from duoskin.engine.heartbeat import Heartbeat
from duoskin.engine.scheduler import Scheduler
from duoskin.engine.steps import StepOps
from duoskin.keystore import KeyStore
from duoskin.models.common import utcnow
from duoskin.models.settings import KNOWN_PROVIDERS, ProviderMode, Settings, SettingsPatchError, apply_settings_patch

log = logging.getLogger("duoskin.runtime")


def parse_providers_mode(text: str | None) -> dict[str, ProviderMode]:
    """``"mock"`` (all providers) or ``"anthropic:real,tripo:mock,..."`` (``DUOSKIN_PROVIDERS``, APP_SPEC §16)."""
    if not text:
        return {}
    text = text.strip().lower()
    if text in ("real", "mock", "disabled"):
        return {p: ProviderMode(text) for p in KNOWN_PROVIDERS}
    out: dict[str, ProviderMode] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, mode = part.partition(":")
        if name not in KNOWN_PROVIDERS or mode not in ("real", "mock", "disabled"):
            raise ValueError(f"bad providers mode '{part}' (expected provider:real|mock|disabled)")
        out[name] = ProviderMode(mode)
    return out


class Runtime:
    def __init__(self, paths: DataPaths, settings: Settings, *, providers_mode: str | None = None,
                 keys: KeyStore | None = None, heartbeat_interval_s: float = 30.0, scheduler_kwargs: dict[str, Any] | None = None,
                 make_default: bool = True) -> None:
        self.paths = paths
        self._settings = settings
        self._effective: Settings | None = None
        self.provider_override = parse_providers_mode(providers_mode if providers_mode is not None
                                                      else os.environ.get("DUOSKIN_PROVIDERS"))
        self.instance_id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.started_at = utcnow()
        self.port: int | None = None
        self.bound_host = "127.0.0.1"
        self.started_via_module = False
        self.shutting_down = False
        self.on_shutdown: Callable[[], None] | None = None
        self._settings_lock = threading.RLock()
        self.doctor_report: dict[str, Any] | None = None
        self.doctor_running = False

        self.db = Database(paths.db)
        self.db.migrate()
        if make_default:
            set_default(self.db)
        self.repo = Repo(self.db)
        self.bus = EventBus(self.db)
        self.cas = Cas(paths.cas_dir, self.db)
        self.cache = StepCache(self.db, self.cas)
        self.keys = keys if keys is not None else KeyStore(paths)
        self.budget = BudgetService(self.db, self.bus, self.effective_settings, self.repo.find_project)
        self.ops = StepOps(self)
        self.gates = GateService(self)
        self.scheduler = Scheduler(self, **(scheduler_kwargs or {}))
        self.heartbeat = Heartbeat(self, heartbeat_interval_s)
        self.doctor_report = self.repo.kv_get("doctor.last")

    # ------------------------------------------------------------------------------------------------ factory
    @classmethod
    def create(cls, home: str | os.PathLike[str] | None = None, *, providers_mode: str | None = None, **kw: Any) -> Runtime:
        paths = config.paths(home)
        return cls(paths, config.load_settings(paths.home), providers_mode=providers_mode, **kw)

    # ------------------------------------------------------------------------------------------------ settings
    @property
    def settings(self) -> Settings:
        """The persisted settings (what ``settings.json`` holds)."""
        return self._settings

    def effective_settings(self) -> Settings:
        """Persisted settings with the ``providers_mode`` / ``DUOSKIN_PROVIDERS`` override applied (not persisted)."""
        eff = self._effective
        if eff is None:
            with self._settings_lock:
                if self.provider_override:
                    data = self._settings.model_dump(mode="json")
                    data["providers"]["modes"].update({k: v.value for k, v in self.provider_override.items()})
                    eff = Settings.model_validate(data)
                else:
                    eff = self._settings
                self._effective = eff
        return eff

    def update_settings(self, patch: dict[str, Any]) -> Settings:
        """Apply a nested partial update, persist it and return the new persisted settings."""
        with self._settings_lock:
            new = apply_settings_patch(self._settings, patch)
            config.save_settings(new, self.paths.home)
            self._settings = new
            self._effective = None
        self.scheduler.notify()
        return new

    def remember_port(self, port: int) -> None:
        """Sticky port: write the port we actually bound back to ``settings.json``."""
        self.port = port
        if self._settings.port != port:
            try:
                self.update_settings({"port": port})
            except SettingsPatchError:
                log.warning("could not save the sticky port")

    @property
    def demo(self) -> bool:
        s = self.effective_settings()
        if s.demo_mode:
            return True
        modes = [m for p, m in s.providers.modes.items() if m != ProviderMode.DISABLED]
        return bool(modes) and all(m == ProviderMode.MOCK for m in modes) and bool(self.provider_override)

    # ------------------------------------------------------------------------------------------------ doctor
    def set_doctor_report(self, report: dict[str, Any]) -> None:
        self.doctor_report = report
        try:
            self.repo.kv_set("doctor.last", report)
        except Exception:   # noqa: BLE001
            log.exception("could not store the doctor report")

    def paid_blocked_reason(self) -> str | None:
        """Why paid (real-provider) steps must not run: a HARD doctor failure, or the daily cap. None when clear.
        A fresh install is never blocked: missing house style, head base and DreamSim only warn (issue file #2)."""
        report = self.doctor_report
        if report and report.get("blocks_paid_features"):
            failed = [c.get("id", "?") for c in report.get("checks", []) if c.get("blocking")]
            return "doctor found problems that block paid features: " + ", ".join(failed)
        try:
            if self.budget.daily_cap_reached():
                return "the daily spending cap was reached"
        except Exception:   # noqa: BLE001
            log.exception("daily cap check failed")
        return None

    # ------------------------------------------------------------------------------------------------ lifecycle
    def startup(self, *, recover: bool = True, start_threads: bool = True, backup: bool = True) -> None:
        """Integrity check, backup, recovery, then start the heartbeat and scheduler threads."""
        problems = self.db.integrity_check()
        if problems:
            log.error("PRAGMA integrity_check reported problems: %s", problems[:3])
        if backup:
            try:
                self.db.backup(self.paths.backups_dir, keep=5)
            except Exception:   # noqa: BLE001
                log.exception("database backup failed")
        keystore.configure(self.keys)
        self.keys.key_status()          # reads every key once so the log redactor knows their exact values
        config.register_runtime(self)
        try:
            self.bus.prune()
        except Exception:   # noqa: BLE001
            log.exception("event prune failed")
        if recover:
            from duoskin.engine.recovery import recover as run_recovery

            run_recovery(self)
        if start_threads:
            self.heartbeat.start()
            self.scheduler.start()

    def shutdown(self) -> None:
        """Stop threads, checkpoint the WAL and close every connection. Safe to call twice."""
        if self.shutting_down:
            return
        self.shutting_down = True
        try:
            self.scheduler.stop()
            self.heartbeat.stop()
            self.bus.close()
        finally:
            self.db.close_all()
        if config.active_runtime() is self:
            config.register_runtime(None)

    def request_shutdown(self) -> None:
        """``POST /api/shutdown``: ask the host (uvicorn in ``run``) to stop; tests install a recorder instead."""
        cb = self.on_shutdown
        if cb is not None:
            cb()


def default_paths_for(home: Path | None) -> DataPaths:
    return config.paths(home)
