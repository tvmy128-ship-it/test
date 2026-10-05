"""API keys (APP_SPEC §14.2).

Storage order when *saving*: Windows Credential Manager through ``keyring`` (service ``DuoSkinStudio``, one entry per
provider); if keyring fails, a DPAPI-encrypted file ``DATA\\secrets.dpapi`` (current-user scope). On a non-Windows
development machine the last resort is ``DATA/secrets.dev.json`` (mode 0600, **not encrypted**; labelled ``dev_file``).

Reading: an environment variable (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``TRIPO_API_KEY``, ``RECRAFT_API_TOKEN``,
``GEMINI_API_KEY``, ``FAL_KEY``) wins over both stores (Settings shows "from environment"), then keyring, then the file.

Hygiene: a secret longer than 1280 characters is refused; ``key_status()`` returns masked tails only (``sk-...abcd``);
every key that is read or written is registered with the log redactor so it can never appear in a log line.

Module-level API (APP_SPEC §5.4): ``get_key``, ``set_key``, ``delete_key``, ``key_status``. They delegate to a default
``KeyStore`` (the running app's store, or one built from ``config.paths()``).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from duoskin import winplat
from duoskin.config import DataPaths
from duoskin.logsetup import register_secret
from duoskin.models.common import Strict, UtcDatetime, utcnow

log = logging.getLogger("duoskin.keystore")

SERVICE = "DuoSkinStudio"
PROVIDERS = ("anthropic", "openai", "tripo", "recraft", "gemini", "fal")
ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "tripo": "TRIPO_API_KEY",
    "recraft": "RECRAFT_API_TOKEN", "gemini": "GEMINI_API_KEY", "fal": "FAL_KEY",
}
REQUIREMENT: dict[str, Literal["required", "recommended", "optional"]] = {
    "anthropic": "required", "openai": "required", "tripo": "required", "recraft": "recommended",
    "gemini": "optional", "fal": "optional",
}
MAX_KEY_LEN = 1280
_PREFIXES = ("sk-", "tsk_", "AIza")

KeySource = Literal["environment", "keyring", "dpapi_file", "dev_file"]


class KeyRejected(ValueError):
    """The value or provider name cannot be stored (empty, too long, control characters, unknown provider)."""


class KeyTestResult(Strict):
    ok: bool | None = None
    at: UtcDatetime
    message: str = ""


class KeyStatus(Strict):
    """What the UI may see about a key. The value itself never leaves the backend."""

    provider: str
    set: bool
    source: KeySource | None = None
    masked: str | None = None
    requirement: Literal["required", "recommended", "optional"]
    store: str                                      # where a newly saved key would go
    env_var: str
    last_test: KeyTestResult | None = None


def mask_key(value: str) -> str:
    """``sk-ant-api03-...-wxyz`` -> ``sk-...wxyz``. Short secrets reveal nothing but a dot."""
    if len(value) < 12:
        return "..."
    prefix = next((p for p in _PREFIXES if value.startswith(p)), "")
    return f"{prefix}…{value[-4:]}"


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise KeyRejected(f"unknown provider '{provider}'")


def _clean(value: str) -> str:
    if not isinstance(value, str):
        raise KeyRejected("the key must be text")
    value = value.strip()
    if not value:
        raise KeyRejected("the key is empty")
    if len(value) > MAX_KEY_LEN:
        raise KeyRejected(f"the key is longer than {MAX_KEY_LEN} characters (Credential Manager limit)")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise KeyRejected("the key contains control characters or a line break")
    return value


class _FileBackend:
    """A JSON file of ``provider -> base64(protect(value))``."""

    def __init__(self, path: Path, protect: Callable[[bytes], bytes], unprotect: Callable[[bytes], bytes], label: KeySource,
                 secure_mode: int | None = None) -> None:
        self.path = path
        self._protect = protect
        self._unprotect = unprotect
        self.label: KeySource = label
        self._mode = secure_mode

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data.get("entries", {}) if isinstance(data, dict) else {}
            return {str(k): str(v) for k, v in entries.items()}
        except (OSError, ValueError):
            return {}

    def _save(self, entries: dict[str, str]) -> None:
        payload = json.dumps({"version": 1, "entries": entries}, indent=2).encode("utf-8")
        winplat.atomic_write(self.path, payload, mode=self._mode)

    def get(self, provider: str) -> str | None:
        blob = self._load().get(provider)
        if not blob:
            return None
        try:
            return self._unprotect(base64.b64decode(blob)).decode("utf-8")
        except Exception:  # noqa: BLE001
            log.warning("could not decrypt the stored %s key", provider)
            return None

    def set(self, provider: str, value: str) -> None:
        entries = self._load()
        entries[provider] = base64.b64encode(self._protect(value.encode("utf-8"))).decode("ascii")
        self._save(entries)

    def delete(self, provider: str) -> bool:
        entries = self._load()
        if provider not in entries:
            return False
        del entries[provider]
        self._save(entries)
        return True


def _plain(b: bytes) -> bytes:
    return b


class KeyStore:
    """One key store bound to a DATA folder. ``keyring_module=None`` auto-detects; pass ``False`` to disable keyring."""

    def __init__(self, paths: DataPaths, *, keyring_module: Any = None, environ: Mapping[str, str] | None = None,
                 file_backend: _FileBackend | None = None) -> None:
        self._paths = paths
        self._env: Mapping[str, str] = os.environ if environ is None else environ
        self._lock = threading.RLock()
        self._keyring = self._detect_keyring(keyring_module)
        if file_backend is not None:
            self._file = file_backend
        elif winplat.IS_WINDOWS:
            self._file = _FileBackend(paths.secrets_dpapi, winplat.dpapi_protect, winplat.dpapi_unprotect, "dpapi_file")
        else:
            self._file = _FileBackend(paths.secrets_dev, _plain, _plain, "dev_file", secure_mode=0o600)

    @staticmethod
    def _detect_keyring(module: Any) -> Any | None:
        if module is False:
            return None
        if module is not None:
            return module
        try:
            import keyring
            from keyring.backends import fail

            backend = keyring.get_keyring()
            if isinstance(backend, fail.Keyring):
                return None
            return keyring
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------------------------------------------- reading
    def _read_stored(self, provider: str) -> tuple[str | None, KeySource | None]:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE, provider)
                if value:
                    return value, "keyring"
            except Exception:  # noqa: BLE001
                log.warning("keyring read failed for %s", provider)
        value = self._file.get(provider)
        if value:
            return value, self._file.label
        return None, None

    def _resolve(self, provider: str) -> tuple[str | None, KeySource | None]:
        _check_provider(provider)
        env_value = (self._env.get(ENV_VARS[provider]) or "").strip()
        if env_value:
            return env_value, "environment"
        return self._read_stored(provider)

    def get_key(self, provider: str) -> str | None:
        with self._lock:
            value, _source = self._resolve(provider)
        register_secret(value)
        return value

    # ------------------------------------------------------------------------------------------------------- writing
    def set_key(self, provider: str, value: str) -> KeySource:
        """Store a key (keyring, else the DPAPI/dev file). Returns where it went. Raises ``KeyRejected``."""
        _check_provider(provider)
        value = _clean(value)
        register_secret(value)
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.set_password(SERVICE, provider, value)
                    self._file.delete(provider)   # never leave a stale copy in the fallback file
                    return "keyring"
                except Exception:  # noqa: BLE001
                    log.warning("keyring write failed for %s; using the %s", provider, self._file.label)
            self._file.set(provider, value)
            return self._file.label

    def delete_key(self, provider: str) -> None:
        _check_provider(provider)
        with self._lock:
            old, _src = self._read_stored(provider)
            if self._keyring is not None:
                try:
                    self._keyring.delete_password(SERVICE, provider)
                except Exception:  # noqa: BLE001, S110
                    pass
            self._file.delete(provider)
        register_secret(old)   # keep masking a deleted key for the rest of the process

    # ------------------------------------------------------------------------------------------------------- status
    def _tests_path(self) -> Path:
        return self._paths.run_dir / "key_tests.json"

    def _load_tests(self) -> dict[str, Any]:
        try:
            data = json.loads(self._tests_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def record_test(self, provider: str, ok: bool | None, message: str = "") -> KeyTestResult:
        _check_provider(provider)
        result = KeyTestResult(ok=ok, at=utcnow(), message=message[:300])
        with self._lock:
            tests = self._load_tests()
            tests[provider] = json.loads(result.model_dump_json())
            winplat.atomic_write(self._tests_path(), json.dumps(tests, indent=2).encode("utf-8"))
        return result

    def status_of(self, provider: str) -> KeyStatus:
        with self._lock:
            value, source = self._resolve(provider)
            tests = self._load_tests()
        register_secret(value)
        last = None
        if provider in tests:
            try:
                last = KeyTestResult.model_validate(tests[provider])
            except ValueError:
                last = None
        store = "keyring" if self._keyring is not None else self._file.label
        return KeyStatus(provider=provider, set=value is not None, source=source,
                         masked=mask_key(value) if value else None, requirement=REQUIREMENT[provider], store=store,
                         env_var=ENV_VARS[provider], last_test=last)

    def key_status(self) -> dict[str, KeyStatus]:
        return {p: self.status_of(p) for p in PROVIDERS}

    def has_key(self, provider: str) -> bool:
        return self.get_key(provider) is not None

    def stored_values(self) -> dict[str, str]:
        """Every key currently resolvable, for the RedactFilter self-test. Callers must never log the result."""
        out: dict[str, str] = {}
        for p in PROVIDERS:
            v = self.get_key(p)
            if v:
                out[p] = v
        return out


# ------------------------------------------------------------------------------------------------------ default store
_default: dict[str, KeyStore | None] = {"store": None}


def configure(store: KeyStore | None) -> None:
    _default["store"] = store


def default_store() -> KeyStore:
    store = _default["store"]
    if store is None:
        from duoskin import config

        store = KeyStore(config.paths())
        _default["store"] = store
    return store


def get_key(provider: str) -> str | None:
    return default_store().get_key(provider)


def set_key(provider: str, value: str) -> None:
    default_store().set_key(provider, value)


def delete_key(provider: str) -> None:
    default_store().delete_key(provider)


def key_status() -> dict[str, KeyStatus]:
    return default_store().key_status()
