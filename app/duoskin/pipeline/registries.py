"""The face and print registries (APP_SPEC §3.7, §6.13): what was registered after earlier duos, read by the ``A_REGISTRY`` check.

Rows are written at the Gate 3 pick (``duo.memory``) so abandoned duos never block future faces. Exact pixel reuse is blocked forever,
per part, for AI-made parts only; near-duplicates are checked on the assembled ``face_canvas`` and on the whole print within the
sliding window of the last 30 duos or against rows with ``listed=1``. Demo, drill and regression streams never enter the registries.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from duoskin.models.common import iso_utc, new_id, utcnow

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.registries")

PRINT_KINDS = ("print", "badge")
TABLE = {"print": "registry_print", "badge": "registry_print"}


def _table(kind: str) -> str:
    return TABLE.get(kind, "registry_face")


def rows(rt: Runtime, kind: str, *, exclude_project: str | None = None) -> list[Any]:
    """``RegistryRow`` objects of one kind from the registry table (other projects' rows only)."""
    from duoskin.imaging.similarity import RegistryRow

    tbl = _table(kind)
    sql = f"SELECT project_id, part_role, asset_sha, pixel_sha, phash, embedding, duo_seq, listed FROM {tbl} WHERE part_role=?"
    args: list[Any] = [kind]
    if exclude_project:
        sql += " AND project_id != ?"
        args.append(exclude_project)
    out = []
    for r in rt.db.conn().execute(sql, args).fetchall():
        try:
            ph = int(r["phash"], 16) if r["phash"] else None
        except ValueError:
            ph = None
        out.append(RegistryRow(kind=kind, pixel_sha=r["pixel_sha"], phash=ph, duo_seq=int(r["duo_seq"]), listed=bool(r["listed"]),
                               asset_id=r["asset_sha"][:12]))
    return out


def current_seq(rt: Runtime) -> int:
    """The position of the duo being made in the approved-duo sequence (the window counts duos, not days)."""
    row = rt.db.conn().execute("SELECT MAX(duo_seq) AS m FROM registry_face").fetchone()
    row2 = rt.db.conn().execute("SELECT MAX(duo_seq) AS m FROM registry_print").fetchone()
    return int(max(row["m"] or 0, row2["m"] or 0)) + 1


def register(rt: Runtime, project_id: str, kind: str, asset_sha: str, *, duo_seq: int, listed: bool = False,
             grammar: dict[str, Any] | None = None) -> str | None:
    """Register one asset (an exact sha of an AI-made part, or the assembled canvas / print). Returns the row id, or None for a mock
    or non-pipeline asset, which never enters a registry."""
    from duoskin.imaging import similarity

    asset = rt.cas.get_asset(asset_sha)
    if asset.first_provenance.source == "mock" or asset.first_provenance.stream != "pipeline":
        return None
    im = _open(rt, asset_sha)
    fp = similarity.registry_fingerprint(im)
    row_id = new_id("reg")
    with rt.db.tx() as c:
        c.execute(f"INSERT INTO {_table(kind)} (id, project_id, part_role, asset_sha, pixel_sha, phash, embedding, json, duo_seq, listed, "
                  "registered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (row_id, project_id, kind, asset_sha, fp["pixel_sha"], f"{fp['phash']:x}", None,
                   json.dumps({"grammar": grammar or {}}), duo_seq, 1 if listed else 0, iso_utc(utcnow())))
    return row_id


def _open(rt: Runtime, sha: str):
    from duoskin.pipeline import common

    return common.open_image(rt.cas.get(sha))
