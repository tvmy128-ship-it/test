"""HTTP client shim for the provider layer.

APP_SPEC section 4.1 pins ``httpx2`` (the module both official SDKs are built on; it checks TLS against the
Windows certificate store). ``httpx2`` is API-compatible with ``httpx``, so the REST adapters (Recraft, Tripo,
Gemini) import ``httpx`` from here: they get ``httpx2`` when it is installed and plain ``httpx`` otherwise.

Tests use ``duoskin.providers._http.httpx.MockTransport`` so that the transport matches the client class that
the adapter really builds (a ``httpx`` transport does not work inside an ``httpx2`` client).
"""
from __future__ import annotations

try:  # pragma: no cover - which branch runs depends on the environment
    import httpx2 as httpx
    HTTP_LIBRARY = "httpx2"
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]
    HTTP_LIBRARY = "httpx"

__all__ = ["HTTP_LIBRARY", "httpx", "make_client"]


def make_client(*, timeout: float, follow_redirects: bool = False, transport=None, headers: dict | None = None):
    """Build a synchronous client. ``transport`` is for tests (``httpx.MockTransport``)."""
    kw: dict = {"timeout": httpx.Timeout(timeout, connect=min(timeout, 30.0)), "follow_redirects": follow_redirects}
    if transport is not None:
        kw["transport"] = transport
    if headers:
        kw["headers"] = headers
    return httpx.Client(**kw)
