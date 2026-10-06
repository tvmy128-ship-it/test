"""External API clients and their mocks (APP_SPEC section 7).

Pipeline and engine code gets adapters only through ``duoskin.providers.registry.get(provider)``; it never imports an SDK. Common types
(``ProviderError``, ``CallCtx``, ``RateLimiter``, ``Cancelled``) live in ``duoskin.providers.base``. Importing this package is cheap: no
adapter, SDK or mock module is imported until it is used.
"""
from duoskin.providers.base import CallCtx, Cancelled, ProviderError, RateLimiter

__all__ = ["CallCtx", "Cancelled", "ProviderError", "RateLimiter"]
