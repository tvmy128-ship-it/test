"""Provider registry (APP_SPEC 7 and 16): ``get(provider)`` returns the real, the mock or a disabled adapter.

Mode lookup, first match wins:

1. ``DUOSKIN_PROVIDERS``: ``mock`` (every provider) or a list such as ``anthropic:real,tripo:mock``; a bare ``mock`` / ``real`` /
   ``disabled`` applies to the providers the list does not name. A typo raises ``ValueError``.
2. An injected ``mode_lookup(provider) -> "real" | "mock" | "disabled"`` (tests, the engine).
3. Settings: ``duoskin.config.load_settings().mode_of(provider)`` (demo mode forces mock), imported lazily. When the settings module
   or file cannot be read the answer is ``mock``: nothing can spend money by accident.

Keys come from ``duoskin.keystore.get_key(provider)`` (imported lazily at call time) or an injected ``key_provider(provider)``; a key is
read only when a real adapter is built and never stored by the registry (only a short hash of it, to notice a changed key). A real provider
without a key raises ``ProviderError(kind="auth", code="missing_key")``; ``real_available(provider)`` lets the pipeline route around it
(bible D22) before asking.

All adapters are synchronous (the engine uses worker threads). Real adapters share one ``CapabilityFlags`` (seeded from
``settings.capabilities``; changes are handed to ``flag_sink`` or, by default, written back to the settings file on a best-effort
basis) and one ``RateLimiter`` per provider (images-per-minute bucket and concurrency from the settings). Mock adapters are
singletons per registry, so a test can reach ``registry.get("openai").requests``.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

from duoskin.providers.base import CapabilityFlags, ProviderError, limiter_for

PROVIDERS = ("anthropic", "openai", "recraft", "tripo", "gemini", "fal")
MODES = ("real", "mock", "disabled")
ENV_VAR = "DUOSKIN_PROVIDERS"

ModeLookup = Callable[[str], str]
KeyProvider = Callable[[str], "str | None"]

_KEY_HINT = {
    "anthropic": "Add your Anthropic key in Settings, or switch to demo mode.",
    "openai": "Add your OpenAI key in Settings, or switch to demo mode.",
    "recraft": "Add your Recraft key in Settings; without it faces fall back to GPT Image parts.",
    "tripo": "Add your Tripo key in Settings; without it 3D models use the manual Tripo pack.",
    "gemini": "Add your Gemini key in Settings; without it the second opinion uses a second Claude juror.",
    "fal": "fal is stored but unused.",
}


def parse_env(value: str | None) -> tuple[str | None, dict[str, str]]:
    """Parse ``DUOSKIN_PROVIDERS``: (the mode for unnamed providers or ``None``, {provider: mode})."""
    default: str | None = None
    per: dict[str, str] = {}
    for raw in (value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" in item:
            prov, _, mode = item.partition(":")
            prov, mode = prov.strip().lower(), mode.strip().lower()
            if prov not in PROVIDERS:
                raise ValueError(f"{ENV_VAR}: unknown provider {prov!r} (known: {', '.join(PROVIDERS)})")
            if mode not in MODES:
                raise ValueError(f"{ENV_VAR}: unknown mode {mode!r} for {prov} (use real, mock or disabled)")
            per[prov] = mode
        else:
            mode = item.lower()
            if mode not in MODES:
                raise ValueError(f"{ENV_VAR}: unknown mode {item!r} (use real, mock or disabled, or provider:mode)")
            default = mode
    return default, per


class DisabledProvider:
    """What ``get`` returns for a provider whose mode is ``disabled``: every method raises a ``ProviderError``."""

    implemented = False

    def __init__(self, provider: str) -> None:
        self.name = provider

    def __repr__(self) -> str:
        return f"DisabledProvider({self.name})"

    def __getattr__(self, attr: str) -> Callable[..., Any]:
        if attr.startswith("_"):
            raise AttributeError(attr)

        def disabled(*_a: Any, **_k: Any) -> Any:
            raise ProviderError(self.name, "other", f"{self.name} is disabled in Settings", code="provider_disabled", billed="no",
                                user_hint=f"{self.name.capitalize()} is switched off in Settings (Providers). Nothing was sent.")
        return disabled


class ProviderRegistry:
    """One registry per process (``default_registry()``); tests build their own."""

    def __init__(self, *, mode_lookup: ModeLookup | None = None, key_provider: KeyProvider | None = None,
                 settings_loader: Callable[[], Any] | None = None, flags: CapabilityFlags | None = None,
                 flag_sink: Callable[[str, Any], None] | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 faults: Any = None, env: Mapping[str, str] | None = None) -> None:
        self._mode_lookup = mode_lookup
        self._key_provider = key_provider
        self._settings_loader = settings_loader
        self._flags = flags
        self._flag_sink = flag_sink
        self.cost_sink = cost_sink
        self._faults = faults
        self._env = env
        self._cache: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.RLock()

    # ----- configuration ------------------------------------------------------------------------------------
    def configure(self, **kw: Any) -> None:
        """Change ``mode_lookup``, ``key_provider``, ``settings_loader``, ``flag_sink``, ``cost_sink`` or ``faults`` and drop the cache."""
        with self._lock:
            for k, v in kw.items():
                if k not in ("mode_lookup", "key_provider", "settings_loader", "flag_sink", "cost_sink", "faults", "env"):
                    raise TypeError(f"unknown registry option {k!r}")
                setattr(self, "_" + k if k != "cost_sink" else k, v)
            self._cache.clear()

    def reset(self) -> None:
        """Forget every adapter (after a settings change or in tests). Flags and limiters are kept."""
        with self._lock:
            self._cache.clear()

    # ----- settings, keys, flags --------------------------------------------------------------------------------
    def settings(self) -> Any | None:
        """The current ``Settings`` or ``None`` when they cannot be loaded."""
        try:
            if self._settings_loader is not None:
                return self._settings_loader()
            from duoskin import config
            eff = getattr(config, "effective_settings", None)       # the running app's settings with demo/provider overrides
            return eff() if callable(eff) else config.load_settings()
        except Exception:  # noqa: BLE001 - config missing or unreadable: callers fall back to safe defaults
            return None

    def _env_value(self) -> str:
        return (self._env if self._env is not None else os.environ).get(ENV_VAR, "")

    def mode_of(self, provider: str) -> str:
        """``real`` | ``mock`` | ``disabled`` for ``provider`` (see the module docstring for the lookup order)."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        default, per = parse_env(self._env_value())
        if provider in per:
            return per[provider]
        if default is not None:
            return default
        if self._mode_lookup is not None:
            mode = str(self._mode_lookup(provider))
            if mode not in MODES:
                raise ValueError(f"mode lookup returned {mode!r} for {provider}")
            return mode
        s = self.settings()
        if s is None:
            return "disabled" if provider == "fal" else "mock"
        try:
            m = s.mode_of(provider)
            return str(getattr(m, "value", m))
        except Exception:  # noqa: BLE001
            return "mock"

    def key_for(self, provider: str) -> str | None:
        from duoskin.logsetup import register_secret

        if self._key_provider is not None:
            key = self._key_provider(provider)
            register_secret(key)          # an injected key source gets the same log/error masking as the keystore
            return key
        try:
            from duoskin.keystore import get_key
        except ImportError:
            return None
        try:
            return get_key(provider)
        except Exception:  # noqa: BLE001 - a keyring failure means "no key", never a crash
            return None

    def real_available(self, provider: str) -> bool:
        """Is a key stored for ``provider``? (The pipeline routes around a missing key before it asks for the adapter.)"""
        return bool(self.key_for(provider))

    @property
    def flags(self) -> CapabilityFlags:
        with self._lock:
            if self._flags is None:
                s = self.settings()
                initial = dict(getattr(s, "capabilities", {}) or {}) if s is not None else {}
                self._flags = CapabilityFlags(initial, on_change=self._persist_flag)
            return self._flags

    def _persist_flag(self, name: str, value: Any) -> None:
        if self._flag_sink is not None:
            self._flag_sink(name, value)
            return
        if not isinstance(value, (bool, str)):
            return
        try:                                       # best effort: write the flag into settings.capabilities
            from duoskin import config
            rt = config.active_runtime() if hasattr(config, "active_runtime") else None
            if rt is not None and hasattr(rt, "update_settings"):
                if rt.settings.capabilities.get(name) != value:
                    rt.update_settings({"capabilities": {name: value}})      # persists, drops the effective-settings cache
                return
            s = config.load_settings()
            caps = dict(s.capabilities)
            if caps.get(name) == value:
                return
            caps[name] = value
            config.save_settings(s.model_copy(update={"capabilities": caps}))
        except Exception:  # noqa: BLE001, S110 - never fail a provider call because the flag could not be saved
            pass

    # ----- the adapters --------------------------------------------------------------------------------------
    def get(self, provider: str) -> Any:
        """The adapter for ``provider`` in its current mode."""
        mode = self.mode_of(provider)
        if mode == "disabled":
            return DisabledProvider(provider)
        key = ""
        if mode == "real":
            k = self.key_for(provider)
            if not k:
                raise ProviderError(provider, "auth", f"no {provider} key is stored", code="missing_key", billed="no",
                                    user_hint=_KEY_HINT.get(provider, "Add the key in Settings."))
            key = hashlib.sha256(k.encode("utf-8")).hexdigest()[:12]
        with self._lock:
            ck = (provider, mode, key)
            adapter = self._cache.get(ck)
            if adapter is None:
                adapter = self._build(provider, mode, self.key_for(provider) if mode == "real" else None)
                self._cache[ck] = adapter
            self._apply_settings(provider, adapter)
            return adapter

    def _limits(self, provider: str) -> dict[str, Any]:
        s = self.settings()
        prov = getattr(s, "providers", None)
        out: dict[str, Any] = {}
        if prov is not None:
            try:
                out["concurrent"] = prov.concurrency_for(provider)
            except Exception:  # noqa: BLE001, S110
                pass
            if provider == "openai":
                out["ipm"] = getattr(prov, "openai_ipm", 5)
        return out

    def _billed(self) -> bool:
        s = self.settings()
        return bool(getattr(getattr(s, "providers", None), "gemini_key_billed", False))

    def _apply_settings(self, provider: str, adapter: Any) -> None:
        if getattr(adapter, "limiter", None) is not None and self.mode_of(provider) == "real":
            lim = self._limits(provider)
            if lim:
                limiter_for(provider, **lim)                     # updates the shared limiter in place
        if provider == "gemini" and hasattr(adapter, "key_billed"):
            adapter.key_billed = self._billed()

    def _build(self, provider: str, mode: str, key: str | None) -> Any:
        common: dict[str, Any] = {"flags": self.flags, "cost_sink": self.cost_sink}
        if mode == "mock":
            return self._build_mock(provider, common)
        lim = self._limits(provider)
        limiter = limiter_for(provider, **lim)
        assert key
        if provider == "anthropic":
            from duoskin.providers.anthropic_llm import AnthropicProvider
            routes = self._routes()
            return AnthropicProvider(key, routes=routes, limiter=limiter, **common)
        if provider == "openai":
            from duoskin.providers.openai_images import OpenAIImages
            return OpenAIImages(key, limiter=limiter, **common)
        if provider == "recraft":
            from duoskin.providers.recraft import RecraftProvider
            return RecraftProvider(key, limiter=limiter, **common)
        if provider == "tripo":
            from duoskin.providers.tripo import TripoApi
            return TripoApi(key, limiter=limiter, **common)
        if provider == "gemini":
            from duoskin.providers.gemini import GeminiProvider
            return GeminiProvider(key, limiter=limiter, key_billed=self._billed(), **common)
        from duoskin.providers.fal import FalProvider
        return FalProvider(key)

    def _routes(self) -> dict[str, Any] | None:
        s = self.settings()
        m = getattr(s, "models", None)
        if m is None:
            return None
        from duoskin.providers.anthropic_llm import build_routes
        return build_routes(planner=m.planner, critic=m.critic, judge=m.judge, checker=m.checker)

    def _build_mock(self, provider: str, common: dict[str, Any]) -> Any:
        faults = self._faults
        if provider == "anthropic":
            from duoskin.providers.mock.llm import MockLLM
            return MockLLM(routes=self._routes(), faults=faults, **common)
        if provider == "openai":
            from duoskin.providers.mock.images import MockImages
            return MockImages(faults=faults, ipm=self._limits("openai").get("ipm"), **common)
        if provider == "recraft":
            from duoskin.providers.mock.recraft import MockRecraft
            return MockRecraft(faults=faults, **common)
        if provider == "tripo":
            from duoskin.providers.mock.tripo import MockTripo
            return MockTripo(faults=faults, **common)
        if provider == "gemini":
            from duoskin.providers.mock.gemini import MockGemini
            return MockGemini(faults=faults, key_billed=self._billed(), **common)
        from duoskin.providers.fal import FalProvider
        return FalProvider(None)

    # ----- reporting -------------------------------------------------------------------------------------------
    def status(self) -> dict[str, dict[str, Any]]:
        """For Settings and ``doctor``: mode, whether a key is stored, and what ``get`` would do. Never contains a key."""
        out: dict[str, dict[str, Any]] = {}
        for p in PROVIDERS:
            try:
                mode = self.mode_of(p)
            except ValueError as e:
                out[p] = {"mode": "invalid", "error": str(e)}
                continue
            has_key = self.real_available(p)
            out[p] = {"mode": mode, "key_stored": has_key,
                      "effective": "mock" if mode == "mock" else "disabled" if mode == "disabled" else ("real" if has_key else "unavailable: no key")}
        return out


_DEFAULT: ProviderRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def default_registry() -> ProviderRegistry:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = ProviderRegistry()
        return _DEFAULT


def get(provider: str) -> Any:
    """The adapter for ``provider`` from the process-wide registry."""
    return default_registry().get(provider)


def configure(**kw: Any) -> None:
    default_registry().configure(**kw)


def reset() -> None:
    """Drop the process-wide registry (tests)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


def mode_of(provider: str) -> str:
    return default_registry().mode_of(provider)


def status() -> dict[str, dict[str, Any]]:
    return default_registry().status()


__all__ = [
    "ENV_VAR", "MODES", "PROVIDERS", "DisabledProvider", "ProviderRegistry", "configure", "default_registry", "get", "mode_of",
    "parse_env", "reset", "status",
]
