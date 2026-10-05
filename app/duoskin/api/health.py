"""``GET /api/health``: liveness. No DB access beyond a ping, so it answers in well under a second (CHK-X05)."""
from __future__ import annotations

from fastapi import APIRouter

from duoskin import __version__
from duoskin.api import RT
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


@router.get("/health")
def health(rt: Runtime = RT) -> dict[str, object]:
    return {"ok": rt.db.ping(), "version": __version__, "instance_id": rt.instance_id, "demo": rt.demo}
