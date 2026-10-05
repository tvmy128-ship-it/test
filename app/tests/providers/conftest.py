"""Provider tests: no outbound sockets, and clean process-wide state (limiters, fault injector, registry, env)."""
from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(self, *a, **kw):
        raise RuntimeError("outbound sockets are blocked in the provider tests (use a MockTransport)")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture(autouse=True)
def _clean_provider_state(monkeypatch):
    from duoskin.providers import base, faults, registry
    monkeypatch.delenv("DUOSKIN_MOCK_FAULTS", raising=False)
    monkeypatch.delenv("DUOSKIN_PROVIDERS", raising=False)
    base.reset_limiters()
    faults.reset_default_injector()
    registry.reset()
    yield
    base.reset_limiters()
    faults.reset_default_injector()
    registry.reset()
