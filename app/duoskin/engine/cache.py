"""Step cache and cache keys (APP_SPEC §8.5, ENG-04).

``cache_key`` follows the spec exactly: step kind, handler version, provider, model snapshot, prompt id + version +
compiled-text hash, schema hash, all params, the ordered input pixel hashes, the mask hash, the kit entries used, the
house-style and style-guide versions, threshold and rule versions, request-shaping capability flags and the nonce.
Handlers supply the model/prompt/kit/version fields through ``cache_fields(p, inputs)``. A property test asserts that
changing any one field changes the key (CHK-P07).

``Reimagine`` = a new ``step.nonce``: identical requests never pay twice, but the user can always force a new result.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from duoskin.db.db import Database
from duoskin.models.common import iso_utc, sha256_of, utcnow

if TYPE_CHECKING:
    from duoskin.engine.cas import Cas
    from duoskin.engine.registry import StepHandler
    from duoskin.models.asset import Asset
    from duoskin.models.job import Step

CACHE_FIELD_NAMES = (
    "model", "prompt_id", "prompt_version", "prompt_sha256", "schema_hash", "mask_pixel_sha", "kit_subset_sha",
    "house_style_version", "style_guide_version", "thresholds_version", "rules_version", "capability_flags",
)


def pixel_sha(png_bytes: bytes) -> str:
    """Image identity for cache keys: decoded RGBA pixels + size, so metadata-only changes do not bust the cache."""
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as im:
        im.load()
        rgba = im.convert("RGBA")
        return hashlib.sha256(f"{rgba.width}x{rgba.height}".encode() + rgba.tobytes()).hexdigest()


def cache_key(step: Step, handler: StepHandler, p: BaseModel | dict[str, Any], inputs: list[Asset]) -> str:
    extra = handler.cache_fields(p, inputs) or {}
    params = p.model_dump(mode="json") if isinstance(p, BaseModel) else p
    return sha256_of({
        "kind": handler.kind, "handler_version": handler.version,
        "provider": handler.provider,
        "model": extra.get("model"),
        "prompt_id": extra.get("prompt_id"), "prompt_version": extra.get("prompt_version"),
        "prompt_sha256": extra.get("prompt_sha256"),
        "schema_hash": extra.get("schema_hash"),
        "params": params,
        "inputs": [a.pixel_sha or a.sha256 for a in inputs],
        "mask": extra.get("mask_pixel_sha"),
        "kit_subset_sha": extra.get("kit_subset_sha"),
        "house_style_version": extra.get("house_style_version"),
        "style_guide_version": extra.get("style_guide_version"),
        "thresholds_version": extra.get("thresholds_version"),
        "rules_version": extra.get("rules_version"),
        "capabilities": extra.get("capability_flags"),
        "nonce": step.nonce,
    })


@dataclass
class CachedResult:
    step_kind: str
    outputs: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


class StepCache:
    """The ``cache`` table. A hit counts only when every output is still present in the CAS."""

    def __init__(self, db: Database, cas: Cas) -> None:
        self.db = db
        self.cas = cas

    def lookup(self, key: str) -> CachedResult | None:
        row = self.db.conn().execute("SELECT step_kind, outputs, result FROM cache WHERE cache_key=?", (key,)).fetchone()
        if row is None:
            return None
        outputs = json.loads(row["outputs"])
        if not all(self.cas.exists(sha) for sha in outputs):
            return None
        with self.db.tx() as c:
            c.execute("UPDATE cache SET last_hit_at=? WHERE cache_key=?", (iso_utc(utcnow()), key))
        return CachedResult(step_kind=row["step_kind"], outputs=outputs, result=json.loads(row["result"]))

    def store(self, key: str, result: CachedResult) -> None:
        now = iso_utc(utcnow())
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO cache (cache_key, step_kind, outputs, result, created_at, last_hit_at) VALUES (?,?,?,?,?,NULL)"
                " ON CONFLICT(cache_key) DO UPDATE SET step_kind=excluded.step_kind, outputs=excluded.outputs,"
                " result=excluded.result",
                (key, result.step_kind, json.dumps(result.outputs), json.dumps(result.result, ensure_ascii=False,
                                                                                 default=str), now))

    def drop(self, key: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM cache WHERE cache_key=?", (key,))

    def prune_missing(self) -> int:
        """Delete cache rows whose outputs left the CAS (after GC). Returns how many rows went."""
        gone: list[str] = []
        for row in self.db.conn().execute("SELECT cache_key, outputs FROM cache").fetchall():
            if not all(self.cas.exists(sha) for sha in json.loads(row["outputs"])):
                gone.append(row["cache_key"])
        if gone:
            with self.db.tx() as c:
                c.executemany("DELETE FROM cache WHERE cache_key=?", [(k,) for k in gone])
        return len(gone)
