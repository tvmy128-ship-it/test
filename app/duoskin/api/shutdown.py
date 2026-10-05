"""``POST /api/shutdown``: 202, then the host stops gracefully (APP_SPEC §4.3: cancel flags, no-wait pool shutdown, WAL
checkpoint, ``os._exit(0)`` after a 2 s grace period; the host installs that callback as ``rt.on_shutdown``)."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse

from duoskin.api import RT
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


@router.post("/shutdown", status_code=202)
def shutdown(background: BackgroundTasks, rt: Runtime = RT) -> JSONResponse:
    background.add_task(rt.request_shutdown)
    return JSONResponse({"ok": True, "message": "DuoSkin Studio is shutting down."}, status_code=202,
                        background=background)
