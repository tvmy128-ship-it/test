"""``GET /api/costs``: ledger rows and totals (APP_SPEC §8.8, §13)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from duoskin import config
from duoskin.api import get_rt
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")


def _price_table_date() -> str | None:
    p = config.APP_ROOT / "duoskin" / "data" / "prices.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("date") or data.get("version") or "prices.json") if isinstance(data, dict) else "prices.json"
    except (OSError, ValueError):
        return None


@router.get("/costs")
def costs(project_id: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
          limit: int = Query(500, ge=1, le=5000), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    rows = rt.budget.ledger(project_id, from_, to, limit)
    return {"rows": [r.model_dump(mode="json") for r in rows], "totals": rt.budget.totals(project_id, from_, to),
            "tripo_available_credits": rt.budget.tripo_available_credits(), "price_table": _price_table_date()}
