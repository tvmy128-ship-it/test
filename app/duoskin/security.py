"""Local-only hardening (APP_SPEC §13, SYS-02, ENG-11, CHK-S06/S07).

``install(app, token, importmap_sha)`` adds, in this order of authority:

1. ``TrustedHostMiddleware(["127.0.0.1", "localhost"])``: any other ``Host`` header (DNS rebinding) gets 400.
2. ``SecurityMiddleware`` (pure ASGI, so SSE streams are never buffered):
   * every POST/PUT/PATCH/DELETE must carry ``X-DuoSkin-Token`` equal to this launch's token, else 403
     ``{"error": "bad_token"}`` (the UI reloads on that);
   * when an ``Origin`` header is present it must be exactly ``http://127.0.0.1:<port>``, else 403 ``bad_origin``;
   * a request to ``/api`` or ``/cas`` that the browser marks ``Sec-Fetch-Site: cross-site`` (or ``same-site``: another port on this host)
     gets 403 ``cross_site``, GET included;
   * every response gets ``X-Content-Type-Options: nosniff``, ``Referrer-Policy: no-referrer`` and ``Cross-Origin-Resource-Policy:
     same-origin``; HTML responses also get the Content-Security-Policy, ``X-Frame-Options: DENY`` and ``Cross-Origin-Opener-Policy``;
     ``/api`` responses are ``Cache-Control: no-store``.
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
MAX_BODY_BYTES = 96 * 1024 * 1024                              # a request body above this is refused on its Content-Length (uploads are capped at 50 MB each)
CROSS_SITE = frozenset({b"cross-site", b"same-site"})        # Sec-Fetch-Site values of a request that did not come from our own origin
META_NAME = "duoskin-token"
TOKEN_PLACEHOLDER = "__DUOSKIN_TOKEN__"

_IMPORTMAP_RE = re.compile(r"<script[^>]*type=[\"']importmap[\"'][^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_META_RE = re.compile(r"<meta\s+name=[\"']duoskin-token[\"']\s+content=[\"'][^\"']*[\"']\s*/?>", re.IGNORECASE)


_SECRET_ENV = re.compile(r"(?i)(api_?key|_key$|^key$|token|secret|password|passwd|credential|private_?key)")


def child_env(extra: dict[str, str] | None = None, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a child process (the mesh worker, Blender, a doctor probe): everything the parent has **except** secrets. The
    children parse untrusted files (a hostile model could exploit a native parser); they never call a provider, so they get no key: not the
    ``ANTHROPIC_API_KEY`` family the key store reads, and not any other variable whose name says key, token, secret or password."""
    import os

    from duoskin.keystore import ENV_VARS

    named = {v.upper() for v in ENV_VARS.values()}
    env = {k: v for k, v in (os.environ if base is None else base).items() if k.upper() not in named and not _SECRET_ENV.search(k)}
    if extra:
        env.update(extra)
    return env


#: No image the app decodes may have more pixels than this (a 4 MB PNG of zeros can claim 100 000 x 100 000). Pillow's own limit (89 MP) only warns.
MAX_IMAGE_PIXELS = 64_000_000


def apply_image_limits() -> None:
    """Lower Pillow's decompression-bomb limit to ``MAX_IMAGE_PIXELS`` (it raises ``DecompressionBombError`` above twice that). Idempotent."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def image_pixels(data: bytes) -> int | None:
    """Width x height read from the header only (no pixel decoding), or None when Pillow cannot identify the data."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            return int(im.width) * int(im.height)
    except Exception:  # noqa: BLE001 - not an image, or a damaged header: the caller's own decode reports it
        return None


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
            "; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


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
        path = scope.get("path", "")
        if (path.startswith(("/api/", "/cas/")) or path == "/api") and headers.get(b"sec-fetch-site", b"") in CROSS_SITE:
            # A page of another site (or another port on this host) is making the browser call us. The browser would hide the answer from it
            # (no CORS), but the request itself must not even be served: no timing probe, no stream held open, no side effect.
            await self._reject(send, 403, "cross_site")
            return
        if scope["method"] in UNSAFE_METHODS:
            declared = headers.get(b"content-length", b"")
            if declared.isdigit() and int(declared) > MAX_BODY_BYTES:       # the biggest legitimate upload is 50 MB; refuse before anything is buffered
                await self._reject(send, 413, "too_large")
                return
            supplied = headers.get(TOKEN_HEADER, b"")
            if not hmac.compare_digest(supplied, self._token):
                await self._reject(send, 403, "bad_token")
                return
            origin = headers.get(b"origin")
            if origin is not None and origin.decode("latin-1") != self._expected_origin(scope):
                await self._reject(send, 403, "bad_origin")
                return
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
                if b"cross-origin-resource-policy" not in names:
                    # Another site's <script>/<img>/<link> cannot load any of our files: no cheap "is DuoSkin running on this port" probe.
                    extra.append((b"cross-origin-resource-policy", b"same-origin"))
                content_type = next((v for k, v in raw if k.lower() == b"content-type"), b"")
                if content_type.lower().startswith(b"text/html"):
                    if b"content-security-policy" not in names:
                        extra.append((b"content-security-policy", csp))
                    if b"x-frame-options" not in names:
                        extra.append((b"x-frame-options", b"DENY"))
                    if b"cross-origin-opener-policy" not in names:
                        extra.append((b"cross-origin-opener-policy", b"same-origin"))
                if path.startswith("/api") and b"cache-control" not in names:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": raw + extra}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def install(app: Any, token: str, importmap_sha: str | None = None, *, get_port: Callable[[], int | None] | None = None) -> None:
    """Add the Host check and the token/Origin/header middleware to a FastAPI/Starlette app."""
    app.add_middleware(SecurityMiddleware, token=token, get_port=get_port, importmap=importmap_sha)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)   # added last = outermost = checked first
