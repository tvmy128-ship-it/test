"""SQLite access: one connection per thread, ``BEGIN IMMEDIATE`` write transactions, migrations (APP_SPEC §6.14).

Connection rules (ENG-13): ``timeout=10``, ``isolation_level=None`` (we issue ``BEGIN``/``COMMIT`` ourselves), WAL,
``synchronous=NORMAL``, ``busy_timeout=5000``, ``foreign_keys=ON``. Every write transaction is ``db.tx()``, which starts
with ``BEGIN IMMEDIATE`` so writers queue instead of failing with "database is locked" halfway through. Nested
``tx()`` calls become SAVEPOINTs. ``on_commit(fn)`` runs ``fn`` after the *outermost* commit (used by the event bus, so
subscribers never hear about a rolled-back change).

A ``Database`` is an object (not a module global) so tests and several in-process apps each get their own file. The
module-level ``conn()``, ``tx()`` and ``migrate()`` of APP_SPEC §5.4 delegate to the *default* database, which
``set_default()`` installs (``create_app`` does it for the app it builds).
"""
from __future__ import annotations

import logging
import random
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from duoskin.db.errors import SchemaTooNew
from duoskin.models.common import utcnow

log = logging.getLogger("duoskin.db")

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class _ThreadState(threading.local):
    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None
        self.depth = 0
        self.hooks: list[Callable[[], None]] = []


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000, begin_deadline_s: float = 20.0) -> None:
        self.path = Path(path)
        self._busy_ms = busy_timeout_ms
        self._begin_deadline = begin_deadline_s
        self._state = _ThreadState()
        self._all_lock = threading.Lock()
        self._all: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._closed = False
        self.lock_retries = 0

    # ------------------------------------------------------------------------------------------------ connections
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def conn(self) -> sqlite3.Connection:
        """This thread's connection (created on first use)."""
        if self._closed:
            raise RuntimeError("database is closed")
        c = self._state.conn
        if c is None:
            c = self._connect()
            self._state.conn = c
            with self._all_lock:
                self._prune_dead_locked()
                self._all.append((threading.current_thread(), c))
        return c

    def _prune_dead_locked(self) -> None:
        keep: list[tuple[threading.Thread, sqlite3.Connection]] = []
        for thread, c in self._all:
            if thread.is_alive():
                keep.append((thread, c))
            else:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
        self._all = keep

    def in_tx(self) -> bool:
        return self._state.depth > 0

    def on_commit(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` after the outermost transaction commits (immediately when no transaction is open)."""
        if self._state.depth > 0:
            self._state.hooks.append(fn)
        else:
            fn()

    # ------------------------------------------------------------------------------------------------ transactions
    def _begin(self, conn: sqlite3.Connection) -> None:
        deadline = time.monotonic() + self._begin_deadline
        delay = 0.004
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("locked" in msg or "busy" in msg) and time.monotonic() < deadline:
                    self.lock_retries += 1
                    time.sleep(delay + random.random() * delay)   # noqa: S311 - jitter, not security
                    delay = min(delay * 2, 0.1)
                    continue
                raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on any exception). Nested calls are SAVEPOINTs."""
        st = self._state
        conn = self.conn()
        if st.depth > 0:
            st.depth += 1
            name = f"sp{st.depth}"
            hooks_len = len(st.hooks)
            conn.execute(f"SAVEPOINT {name}")
            try:
                yield conn
            except BaseException:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
                del st.hooks[hooks_len:]
                raise
            else:
                conn.execute(f"RELEASE {name}")
            finally:
                st.depth -= 1
            return
        self._begin(conn)
        st.depth = 1
        try:
            yield conn
        except BaseException:
            st.depth = 0
            st.hooks.clear()
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                st.depth = 0
                st.hooks.clear()
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            st.depth = 0
            hooks, st.hooks = st.hooks, []
            for hook in hooks:
                try:
                    hook()
                except Exception:   # noqa: BLE001 - a failing subscriber must not undo a committed change
                    log.exception("post-commit hook failed")

    # ------------------------------------------------------------------------------------------------ migrations
    @staticmethod
    def migrations() -> list[tuple[int, Path]]:
        found: list[tuple[int, Path]] = []
        for f in sorted(SCHEMA_DIR.glob("*.sql")):
            m = _MIGRATION_RE.match(f.name)
            if m:
                found.append((int(m.group(1)), f))
        return found

    def schema_version(self) -> int:
        return int(self.conn().execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """Apply every ``schema/NNN_*.sql`` newer than ``PRAGMA user_version``, each in its own transaction."""
        conn = self.conn()
        migrations = self.migrations()
        latest = migrations[-1][0] if migrations else 0
        current = self.schema_version()
        if current > latest:
            raise SchemaTooNew(f"database schema {current} is newer than this app ({latest})")
        for number, path in migrations:
            if number <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;"
            try:
                conn.executescript(script)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            log.info("applied migration %s", path.name)
            current = number
        return current

    # ------------------------------------------------------------------------------------------------ maintenance
    def ping(self) -> bool:
        try:
            self.conn().execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def integrity_check(self) -> list[str]:
        """``PRAGMA integrity_check``: an empty list means "ok"; otherwise the problems reported."""
        rows = [r[0] for r in self.conn().execute("PRAGMA integrity_check").fetchall()]
        return [] if rows == ["ok"] else rows

    def checkpoint(self, mode: str = "TRUNCATE") -> tuple[int, int, int]:
        if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(mode)
        row = self.conn().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def backup(self, dest_dir: Path, keep: int = 5) -> Path | None:
        """``Connection.backup()`` into ``dest_dir`` and keep only the newest ``keep`` copies. None if nothing to back up."""
        if not self.path.exists() or self.schema_version() == 0:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        target = dest_dir / f"duoskin-{stamp}.sqlite3"
        n = 1
        while target.exists():
            n += 1
            target = dest_dir / f"duoskin-{stamp}-{n}.sqlite3"
        dest = sqlite3.connect(str(target))
        try:
            self.conn().backup(dest)
        finally:
            dest.close()
        copies = sorted(dest_dir.glob("duoskin-*.sqlite3"), key=lambda p: (p.stat().st_mtime, p.name))
        for old in copies[:-keep] if keep > 0 else copies:
            try:
                old.unlink()
            except OSError:
                log.warning("could not delete old backup %s", old.name)
        return target

    def close_all(self) -> None:
        """Checkpoint the WAL and close every thread's connection (clean shutdown)."""
        try:
            if not self._closed and self._state.conn is not None:
                self.checkpoint("TRUNCATE")
        except sqlite3.Error:
            log.warning("WAL checkpoint failed at shutdown")
        self._closed = True
        with self._all_lock:
            for _thread, c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all = []
        self._state = _ThreadState()


# ---------------------------------------------------------------------------------------------- default database
_default: dict[str, Database | None] = {"db": None}


def set_default(db: Database | None) -> None:
    _default["db"] = db


def default() -> Database:
    db = _default["db"]
    if db is None:
        raise RuntimeError("no default database: build a Runtime (create_app) first")
    return db


def conn() -> sqlite3.Connection:
    return default().conn()


def tx():
    return default().tx()


def migrate() -> int:
    return default().migrate()
