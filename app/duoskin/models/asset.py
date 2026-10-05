"""``Asset``, ``AssetLink`` and ``Provenance`` (APP_SPEC §6.7)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from duoskin.models.common import PartId, Sha256, Strict, UtcDatetime

AssetKind = Literal["png", "jpeg", "webp", "svg", "glb", "gltf", "bin", "fbx", "obj_zip", "blend", "json", "txt",
                    "luau", "zip", "npz", "html"]


class Provenance(Strict):
    source: Literal["openai", "recraft", "tripo_api", "tripo_manual", "gemini", "anthropic", "code", "kit",
                    "user", "human_polish", "mock"]
    stream: Literal["pipeline", "drill", "regression", "showcase"] = "pipeline"   # data-stream separation
    step_id: str | None = None
    step_kind: str | None = None
    handler_version: int | None = None
    provider: str | None = None
    model: str | None = None                       # requested snapshot
    served_model: str | None = None                # the model that answered (Claude fallback) or the API echo
    prompt_id: str | None = None
    prompt_version: int | None = None
    prompt_sha256: Sha256 | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    input_shas: list[Sha256] = Field(default_factory=list)     # ordered; images hashed on decoded RGBA pixels
    mask_sha: Sha256 | None = None
    nonce: str = ""
    request_id: str | None = None                  # OpenAI r._request_id, Anthropic request id, Recraft image_id
    remote_task_id: str | None = None              # Tripo task_id
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    cost_basis: Literal["estimate", "usage", "credits"] | None = None
    raw_sha256: Sha256 | None = None               # untouched provider bytes (C2PA / SynthID kept)
    check_ids: list[str] = Field(default_factory=list)
    license: str = "n/a"
    notes: list[str] = Field(default_factory=list)
    created_at: UtcDatetime


class Asset(Strict):
    sha256: Sha256                                 # of the file bytes
    pixel_sha: Sha256 | None = None                # images: sha of size + decoded RGBA pixels (cache identity)
    kind: AssetKind
    mime: str
    bytes: int
    width: int | None = None
    height: int | None = None
    tris: int | None = None                        # meshes, after triangulation
    first_provenance: Provenance


class AssetLink(Strict):
    id: str
    asset_sha: Sha256
    project_id: str | None = None
    part_id: PartId | None = None
    step_id: str | None = None
    role: str      # "draft", "final", "concept_of_record", "view_left", "mesh_raw", "mesh_repaired", ...
    status: Literal["candidate", "rejected", "chosen", "final", "approved", "superseded", "export"] = "candidate"
    rank: int | None = None
    provenance: Provenance                          # provenance in this context (may differ from first_provenance)
