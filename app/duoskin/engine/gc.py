"""Storage clean-up (APP_SPEC §8.5, ENG-12, CHK-X03). ``python -m duoskin gc [--dry-run]`` and Settings -> Storage.

Dry run first, always available. A purge deletes only assets that **no** protected record references:

* an approval, a gate decision, a gate (tile assets), a change request, a part, a spec, a project (reference images);
* an export (asset links with status ``export``, ``approved``, ``final`` or ``chosen``);
* a registry row (``registry_face``, ``registry_print``), the cross-duo memory or the Tripo/inbox table;
* a step created within the last N days (default 30).

References are found by scanning those rows for 64-hex strings, so a new place that stores a sha is protected by
default. Cache rows whose outputs were deleted are dropped; old events and temp files are pruned too.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from duoskin.db.repo import shas_in
from duoskin.engine.cas import EXT_BY_KIND
from duoskin.models.common import iso_utc, utcnow

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.gc")

PROTECTED_LINK_STATUSES = ("chosen", "final", "approved", "export")
_JSON_TABLES = ("parts", "approvals", "decisions", "gates", "changes", "projects", "specs", "registry_face",
                "registry_print", "duo_memory", "inbox")


@dataclass
class GcItem:
    sha: str
    kind: str
    bytes: int
    created_at: str


@dataclass
class GcReport:
    dry_run: bool
    older_than_days: int
    assets_total: int = 0
    referenced: int = 0
    candidates: list[GcItem] = field(default_factory=list)
    deleted: int = 0
    freed_bytes: int = 0
    orphan_files: list[str] = field(default_factory=list)
    cache_rows_dropped: int = 0
    events_pruned: int = 0
    tmp_files_removed: int = 0

    @property
    def candidate_bytes(self) -> int:
        return sum(c.bytes for c in self.candidates)

    def summary(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        n = len(self.candidates) if self.dry_run else self.deleted
        size = self.candidate_bytes if self.dry_run else self.freed_bytes
        return (f"{verb} {n} unreferenced assets ({size / 1_048_576:.1f} MB); {len(self.orphan_files)} stray files; "
                f"{self.referenced} of {self.assets_total} assets are referenced")


def referenced_shas(rt: Runtime, *, step_cutoff_iso: str) -> set[str]:
    conn = rt.db.conn()
    refs: set[str] = set()
    for table in _JSON_TABLES:
        for row in conn.execute(f"SELECT json FROM {table}"):   # noqa: S608 - fixed table names
            refs |= shas_in(row[0])
    for row in conn.execute(
            f"SELECT asset_sha FROM asset_links WHERE status IN ({','.join('?' for _ in PROTECTED_LINK_STATUSES)})",
            PROTECTED_LINK_STATUSES):
        refs.add(row[0])
    for row in conn.execute("SELECT json FROM steps WHERE created_at >= ?", (step_cutoff_iso,)):
        refs |= shas_in(row[0])
    return refs


def gc(rt: Runtime, *, dry_run: bool = True, older_than_days: int = 30, now=None) -> GcReport:
    now = now or utcnow()
    cutoff = iso_utc(now - timedelta(days=older_than_days))
    report = GcReport(dry_run=dry_run, older_than_days=older_than_days)
    refs = referenced_shas(rt, step_cutoff_iso=cutoff)
    rows = rt.db.conn().execute("SELECT sha256, kind, bytes, created_at FROM assets").fetchall()
    report.assets_total = len(rows)
    for r in rows:
        if r["sha256"] in refs:
            report.referenced += 1
        elif r["created_at"] < cutoff:
            report.candidates.append(GcItem(r["sha256"], r["kind"], int(r["bytes"]), r["created_at"]))
    known = {r["sha256"] for r in rows}
    hour_ago = time.time() - 3600
    for path in rt.cas.all_files():
        stem = path.name.split(".")[0]
        if stem not in known and path.stat().st_mtime < hour_ago:
            report.orphan_files.append(str(path))
    if dry_run:
        return report
    for item in report.candidates:
        path = rt.cas.path(item.sha, EXT_BY_KIND[item.kind])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", path.name)
            continue
        with rt.db.tx() as c:
            c.execute("DELETE FROM asset_links WHERE asset_sha=?", (item.sha,))
            c.execute("DELETE FROM assets WHERE sha256=?", (item.sha,))
        report.deleted += 1
        report.freed_bytes += item.bytes
    for p in report.orphan_files:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    report.cache_rows_dropped = rt.cache.prune_missing()
    report.events_pruned = rt.bus.prune()
    report.tmp_files_removed = _clean_tmp(rt.paths.tmp_dir)
    return report


def _clean_tmp(tmp: Path, older_than_s: float = 86400.0) -> int:
    n = 0
    cutoff = time.time() - older_than_s
    if not tmp.exists():
        return 0
    for p in tmp.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n
