"""Local-only hardening (APP_SPEC §13, SYS-02, ENG-11, CHK-S06/S07).

``install(app, token, importmap_sha)`` adds, in this order of authority:

1. ``TrustedHostMiddleware(["127.0.0.1", "localhost"])``: any other ``Host`` header (DNS rebinding) gets 400.
2. ``SecurityMiddleware`` (pure ASGI, so SSE streams are never buffered):
   * every POST/PUT/PATCH/DELETE must carry ``X-DuoSkin-Token`` equal to this launch's token, else 403
     ``{"error": "bad_token"}`` (the UI reloads on that);
   * when an ``Origin`` header is present it must be exactly ``http://127.0.0.1:<port>``, else 403 ``bad_origin``;
   * every response gets ``X-Content-Type-Options: nosniff`` and ``Referrer-Policy: no-referrer``; HTML responses get the
     Content-Security-Policy; ``/api`` responses are ``Cache-Control: no-store``.
3. No CORS middleware, no auth cookie. The token travels in a ``<meta name="duoskin-token">`` tag of an ``index.html`` that
   is always served ``no-store`` (cookies are not port-scoped and a cached page would hold an old token).

CAS files carry their own headers (``Content-Security-Policy: sandbox``, ``nosniff``, immutable cache) set by their route.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from typing import Any

from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = b"x-duoskin-token"
META_NAME = "duoskin-token"
TOKEN_PLACEHOLDER = "__DUOSKIN_TOKEN__"

_IMPORTMAP_RE = re.compile(r"<script[^>]*type=[\"']importmap[\"'][^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_META_RE = re.compile(r"<meta\s+name=[\"']duoskin-token[\"']\s+content=[\"'][^\"']*[\"']\s*/?>", re.IGNORECASE)


def importmap_csp_source(html: str) -> str | None:
    """CSP source (``sha256-<base64>``) of the page's inline import map, or None when it has none."""
    m = _IMPORTMAP_RE.search(html)
    if not m:
        return None
    digest = hashlib.sha256(m.group(1).encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def content_security_policy(importmap: str | None = None) -> str:
    script = "script-src 'self'" + (f" '{importmap}'" if importmap else "")
    return ("default-src 'self'; img-src 'self' blob: data:; " + script +
            "; style-src 'self'; object-src 'none'; frame-ancestors 'none'")


def inject_token(html: str, token: str) -> str:
    """Put the launch token into the ``<meta name="duoskin-token">`` tag (add the tag when the page has none)."""
    tag = f'<meta name="{META_NAME}" content="{token}">'
    if _META_RE.search(html):
        return _META_RE.sub(tag, html, count=1)
    if "<head>" in html:
        return html.replace("<head>", f"<head>\n{tag}", 1)
    return tag + html


def _json_response(status: int, body: dict[str, Any]) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    payload = json.dumps(body).encode("utf-8")
    return status, [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                    (b"cache-control", b"no-store")], payload


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, *, token: str, get_port: Callable[[], int | None] | None = None,
                 importmap: str | None = None) -> None:
        self.app = app
        self._token = token.encode("utf-8")
        self._get_port = get_port
        self._csp = content_security_policy(importmap).encode("ascii")

    def _expected_origin(self, scope: Scope) -> str:
        port = self._get_port() if self._get_port else None
        if port is None:
            server = scope.get("server")
            port = server[1] if server else 80
        return f"http://127.0.0.1:{port}"

    async def _reject(self, send: Send, status: int, error: str) -> None:
        status, headers, body = _json_response(status, {"error": error})
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if scope["method"] in UNSAFE_METHODS:
            supplied = headers.get(TOKEN_HEADER, b"")
            if not hmac.compare_digest(supplied, self._token):
                await self._reject(send, 403, "bad_token")
                return
            origin = headers.get(b"origin")
            if origin is not None and origin.decode("latin-1") != self._expected_origin(scope):
                await self._reject(send, 403, "bad_origin")
                return
        path: str = scope.get("path", "")
        csp = self._csp

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                names = {k.lower() for k, _ in raw}
                extra: list[tuple[bytes, bytes]] = []
                if b"x-content-type-options" not in names:
                    extra.append((b"x-content-type-options", b"nosniff"))
                if b"referrer-policy" not in names:
                    extra.append((b"referrer-policy", b"no-referrer"))
                content_type = next((v for k, v in raw if k.lower() == b"content-type"), b"")
                if content_type.lower().startswith(b"text/html") and b"content-security-policy" not in names:
                    extra.append((b"content-security-policy", csp))
                if path.startswith("/api") and b"cache-control" not in names:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": raw + extra}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def install(app: Any, token: str, importmap_sha: str | None = None, *, get_port: Callable[[], int | None] | None = None) -> None:
    """Add the Host check and the token/Origin/header middleware to a FastAPI/Starlette app."""
    app.add_middleware(SecurityMiddleware, token=token, get_port=get_port, importmap=importmap_sha)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)   # added last = outermost = checked first
