"""``GET /api/doctor``, ``POST /api/doctor/run``, ``POST /api/diagnostics`` (APP_SPEC §13, §15.4)."""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from duoskin.api import RT
from duoskin.api.doctor_checks import run_doctor
from duoskin.engine.diagnostics import export_diagnostics
from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.doctor")
_start_lock = threading.Lock()
router = APIRouter(prefix="/api")


def run_in_background(rt: Runtime, *, quick: bool = False, setup: bool = False) -> bool:
    """Start the doctor on a thread; the result is stored, saved to the DB and announced as a ``doctor.result`` event.
    Returns False when a run is already in progress."""
    with _start_lock:
        if rt.doctor_running:
            return False
        rt.doctor_running = True

    def work() -> None:
        try:
            report = run_doctor(rt, quick=quick, setup=setup)
            rt.set_doctor_report(report)
            rt.bus.emit("doctor.result", {"summary": report["summary"], "exit_code": report["exit_code"],
                                          "blocks_paid_features": report["blocks_paid_features"]})
            rt.scheduler.notify()
        except Exception:
            log.exception("doctor run failed")
        finally:
            rt.doctor_running = False

    threading.Thread(target=work, name="duoskin-doctor", daemon=True).start()
    return True


@router.get("/doctor")
def get_doctor(rt: Runtime = RT) -> dict[str, Any]:
    if rt.doctor_report is None:
        return {"ran": False, "running": rt.doctor_running}
    return {"ran": True, "running": rt.doctor_running, **rt.doctor_report}


@router.post("/doctor/run", status_code=202)
def run_doctor_route(quick: bool = False, rt: Runtime = RT) -> JSONResponse:
    started = run_in_background(rt, quick=quick)
    return JSONResponse({"started": started, "running": True}, status_code=202)


@router.post("/diagnostics")
def diagnostics(rt: Runtime = RT) -> dict[str, str]:
    """Build the redacted diagnostics zip under ``EXPORTS\\Diagnostics`` and return its path."""
    return {"path": str(export_diagnostics(rt))}
