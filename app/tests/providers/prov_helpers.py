"""Shared helpers for the provider tests: scripted MockTransport handlers and SSE builders.

Every test that builds an adapter on a ``MockTransport`` ends with ``spy.assert_hit()`` (SYS-22: a test whose
transport was never reached tests nothing). No test here opens a socket.
"""
from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from typing import Any

from PIL import Image

from duoskin.providers._http import httpx


class Spy:
    """A MockTransport handler that replays scripted responses and records every request."""

    def __init__(self, *responses: Any, default: Any = None) -> None:
        self.script: list[Any] = list(responses)
        self.default = default
        self.requests: list[Any] = []
        self.bodies: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        body: Any = None
        ctype = request.headers.get("content-type", "")
        if request.content and "json" in ctype:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        self.bodies.append(body)
        if self.script:
            r = self.script.pop(0)
        elif self.default is not None:
            r = self.default
        else:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        if callable(r):
            r = r(request)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def transport(self) -> Any:
        return httpx.MockTransport(self)

    def assert_hit(self, n: int | None = None) -> None:
        assert self.requests, "the MockTransport handler was never hit (SYS-22)"
        if n is not None:
            assert len(self.requests) == n, f"expected {n} requests, saw {len(self.requests)}"


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Any:
    return httpx.Response(status, json=data, headers=headers or {})


def png_bytes(w: int = 64, h: int = 64, color: tuple[int, ...] = (200, 40, 40, 255), mode: str = "RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), color if mode == "RGBA" else color[:3]).save(buf, "PNG")
    return buf.getvalue()


def png_b64(*a: Any, **kw: Any) -> str:
    return base64.b64encode(png_bytes(*a, **kw)).decode("ascii")


# --------------------------------------------------------------------------------------------------------------
# Anthropic SSE
# --------------------------------------------------------------------------------------------------------------

def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def anthropic_stream(*, text: str | None = "{}", thinking: str = "", stop_reason: str = "end_turn", model: str = "claude-opus-5",
                     usage: dict[str, int] | None = None, stop_details: dict[str, Any] | None = None,
                     request_id: str = "req_test_1") -> Any:
    """A streaming /v1/messages response."""
    u = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    u.update(usage or {})
    ev: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {**u, "output_tokens": 1}}})]
    idx = 0
    if thinking:
        ev += [("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
               ("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "thinking_delta", "thinking": thinking}}),
               ("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "signature_delta", "signature": "sig"}}),
               ("content_block_stop", {"type": "content_block_stop", "index": idx})]
        idx += 1
    if text is not None:
        ev += [("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}),
               ("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}}),
               ("content_block_stop", {"type": "content_block_stop", "index": idx})]
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    ev += [("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": u["output_tokens"]}}),
           ("message_stop", {"type": "message_stop"})]
    return httpx.Response(200, content=_sse(ev), headers={"content-type": "text/event-stream", "request-id": request_id})


def anthropic_error(status: int, message: str, etype: str = "invalid_request_error", headers: dict[str, str] | None = None) -> Any:
    return httpx.Response(status, json={"type": "error", "error": {"type": etype, "message": message}},
                          headers={"request-id": "req_err_1", **(headers or {})})


def make_anthropic_client(handler: Callable[[Any], Any], **kw: Any) -> Any:
    """An ``anthropic.Anthropic`` whose transport is the scripted handler (SDK retries off unless asked)."""
    import anthropic
    import httpx2
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    return anthropic.Anthropic(api_key="sk-ant-test-0000", http_client=http, max_retries=kw.pop("max_retries", 0), **kw)


# --------------------------------------------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------------------------------------------

def openai_images_response(images: list[bytes], *, size: str = "1024x1024", usage: dict[str, Any] | None = None,
                           request_id: str = "req_oai_1", status: int = 200) -> Any:
    body: dict[str, Any] = {"created": 1, "data": [{"b64_json": base64.b64encode(b).decode("ascii")} for b in images],
                            "size": size, "output_format": "png", "quality": "low", "background": "opaque"}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(status, json=body, headers={"x-request-id": request_id})


def openai_error(status: int, message: str, code: str | None = None, param: str | None = None,
                 etype: str = "invalid_request_error", headers: dict[str, str] | None = None) -> Any:
    return httpx.Response(status, json={"error": {"message": message, "type": etype, "param": param, "code": code}},
                          headers={"x-request-id": "req_oai_err", **(headers or {})})


def make_openai_client(handler: Callable[[Any], Any], **kw: Any) -> Any:
    import httpx2
    import openai
    http = openai.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    return openai.OpenAI(api_key="sk-test-0000", http_client=http, max_retries=0, timeout=kw.pop("timeout", 30), **kw)


def noop_sleep(_s: float) -> None:
    return None


class FakeClock:
    """A clock whose ``sleep`` advances it instantly: pass ``now`` / ``sleep`` to a RateLimiter (and an adapter) so waits cost no time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s
