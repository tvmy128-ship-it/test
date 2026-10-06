"""fal: a settings slot and an adapter stub only (APP_SPEC 7.7, bible D23).

No pipeline step calls fal. Settings shows the key as "stored, unused". Every method raises a
``ProviderError`` (``kind="other"``, ``code="not_implemented"``) so a stray call can never be mistaken for a working provider.
"""
from __future__ import annotations

from typing import Any, NoReturn

from duoskin.providers.base import ProviderError

PROVIDER = "fal"


class FalProvider:
    """Stub. ``status()`` is the only thing that works: it tells Settings the key is stored but unused."""

    name = PROVIDER
    implemented = False

    def __init__(self, api_key: str | None = None, **_: Any) -> None:
        self._has_key = bool(api_key)

    def __repr__(self) -> str:
        return "FalProvider(stub)"

    def status(self) -> dict[str, Any]:
        return {"provider": PROVIDER, "implemented": False, "key_stored": self._has_key, "label": "stored, unused"}

    def test_key(self) -> dict[str, Any]:
        return {"ok": None, "message": "fal is stored but unused: no step calls it, so there is nothing to test."}

    def _nope(self) -> NoReturn:
        raise ProviderError(PROVIDER, "other", "fal is a settings slot only; no step calls it", code="not_implemented", billed="no",
                            user_hint="fal is not used by any step yet. Nothing was sent.")

    def generate(self, *_: Any, **__: Any) -> NoReturn:
        self._nope()

    def edit(self, *_: Any, **__: Any) -> NoReturn:
        self._nope()

    def call(self, *_: Any, **__: Any) -> NoReturn:
        self._nope()


__all__ = ["FalProvider"]
