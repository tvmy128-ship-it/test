"""Content-addressed store (APP_SPEC §6.7, §8.5).

Every asset is one immutable file at ``cas/<sha[:2]>/<sha256>.<ext>``, written once through a temp file and
``os.replace`` (retried for up to 2 s on ``PermissionError``). The ``assets`` table holds the facts about the bytes
(kind, mime, size, image size, ``pixel_sha``); ``asset_links`` say what a file is *for* (project, part, step, role).

Public interface (APP_SPEC §5.4)::

    cas.put(data, ext, *, link=None, prov) -> Asset
    cas.path(sha, ext=None) -> Path
    cas.get(sha) -> bytes
    pixel_sha(png_bytes) -> str          # re-exported from engine.cache
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path
from typing import Any

from duoskin import winplat
from duoskin.db.db import Database
from duoskin.db.errors import NotFound
from duoskin.engine.cache import pixel_sha
from duoskin.models.asset import Asset, AssetKind, AssetLink, Provenance
from duoskin.models.common import iso_utc, new_id, utcnow
from duoskin.security import MAX_IMAGE_PIXELS, apply_image_limits

log = logging.getLogger("duoskin.cas")

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EXT_BY_KIND: dict[str, str] = {
    "png": "png", "jpeg": "jpg", "webp": "webp", "svg": "svg", "glb": "glb", "gltf": "gltf", "bin": "bin", "fbx": "fbx",
    "obj_zip": "zip", "blend": "blend", "json": "json", "txt": "txt", "luau": "luau", "zip": "zip", "npz": "npz",
    "html": "html",
}
_KIND_BY_EXT: dict[str, str] = {**{k: k for k in EXT_BY_KIND}, "jpg": "jpeg", "jpeg": "jpeg"}
MIME_BY_KIND: dict[str, str] = {
    "png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml", "glb": "model/gltf-binary",
    "gltf": "model/gltf+json", "bin": "application/octet-stream", "fbx": "application/octet-stream",
    "obj_zip": "application/zip", "blend": "application/octet-stream", "json": "application/json",
    "txt": "text/plain; charset=utf-8", "luau": "text/plain; charset=utf-8", "zip": "application/zip",
    "npz": "application/octet-stream", "html": "text/html; charset=utf-8",
}
_IMAGE_KINDS = ("png", "jpeg", "webp")


class CasError(ValueError):
    pass


def kind_for_ext(ext: str) -> AssetKind:
    key = ext.lower().lstrip(".")
    kind = _KIND_BY_EXT.get(key)
    if kind is None:
        raise CasError(f"unsupported asset type '{ext}'")
    return kind   # type: ignore[return-value]


def is_sha(value: str) -> bool:
    return bool(SHA_RE.match(value))


def make_prov(source: str, **kw: Any) -> Provenance:
    """A ``Provenance`` with ``created_at`` filled: ``make_prov("code", step_kind="clothing.compose")``."""
    return Provenance(source=source, created_at=kw.pop("created_at", utcnow()), **kw)   # type: ignore[arg-type]


class Cas:
    def __init__(self, root: Path, db: Database) -> None:
        self.root = root
        self.db = db
        apply_image_limits()

    # ------------------------------------------------------------------------------------------------ paths
    def _file(self, sha: str, ext: str) -> Path:
        return self.root / sha[:2] / f"{sha}.{ext}"

    def path(self, sha: str, ext: str | None = None) -> Path:
        """Where the file for ``sha`` lives (it may not exist). Looks the extension up in the DB unless given."""
        if not is_sha(sha):
            raise CasError("not a sha256")
        if ext is None:
            row = self.db.conn().execute("SELECT kind FROM assets WHERE sha256=?", (sha,)).fetchone()
            if row is None:
                raise NotFound("asset", sha)
            ext = EXT_BY_KIND[row["kind"]]
        return self._file(sha, ext.lower().lstrip("."))

    def exists(self, sha: str) -> bool:
        if not is_sha(sha):
            return False
        row = self.db.conn().execute("SELECT kind FROM assets WHERE sha256=?", (sha,)).fetchone()
        return row is not None and self._file(sha, EXT_BY_KIND[row["kind"]]).exists()

    # ------------------------------------------------------------------------------------------------ writes
    def put(self, data: bytes, ext: str, *, link: AssetLink | None = None, prov: Provenance) -> Asset:
        """Store ``data`` (idempotent: the same bytes give the same asset). Records ``link`` when given."""
        if not isinstance(data, (bytes, bytearray)):
            raise CasError("asset data must be bytes")
        data = bytes(data)
        kind = kind_for_ext(ext)
        sha = hashlib.sha256(data).hexdigest()
        target = self._file(sha, EXT_BY_KIND[kind])
        problems = winplat.lint_path(target)
        if problems:
            raise CasError("; ".join(problems))
        asset = self._find(sha)
        if asset is None:
            width, height, px = self._image_facts(data, kind)
            if not target.exists() or target.stat().st_size != len(data):
                winplat.atomic_write(target, data)
            asset = Asset(sha256=sha, pixel_sha=px, kind=kind, mime=MIME_BY_KIND[kind], bytes=len(data), width=width,
                          height=height, first_provenance=prov)
            with self.db.tx() as c:
                c.execute(
                    "INSERT OR IGNORE INTO assets (sha256, pixel_sha, kind, mime, bytes, width, height, tris, json, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sha, px, kind, asset.mime, len(data), width, height, None, asset.model_dump_json(),
                     iso_utc(utcnow())))
            asset = self._find(sha) or asset
        elif not target.exists():
            winplat.atomic_write(target, data)    # the row survived but the file was removed: heal it
        if link is not None:
            self.add_link(link.model_copy(update={"asset_sha": sha}))
        return asset

    @staticmethod
    def _image_facts(data: bytes, kind: str) -> tuple[int | None, int | None, str | None]:
        if kind not in _IMAGE_KINDS:
            return None, None, None
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > MAX_IMAGE_PIXELS:          # the header says so: refuse before one pixel is decoded
                    raise CasError(f"the image has {im.width * im.height:,} pixels; the limit is {MAX_IMAGE_PIXELS:,}")
                im.load()
                return im.width, im.height, pixel_sha(data)
        except CasError:
            raise
        except Exception as exc:
            raise CasError(f"not a valid {kind} image: {type(exc).__name__}") from exc

    def add_link(self, link: AssetLink) -> AssetLink:
        if not link.id:
            link = link.model_copy(update={"id": new_id("lnk")})
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO asset_links (id, asset_sha, project_id, part_id, step_id, role, status, stream, rank, json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (link.id, link.asset_sha, link.project_id, link.part_id, link.step_id, link.role, link.status,
                 link.provenance.stream, link.rank, link.model_dump_json(), iso_utc(utcnow())))
        return link

    def update_meta(self, sha: str, *, tris: int | None = None) -> Asset:
        """Record facts learned after storing (a mesh's triangle count)."""
        with self.db.tx() as c:
            asset = self.get_asset(sha).model_copy(update={"tris": tris})
            c.execute("UPDATE assets SET tris=?, json=? WHERE sha256=?", (tris, asset.model_dump_json(), sha))
        return asset

    # ------------------------------------------------------------------------------------------------ reads
    def _find(self, sha: str) -> Asset | None:
        row = self.db.conn().execute("SELECT json FROM assets WHERE sha256=?", (sha,)).fetchone()
        return Asset.model_validate_json(row["json"]) if row else None

    def get_asset(self, sha: str) -> Asset:
        asset = self._find(sha) if is_sha(sha) else None
        if asset is None:
            raise NotFound("asset", sha)
        return asset

    def find_asset(self, sha: str) -> Asset | None:
        return self._find(sha) if is_sha(sha) else None

    def get(self, sha: str) -> bytes:
        """The file's bytes. Raises ``NotFound`` when the asset or its file is missing."""
        asset = self.get_asset(sha)
        p = self._file(sha, EXT_BY_KIND[asset.kind])
        try:
            return p.read_bytes()
        except FileNotFoundError:
            raise NotFound("asset file", sha) from None

    def describe(self, sha: str, ext: str) -> tuple[Path, str, str] | None:
        """``(path, mime, kind)`` for serving ``/cas/<sha>.<ext>``; None when unknown or the extension does not match."""
        asset = self.find_asset(sha)
        if asset is None or EXT_BY_KIND[asset.kind] != ext.lower().lstrip("."):
            return None
        p = self._file(sha, EXT_BY_KIND[asset.kind])
        return (p, asset.mime, asset.kind) if p.exists() else None

    def all_files(self) -> list[Path]:
        return [p for p in self.root.glob("??/*") if p.is_file() and not p.name.startswith(".")]

    def disk_usage(self) -> int:
        return sum(p.stat().st_size for p in self.all_files())
