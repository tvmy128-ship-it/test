"""``GET /api/events`` (SSE) and ``GET /api/events/poll`` (APP_SPEC §8.7).

The stream replays rows with ``id > after`` from the ``events`` table before tailing, subscribing *first* so nothing is
lost in between. ``Last-Event-ID`` (an automatic browser reconnect) wins over ``?after``. Pings every 15 s keep the
connection alive. ``?once=1`` replays and then closes (used by tests and by clients that only want the backlog).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from duoskin.api import get_rt
from duoskin.engine.runtime import Runtime

router = APIRouter(prefix="/api")
REPLAY_BATCH = 500


def _after_id(request: Request, after: int) -> int:
    last = request.headers.get("last-event-id")
    if last and last.strip().isdigit():
        return int(last.strip())
    return after


@router.get("/events/poll")
def poll(after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000), rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    events = rt.bus.events_after(after, limit=limit)
    return {"events": [e.model_dump(mode="json") for e in events], "max_event_id": rt.bus.max_event_id()}


async def event_stream(rt: Runtime, after: int, *, once: bool = False) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE dicts (``id``, ``data``): backlog from the table, then live events. Ends when the client leaves."""
    loop = asyncio.get_running_loop()
    sub = rt.bus.subscribe(loop)
    last = after
    try:
        while True:
            batch = await loop.run_in_executor(None, lambda: rt.bus.events_after(last, limit=REPLAY_BATCH))
            for e in batch:
                last = e.id
                yield {"id": str(e.id), "data": e.sse_data()}
            if len(batch) < REPLAY_BATCH:
                break
        if once:
            return
        while True:
            event = await sub.queue.get()   # cancelled by sse-starlette when the client disconnects
            if event is None:
                return
            if event.id <= last:
                continue   # already replayed from the table
            last = event.id
            yield {"id": str(event.id), "data": event.sse_data()}
    finally:
        sub.close()


@router.get("/events")
async def events(request: Request, after: int = Query(0, ge=0), once: bool = Query(False),
                 rt: Runtime = Depends(get_rt)) -> EventSourceResponse:
    return EventSourceResponse(event_stream(rt, _after_id(request, after), once=once), ping=15)
