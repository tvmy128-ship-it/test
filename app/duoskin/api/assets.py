"""``GET /cas/{sha}.{ext}``: content-addressed files (APP_SPEC §6.7, SYS-04).

The name is regex-checked (64 lower-case hex + a short extension) and only ever looked up in the ``assets`` table: no
user-supplied path is read. Responses are immutable-cached and sandboxed so an SVG or HTML asset can never run script in
our origin: ``Cache-Control: public, max-age=31536000, immutable``, ``X-Content-Type-Options: nosniff``,
``Content-Security-Policy: sandbox``. (The stub ``POST /api/uploads/mask`` belongs to the uploads router of a later track.)
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from duoskin.api import get_rt
from duoskin.engine.runtime import Runtime

router = APIRouter()
_NAME_RE = re.compile(r"^([0-9a-f]{64})\.([a-z0-9]{1,8})$")

CAS_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
}


@router.get("/cas/{name}")
def get_asset(name: str, rt: Runtime = Depends(get_rt)) -> FileResponse:
    m = _NAME_RE.match(name)
    if not m:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "no such asset"})
    found = rt.cas.describe(m.group(1), m.group(2))
    if found is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "no such asset"})
    path, mime, _kind = found
    return FileResponse(path, media_type=mime, headers=CAS_HEADERS)
