"""``POST /api/os/open-folder`` (APP_SPEC §13): show a folder of the app in Explorer.

The request names a *kind* and an *id*, never a path: ``{"kind": "export" | "tripo_pack" | "polish_pack" | "inbox" | "logs", "id": ...}``.
The folder is looked up in the app's own records, and it is opened only when all of these hold:

* the id is a plain token (letters, digits, ``._-``): no separator, drive, colon, ``..``, wildcard or NUL can reach the file system;
* the folder resolves (symlinks and junctions followed) to a **directory inside the exports root or the DATA folder**;
* it is a directory, never a file: ``os.startfile`` on a file would *run* it, so a file is refused before anything is called.

Only ``os.startfile(<resolved directory>)`` is ever called, on Windows; there is no ``shell=True``, no command line and no user text in
any argument. Elsewhere (a development machine) the answer is 501 and nothing opens. Tests replace ``OPENER``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from duoskin import config, winplat
from duoskin.api import RT
from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.os_open")
router = APIRouter(prefix="/api/os")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class OpenFolderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["export", "tripo_pack", "polish_pack", "inbox", "logs"]
    id: str = Field(default="", max_length=120)


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _allowed_roots(rt: Runtime) -> list[Path]:
    roots = [rt.paths.home, config.exports_root(rt.effective_settings())]
    out: list[Path] = []
    for r in roots:
        try:
            out.append(r.resolve())
        except OSError:
            continue
    return out


def _inside(path: Path, roots: list[Path]) -> bool:
    """Is ``path`` one of ``roots`` or below one? Compared case-insensitively on Windows (``os.path.normcase``), whole path parts only."""
    p = os.path.normcase(str(path))
    for r in roots:
        base = os.path.normcase(str(r)).rstrip("\\/")
        if p == base or p.startswith(base + os.sep):
            return True
    return False


def _pack_state(rt: Runtime, pack_id: str, *, polish: bool) -> dict[str, Any] | None:
    rows = rt.db.conn().execute("SELECT value FROM kv WHERE key LIKE 'pack:%'").fetchall()
    for row in rows:
        try:
            state = json.loads(row["value"])
        except ValueError:
            continue
        if isinstance(state, dict) and state.get("pack_id") == pack_id and (state.get("kind") == "polish") == polish:
            return state
    return None


def folder_for(rt: Runtime, kind: str, ident: str) -> Path:
    """The directory a request refers to, or an ``HTTPException``. Never touches a path the client wrote."""
    if kind in ("export", "tripo_pack", "polish_pack"):
        if not _ID_RE.match(ident):
            raise _refuse(422, "bad_id", "the id is not valid")
    elif ident and not _ID_RE.match(ident):
        raise _refuse(422, "bad_id", "the id is not valid")
    raw: str | None
    if kind == "export":
        from duoskin.pipeline import export

        raw = export.get_state(rt, ident).get("kit_dir")
    elif kind in ("tripo_pack", "polish_pack"):
        state = _pack_state(rt, ident, polish=kind == "polish_pack")
        raw = state.get("folder") if state else None
    elif kind == "inbox":
        raw = str(config.tripo_inbox(rt.effective_settings()))
    else:
        raw = str(rt.paths.logs_dir)
    if not raw:
        raise _refuse(404, "no_such_folder", "there is no such folder yet")
    try:
        folder = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise _refuse(404, "no_such_folder", "the folder is not there any more") from None
    if not folder.is_dir():
        raise _refuse(422, "not_a_folder", "only folders can be opened")
    if not _inside(folder, _allowed_roots(rt)):
        log.warning("refused to open a folder outside the data and exports folders (kind=%s)", kind)
        raise _refuse(403, "outside_app_folders", "this folder is outside the folders DuoSkin Studio owns, so it is not opened from here")
    return folder


def _startfile(folder: Path) -> None:
    """Windows only: ``os.startfile`` of a verified directory. Nothing else is ever launched."""
    if not winplat.IS_WINDOWS or not hasattr(os, "startfile"):
        raise _refuse(501, "unavailable", "opening folders is only available on Windows")
    text = str(folder).removeprefix("\\\\?\\")        # an extended-length path: Explorer wants the plain form
    os.startfile(text)   # type: ignore[attr-defined]  # a resolved, existing directory under DATA/EXPORTS (checked above)


#: tests swap this for a recorder
OPENER: dict[str, Callable[[Path], None]] = {"fn": _startfile}


@router.post("/open-folder", status_code=204)
def open_folder(body: OpenFolderIn, rt: Runtime = RT) -> Response:
    folder = folder_for(rt, body.kind, body.id)
    OPENER["fn"](folder)
    return Response(status_code=204)
