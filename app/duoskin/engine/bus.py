"""Event bus (APP_SPEC §6.12, §8.7).

Every event is inserted into the ``events`` table first (row id = the SSE ``id:``), and only after that transaction
commits is it pushed to the in-memory subscribers with ``loop.call_soon_threadsafe``. A subscriber that joins late
replays rows with ``id > after`` from the table (``events_after``), so no event is ever lost between a page's snapshot
and its stream: subscribe *first*, then replay, then tail, skipping ids already seen.

Throttling: ``emit_progress`` lets through at most 2 progress events per second per step. ``llm.thinking`` text is cut
to 400 characters. ``prune`` deletes events older than 30 days (``llm.thinking`` after 24 hours).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from duoskin.db.db import Database
from duoskin.models.common import iso_utc, parse_iso, utcnow

log = logging.getLogger("duoskin.bus")

EVENT_TYPES = frozenset({
    "step.state", "step.progress", "job.state", "gate.opened", "gate.updated", "tile.updated", "part.state",
    "spec.updated", "cost.added", "budget.low", "project.stage", "llm.thinking", "inbox.file", "doctor.result", "toast",
    "warning.released", "server.restarting",
})
THINKING_MAX_CHARS = 400
PROGRESS_MIN_INTERVAL_S = 0.5
SUBSCRIBER_QUEUE_SIZE = 1000


class Event(BaseModel):
    id: int
    ts: str
    project_id: str | None = None
    type: str
    payload: dict[str, Any]

    def sse_data(self) -> str:
        return self.model_dump_json()


@dataclass
class Subscription:
    """An asyncio queue fed from any thread. ``get()`` returns an ``Event``, or ``None`` when the stream must end
    (the subscriber was too slow or the bus closed); the client then reconnects with ``Last-Event-ID``."""

    bus: EventBus
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Event | None]
    closed: bool = False

    def close(self) -> None:
        self.closed = True
        self.bus._remove(self)

    def _deliver(self, event: Event | None) -> None:   # runs on the subscriber's loop thread
        if self.closed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("event subscriber too slow; closing its stream")
            self.closed = True
            self.bus._remove(self)
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


class EventBus:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._progress_last: dict[str, float] = {}

    # ------------------------------------------------------------------------------------------------ emit
    def emit(self, type: str, payload: dict[str, Any] | None = None, project_id: str | None = None) -> int:   # noqa: A002
        """Insert the event and push it to subscribers after commit. Returns the event id."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type '{type}'")
        payload = dict(payload or {})
        if type == "llm.thinking" and isinstance(payload.get("text"), str):
            payload["text"] = payload["text"][:THINKING_MAX_CHARS]
        ts = iso_utc(utcnow())
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO events (ts, project_id, type, payload) VALUES (?,?,?,?)",
                            (ts, project_id, type, json.dumps(payload, ensure_ascii=False, default=str)))
            event_id = int(cur.lastrowid or 0)
            event = Event(id=event_id, ts=ts, project_id=project_id, type=type, payload=json.loads(
                json.dumps(payload, ensure_ascii=False, default=str)))
            self.db.on_commit(lambda: self._push(event))
        return event_id

    def emit_progress(self, step_id: str, payload: dict[str, Any], project_id: str | None = None, *,
                      force: bool = False) -> int | None:
        """``step.progress`` at most twice a second per step (``force=True`` for the final 100% update)."""
        now = time.monotonic()
        with self._lock:
            last = self._progress_last.get(step_id)
            if not force and last is not None and now - last < PROGRESS_MIN_INTERVAL_S:
                return None
            self._progress_last[step_id] = now
        return self.emit("step.progress", {"step_id": step_id, **payload}, project_id)

    def forget_step(self, step_id: str) -> None:
        with self._lock:
            self._progress_last.pop(step_id, None)

    def _push(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub._deliver, event)
            except RuntimeError:   # the subscriber's loop is closed
                self._remove(sub)

    # ------------------------------------------------------------------------------------------------ subscribe
    def subscribe(self, loop: asyncio.AbstractEventLoop | None = None) -> Subscription:
        loop = loop or asyncio.get_running_loop()
        sub = Subscription(self, loop, asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE))
        with self._lock:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def close(self) -> None:
        """Tell every subscriber to end its stream (server shutdown)."""
        with self._lock:
            subs, self._subs = list(self._subs), []
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub._deliver, None)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------------------------------------ replay
    def events_after(self, after_id: int = 0, *, limit: int = 500, project_id: str | None = None) -> list[Event]:
        sql, args = "SELECT id, ts, project_id, type, payload FROM events WHERE id>?", [int(after_id)]
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        rows = self.db.conn().execute(sql + " ORDER BY id LIMIT ?", (*args, int(limit))).fetchall()
        return [Event(id=r["id"], ts=r["ts"], project_id=r["project_id"], type=r["type"], payload=json.loads(r["payload"]))
                for r in rows]

    def max_event_id(self) -> int:
        row = self.db.conn().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------------------------------------ prune
    def prune(self, *, keep_days: int = 30, thinking_hours: int = 24) -> int:
        now = utcnow()
        with self.db.tx() as c:
            a = c.execute("DELETE FROM events WHERE ts < ?", (iso_utc(now - timedelta(days=keep_days)),)).rowcount
            b = c.execute("DELETE FROM events WHERE type='llm.thinking' AND ts < ?",
                          (iso_utc(now - timedelta(hours=thinking_hours)),)).rowcount
        return a + b


def event_age_s(event: Event) -> float:
    return (utcnow() - parse_iso(event.ts)).total_seconds()
