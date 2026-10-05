"""Shared base types (APP_SPEC §6.1)."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")]
PartId = Annotated[
    str,
    Field(pattern=r"^(duo|(a|b)\.(face|hair|shirt|pants|colours|acc\.[0-9]|print\.(top|bottom|shoes|charm)\.[0-9]))$"),
]
CharKey = Literal["a", "b"]
Provider = Literal["anthropic", "openai", "recraft", "tripo", "gemini", "fal"]
ProviderMode = Literal["real", "mock", "disabled"]


def canonical_json(obj: Any) -> bytes:
    """Stable JSON bytes used for every hash and cache key."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(obj if isinstance(obj, bytes) else canonical_json(obj)).hexdigest()


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    """Prefixed, time-sortable id (prj_, spc_, job_, stp_, gat_, dec_, chg_, cst_, lbl_)."""
    ms = int(time.time() * 1000)
    t = ""
    for _ in range(10):
        t = _ULID_ALPHABET[ms & 31] + t
        ms >>= 5
    r = "".join(secrets.choice(_ULID_ALPHABET) for _ in range(16))
    return f"{prefix}_{t}{r}".lower()


# ---- time helpers (additive, added by the foundation track; APP_SPEC §6.1: times are UTC with a timezone) ----

def utcnow() -> datetime:
    return datetime.now(UTC)


def _to_utc(v: datetime) -> datetime:
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]


def iso_utc(dt: datetime) -> str:
    """Fixed-width UTC timestamp (``2026-10-05T12:00:00.000000Z``): sorts lexicographically, so SQL can compare it."""
    return _to_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(text: str) -> datetime:
    return _to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
