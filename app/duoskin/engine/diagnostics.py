"""Redacted diagnostics zip (APP_SPEC §8.6, ``export-diagnostics``, ``POST /api/diagnostics``).

Holds the app log files, the failing steps' JSON, the last doctor report and version facts. Every text goes through the
log redactor (``logsetup.redact``) on its way in, so no API key can end up in the zip (CHK-E06 scans for them too).
"""
from __future__ import annotations

import json
import platform
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from duoskin import __version__, config
from duoskin.logsetup import redact
from duoskin.models.common import utcnow

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime


def export_diagnostics(rt: Runtime, dest_dir: Path | None = None) -> Path:
    dest_dir = dest_dir or (config.exports_root(rt.effective_settings()) / "Diagnostics")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    target = dest_dir / f"duoskin-diagnostics-{stamp}.zip"
    n = 1
    while target.exists():
        n += 1
        target = dest_dir / f"duoskin-diagnostics-{stamp}-{n}.zip"
    failing = [s.model_dump(mode="json") for s in rt.repo.list_steps(state="failed", limit=200)]
    info = {"app_version": __version__, "python": sys.version, "platform": platform.platform(),
            "instance_id": rt.instance_id, "time": utcnow().isoformat(), "settings": rt.settings.model_dump(mode="json"),
            "step_counts": rt.ops.counts(), "db_schema_version": rt.db.schema_version()}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("info.json", redact(json.dumps(info, indent=2, default=str)))
        zf.writestr("failing_steps.json", redact(json.dumps(failing, indent=2, default=str)))
        zf.writestr("doctor.json", redact(json.dumps(rt.doctor_report or {}, indent=2, default=str)))
        for log_file in sorted(rt.paths.logs_dir.glob("*.log*")):
            try:
                zf.writestr(f"logs/{log_file.name}", redact(log_file.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return target
