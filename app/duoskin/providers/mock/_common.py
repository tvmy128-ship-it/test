"""Shared plumbing of the mock providers."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any

from duoskin.providers.base import CallRecord, CapabilityFlags, request_hash
from duoskin.providers.faults import FaultInjector, default_injector, raise_for
from duoskin.providers.pricing import as_mock


class MockBase:
    """Request recording, fault checks and mock cost rows."""

    provider = "mock"

    def __init__(self, *, faults: FaultInjector | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 flags: CapabilityFlags | None = None) -> None:
        self._faults = faults
        self.cost_sink = cost_sink
        self.flags = flags or CapabilityFlags()
        self.requests: list[CallRecord] = []
        self.costs: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def faults(self) -> FaultInjector:
        """The injector given at construction, else the process-wide one built from ``DUOSKIN_MOCK_FAULTS``."""
        return self._faults or default_injector()

    def record(self, provider: str, operation: str, request: Any) -> CallRecord:
        rec = CallRecord(provider=provider, operation=operation, request=_summary(request), request_sha=request_hash(request))
        with self._lock:
            self.requests.append(rec)
        return rec

    def fault(self, provider: str, *, tag: str = "", seed: int | None = None, aliases: Iterable[str] = ()) -> str | None:
        """Count a faultable call. Error faults raise the real ``ProviderError``; behavioural faults are returned."""
        name = self.faults.check(provider, tag=tag, seed=seed, aliases=aliases)
        if name:
            raise_for(provider, name, tag=tag)
        return name

    def record_cost(self, cost: dict[str, Any] | None) -> dict[str, Any] | None:
        if cost is None:
            return None
        row = as_mock(cost)
        with self._lock:
            self.costs.append(row)
        if self.cost_sink is not None:
            try:
                self.cost_sink(row)
            except Exception:  # noqa: BLE001, S110 - a ledger failure must not hide the result
                pass
        return row

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.costs.clear()


def _summary(request: Any) -> dict[str, Any]:
    from duoskin.providers.base import jsonable
    j = jsonable(request)
    return j if isinstance(j, dict) else {"value": j}
