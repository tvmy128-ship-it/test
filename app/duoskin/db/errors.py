"""Errors shared by the DB layer and the API."""
from __future__ import annotations

from typing import Any


class NotFound(LookupError):
    """A row does not exist."""

    def __init__(self, what: str, key: str) -> None:
        super().__init__(f"{what} '{key}' not found")
        self.what = what
        self.key = key


class ConflictError(Exception):
    """Optimistic-lock failure (HTTP 409). ``current`` is the object as it is now, so the UI can refresh."""

    def __init__(self, message: str, current: Any = None, code: str = "conflict") -> None:
        super().__init__(message)
        self.current = current
        self.code = code


class SchemaTooNew(RuntimeError):
    """The database was written by a newer app version."""
