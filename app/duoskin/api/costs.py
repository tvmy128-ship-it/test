"""``GET /api/costs``: ledger rows and totals (APP_SPEC §8.8, §13)."""
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Query

from duoskin import config
from duoskin.api import RT
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
def costs(project_id: str | None = None, from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
          limit: Annotated[int, Query(ge=1, le=5000)] = 500, rt: Runtime = RT) -> dict[str, Any]:
    rows = rt.budget.ledger(project_id, from_, to, limit)
    return {"rows": [r.model_dump(mode="json") for r in rows], "totals": rt.budget.totals(project_id, from_, to),
            "tripo_available_credits": rt.budget.tripo_available_credits(), "price_table": _price_table_date()}
