"""``GET/PUT /api/settings`` (APP_SPEC §13, §14). API keys are never part of settings."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from duoskin.api import RT
from duoskin.engine.runtime import Runtime
from duoskin.models.settings import Settings, SettingsPatchError

router = APIRouter(prefix="/api")


@router.get("/settings")
def get_settings(rt: Runtime = RT) -> Settings:
    return rt.settings


@router.put("/settings")
def put_settings(patch: Annotated[dict[str, Any], Body()], rt: Runtime = RT) -> Settings:
    """Deep-merge a nested partial ``Settings`` object (unknown fields and ``schema_version``/``telemetry`` changes are refused)."""
    try:
        return rt.update_settings(patch)
    except SettingsPatchError as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_settings", "message": str(exc)}) from exc
