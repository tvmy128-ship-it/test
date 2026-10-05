"""Logging: JSON log lines, secret redaction, faulthandler and excepthooks (APP_SPEC §5.2, §14.2, CHK-S12).

``RedactFilter`` masks ``sk-...``, ``tsk_...``, ``AIza...``, ``Bearer ...``, ``x-api-key: ...``, signed-URL query strings and
the *exact value* of every key that was registered with ``register_secret`` (the keystore does this for every key it
reads or writes). Keys never reach a log line, the diagnostics zip or provenance.
"""
from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import re
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote, quote_plus

from duoskin.models.common import iso_utc, utcnow

REDACTED = "[REDACTED]"

_QUERY_SECRETS = (r"x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential|x-goog-api-key|"
                  r"signature|sig|token|access[_-]?token|refresh[_-]?token|id[_-]?token|key|api[_-]?key|apikey|secret|client[_-]?secret|"
                  r"password|passwd|auth|authorization|credential|policy|key-pair-id")
_HEADER_SECRETS = (r"x-api-key|x-goog-api-key|api[_-]?key|api[_-]?token|apikey|authorization|proxy-authorization|x-auth-token|"
                   r"x-access-token|access[_-]?token|refresh[_-]?token|client[_-]?secret|fal[_-]?key")
_BARE_SECRETS = r"token|secret|password|passwd"

_WS = r"[ \t]{0,8}"           # bounded: an unbounded \s* around an optional quote backtracks quadratically on a long run of spaces

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{6,}"),                                             # not the end of "risk-averse"
    re.compile(r"(?<![A-Za-z0-9])tsk_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),               # a JWT
    re.compile(r"(?i)\b(?:bearer|basic|digest)[ \t]{1,20}[A-Za-z0-9._\-~+/=]{6,}"),
    re.compile(rf"(?i)\b((?:set-)?cookie)({_WS}[\"']?{_WS}[:=]{_WS}[\"']?)[^\r\n\"'}}]+"),            # a cookie header: everything on its line
    re.compile(rf"(?i)({_HEADER_SECRETS})({_WS}[\"']?{_WS}[:=]{_WS}[\"']?)[^\s,\"'}}&]{{4,}}"),
    re.compile(rf"(?i)(?<![\w-])({_BARE_SECRETS})({_WS}[\"']?{_WS}[:=]{_WS}[\"']?)[^\s,\"'}}&]{{4,}}"),
    re.compile(rf"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){{0,8}}_(?:KEY|TOKEN|SECRET))({_WS}={_WS})[^\s,\"'}}&]{{4,}}"),   # OPENAI_API_KEY=..., RECRAFT_API_TOKEN=...
    re.compile(rf"(?i)([?&;](?:{_QUERY_SECRETS})=)[^&\s\"'<>]+"),                                  # signed-URL and ?key= query strings
    re.compile(r"(?i)(\bhttps?://)[^/\s:@\"']{1,200}:[^/\s@\"']{1,200}(?=@)"),                      # user:password@host
)
_SECRET_LOCK = threading.Lock()
_SECRETS: set[str] = set()
_MIN_SECRET_LEN = 6


def _variants(value: str) -> set[str]:
    """The ways one secret can show up in text: as is, URL-encoded, JSON-escaped, Python-repr-escaped."""
    out = {value}
    try:
        out.add(quote(value, safe=""))
        out.add(quote_plus(value))
        out.add(json.dumps(value)[1:-1])
        out.add(repr(value)[1:-1])
    except (TypeError, ValueError):
        pass
    return {v for v in out if len(v) >= _MIN_SECRET_LEN}


def register_secret(value: str | None) -> None:
    """Remember an exact secret value (and its URL-encoded / JSON-escaped forms) so it is masked wherever it appears."""
    if value and len(value) >= _MIN_SECRET_LEN:
        forms = _variants(value)
        with _SECRET_LOCK:
            _SECRETS.update(forms)


def forget_secret(value: str | None) -> None:
    if value:
        with _SECRET_LOCK:
            _SECRETS.difference_update(_variants(value))


def registered_secret_count() -> int:
    with _SECRET_LOCK:
        return len(_SECRETS)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with every secret masked."""
    if not text:
        return text
    with _SECRET_LOCK:
        secrets = set(_SECRETS)
    secrets.update(s for s in extra_secrets if s and len(s) >= _MIN_SECRET_LEN)
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_data(obj: Any, *, _depth: int = 0) -> Any:
    """``redact`` applied to every string (keys and values) inside JSON-like data (dict, list, tuple, str); other values pass through.
    Used where free text from exceptions or providers enters an event payload, a step row or a zip."""
    if _depth > 40:
        return obj
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {(redact(k) if isinstance(k, str) else k): redact_data(v, _depth=_depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_data(v, _depth=_depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_data(v, _depth=_depth + 1) for v in obj)
    return obj


def _scrub_record(record: logging.LogRecord) -> None:
    """Rewrite the record in place so that formatting it can never reveal a key (message, args, traceback, stack)."""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001
        message = str(record.msg)
    record.msg = redact(message)
    record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.exc_info = None
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


class RedactFilter(logging.Filter):
    """Rewrite the record so formatting it can never reveal a key (message, args, traceback, stack)."""

    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True


_factory_lock = threading.Lock()
_factory_installed = False


def install_record_factory() -> None:
    """Make every ``LogRecord`` born redacted, so no handler (the file, the console, ``logging.lastResort`` before ``setup_logging`` has
    run, a test's capture handler, a third-party handler) can ever see a key. Idempotent; the handler filters stay as a second layer."""
    global _factory_installed
    with _factory_lock:
        if _factory_installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            try:
                _scrub_record(record)
            except Exception:  # noqa: BLE001, S110 - logging must never raise
                pass
            return record

        logging.setLogRecordFactory(factory)
        _factory_installed = True


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso_utc(utcnow()), "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
            "thread": record.threadName,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")


class _NoWinError10054(logging.Filter):
    """Drop the harmless "ConnectionResetError: [WinError 10054]" noise asyncio logs when a browser tab closes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("asyncio"):
            return True
        text = record.getMessage() + (record.exc_text or "")
        if record.exc_info and record.exc_info[1] is not None:
            text += repr(record.exc_info[1])
        return "WinError 10054" not in text


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` that survives Windows file locking (SYS-14).

    Renaming ``duoskin.log`` fails with ``PermissionError`` (WinError 32) while antivirus, OneDrive or a second copy of the
    app (``doctor.bat`` while the app runs) has the file open. The rollover is then skipped and tried again a minute later;
    the record that triggered it is still written, and logging never raises.
    """

    retry_after_s = 60.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._not_before = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._not_before:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._not_before = time.monotonic() + self.retry_after_s
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    pass


class LogHandle:
    def __init__(self, handlers: list[logging.Handler], fault_file: TextIO | None, restore: list[Any]) -> None:
        self._handlers = handlers
        self._fault_file = fault_file
        self._restore = restore

    def shutdown(self) -> None:
        root = logging.getLogger()
        for h in self._handlers:
            root.removeHandler(h)
            h.close()
        self._handlers = []
        for fn in self._restore:
            fn()
        self._restore = []
        if self._fault_file is not None:
            try:
                faulthandler.disable()
                self._fault_file.close()
            except (OSError, ValueError):
                pass
            self._fault_file = None


_current: LogHandle | None = None
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic", "openai", "urllib3", "hpack", "h11", "h2")


def setup_logging(logs_dir: Path, *, level: int | str = logging.INFO, console: bool = True, dev: bool = False,
                  hooks: bool = True) -> LogHandle:
    """Install the file (JSON lines, rotating 5 x 5 MB) and optional console handlers on the root logger.

    Idempotent: a second call replaces the first call's handlers. SDK/HTTP libraries are held at WARNING because their
    DEBUG output includes request headers (never enable ``ANTHROPIC_LOG=debug``).
    """
    global _current
    install_record_factory()
    if _current is not None:
        _current.shutdown()
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if dev else level)
    handlers: list[logging.Handler] = []

    file_handler = SafeRotatingFileHandler(logs_dir / "duoskin.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    handlers.append(file_handler)
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(_ConsoleFormatter())
        handlers.append(stream)
    for h in handlers:
        h.addFilter(RedactFilter())
        h.addFilter(_NoWinError10054())
        root.addHandler(h)
    for noisy in NOISY_LOGGERS:                                       # their DEBUG/INFO lines carry full URLs (signed ones too) and headers
        logging.getLogger(noisy).setLevel(logging.WARNING)

    fault_file: TextIO | None = None
    try:
        fault_file = open(logs_dir / "faulthandler.log", "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=fault_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        fault_file = None

    restore: list[Any] = []
    if hooks:
        log = logging.getLogger("duoskin.crash")
        prev_excepthook, prev_thread_hook, prev_unraisable = sys.excepthook, threading.excepthook, sys.unraisablehook

        def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
            log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
            prev_excepthook(exc_type, exc, tb)

        def _thread_hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_type is SystemExit:
                return
            log.critical("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        def _unraisable(args: Any) -> None:
            log.error("unraisable exception: %s", args.err_msg, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        sys.excepthook, threading.excepthook, sys.unraisablehook = _excepthook, _thread_hook, _unraisable
        restore.append(lambda: setattr(sys, "excepthook", prev_excepthook))
        restore.append(lambda: setattr(threading, "excepthook", prev_thread_hook))
        restore.append(lambda: setattr(sys, "unraisablehook", prev_unraisable))

    _current = LogHandle(handlers, fault_file, restore)
    return _current
