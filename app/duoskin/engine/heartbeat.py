"""Lease heartbeat (APP_SPEC §8.4): every 30 s extend ``lease_until`` of every RUNNING step this instance owns.

Long blocking SDK calls never call ``progress()``, so without this thread a healthy 10-minute call would look dead.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duoskin.engine.runtime import Runtime

log = logging.getLogger("duoskin.heartbeat")

DEFAULT_INTERVAL_S = 30.0


class Heartbeat:
    def __init__(self, rt: Runtime, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        self.rt = rt
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0

    def beat(self) -> int:
        """Extend the leases once. Returns how many steps were extended."""
        n = self.rt.ops.extend_leases(self.rt.instance_id)
        self.beats += 1
        return n

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="duoskin-heartbeat", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.beat()
            except Exception:   # noqa: BLE001 - a failed beat must not kill the thread; the next one retries
                log.exception("heartbeat failed")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
