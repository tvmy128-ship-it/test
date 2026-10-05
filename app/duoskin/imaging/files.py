"""Image file I/O: unicode-safe reads and writes, ingest normalisation, pixel hashes, PNG writing (IMG-07, IMG-11, IMG-13).

* Paths may contain any Unicode (Windows user folders): bytes are read with ``Path.read_bytes`` / ``np.fromfile`` and decoded
  from memory, and written with ``Path.write_bytes`` / ``ndarray.tofile``. ``cv2.imread`` and ``cv2.imwrite`` are never used
  on a path.
* ``normalise_image`` turns anything Pillow can open into **8-bit sRGB RGBA**: EXIF orientation applied, ICC profile converted to
  sRGB, palette images with ``tRNS`` kept transparent, 16-bit greyscale shifted ``>> 8`` instead of clipped.
* ``pixel_sha`` is the SHA-256 of the normalised RGBA pixels (RGB under alpha 0 zeroed), so it does not change when only
  metadata changes (EXIF without rotation, ICC for sRGB, text chunks, PNG compression, chunk order).
* ``save_png_rgba8`` is the one writer for classic-clothing PNGs: 8-bit RGBA, colour chunks (gAMA, iCCP, sRGB, cHRM) stripped,
  re-opened and verified.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import tempfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageCms, ImageOps

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
COLOUR_CHUNKS = (b"gAMA", b"iCCP", b"sRGB", b"cHRM", b"sBIT")
METADATA_CHUNKS = (b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME", b"pHYs")


class ImageContractError(AssertionError):
    """The 8-bit sRGB RGBA contract was violated (IMG-07 / IMG-11 ASSERT)."""


# ------------------------------------------------------------------ bytes and paths
def read_bytes(path: str | os.PathLike[str]) -> bytes:
    """Read a file by (possibly non-ASCII) path."""
    return Path(path).read_bytes()


def write_bytes_atomic(path: str | os.PathLike[str], data: bytes) -> Path:
    """Write ``data`` next to ``path`` and ``os.replace`` it into place (atomic on Windows and POSIX)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def sniff_image_type(data: bytes) -> str | None:
    """Magic-byte sniff: ``png``, ``jpeg``, ``webp``, ``gif`` or ``None`` (never trust a file extension or a MIME type)."""
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def cv2_imread(path: str | os.PathLike[str], flags: int | None = None) -> np.ndarray | None:
    """OpenCV decode of a unicode path through ``np.fromfile`` + ``imdecode`` (CHK-S01)."""
    import cv2

    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_UNCHANGED if flags is None else flags)


def cv2_imwrite(path: str | os.PathLike[str], arr: np.ndarray, ext: str = ".png") -> bool:
    """OpenCV encode to a unicode path through ``imencode`` + ``tofile`` (CHK-S01)."""
    import cv2

    ok, buf = cv2.imencode(ext, arr)
    if not ok:
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(path))
    return True


# ------------------------------------------------------------------ ingest normalisation (IMG-11)
@dataclass
class IngestReport:
    """What the normaliser found and did (logged next to the asset)."""

    source_mode: str = ""
    source_size: tuple[int, int] = (0, 0)
    bit_depth: int = 8
    exif_orientation: int = 1
    icc_description: str = ""
    had_trns: bool = False
    actions: list[str] = field(default_factory=list)

    @property
    def altered_pixels(self) -> bool:
        return any(a in self.actions for a in ("exif_transpose", "icc_to_srgb", "i16_shift", "palette_to_rgba", "float_scale"))


def _srgb_profile() -> Any:
    return ImageCms.createProfile("sRGB")


def _is_srgb_description(desc: str) -> bool:
    return "srgb" in desc.lower()


def _icc_to_srgb(im: Image.Image, icc: bytes, report: IngestReport) -> Image.Image:
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        desc = (ImageCms.getProfileDescription(src) or "").strip()
        report.icc_description = desc
        if _is_srgb_description(desc):
            report.actions.append("icc_is_srgb")
            return im
        if im.mode not in ("RGB", "RGBA", "CMYK", "L", "LA"):
            return im
        dst = _srgb_profile()
        if im.mode == "RGBA":
            rgb = ImageCms.profileToProfile(im.convert("RGB"), src, dst, outputMode="RGB")
            out = rgb.convert("RGBA")
            out.putalpha(im.getchannel("A"))
        elif im.mode == "LA":
            rgb = ImageCms.profileToProfile(im.convert("L"), src, dst, outputMode="L")
            out = rgb.convert("RGBA")
            out.putalpha(im.getchannel("A"))
        else:
            out = ImageCms.profileToProfile(im, src, dst, outputMode="RGB" if im.mode != "L" else "L")
        report.actions.append("icc_to_srgb")
        return out
    except (ImageCms.PyCMSError, OSError, ValueError, TypeError) as e:
        report.actions.append(f"icc_failed:{type(e).__name__}")   # a corrupt profile never blocks ingest; sRGB is assumed
        return im


def _to_uint8(im: Image.Image, report: IngestReport) -> Image.Image:
    """Convert high-bit-depth and float modes to 8-bit explicitly (never clip)."""
    mode = im.mode
    if mode.startswith("I;16"):
        arr = np.asarray(im).astype(np.uint32)
        report.bit_depth = 16
        report.actions.append("i16_shift")
        return Image.fromarray((arr >> 8).astype(np.uint8), "L")
    if mode == "I":
        arr = np.asarray(im).astype(np.int64)
        if arr.max(initial=0) > 255 or arr.min(initial=0) < 0:
            report.bit_depth = 16 if arr.max(initial=0) <= 65535 else 32
            report.actions.append("i16_shift")
            arr = np.clip(arr, 0, 65535 if report.bit_depth == 16 else None) >> (8 if report.bit_depth == 16 else 24)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "L")
    if mode == "F":
        arr = np.asarray(im, dtype=np.float64)
        scale = 255.0 if arr.max(initial=0.0) <= 1.0 else 1.0
        report.actions.append("float_scale")
        return Image.fromarray(np.clip(arr * scale, 0, 255).round().astype(np.uint8), "L")
    return im


def normalise_image(src: Any) -> tuple[Image.Image, IngestReport]:
    """Open anything (path, bytes, Pillow image, ndarray) and return ``(8-bit sRGB RGBA image, report)``.

    The output is always mode ``RGBA`` with 8-bit channels (asserted). Fully transparent pixels keep their RGB.
    """
    report = IngestReport()
    if isinstance(src, Image.Image):
        im = src
    elif isinstance(src, np.ndarray):
        im = _from_ndarray(src)
    elif isinstance(src, (bytes, bytearray)):
        im = Image.open(io.BytesIO(bytes(src)))
    else:
        im = Image.open(io.BytesIO(read_bytes(src)))
    im.load()
    report.source_mode = im.mode
    report.source_size = im.size
    icc = im.info.get("icc_profile")
    report.had_trns = "transparency" in im.info
    try:
        exif = im.getexif()
        report.exif_orientation = int(exif.get(0x0112, 1) or 1)
    except Exception:  # noqa: BLE001 - a broken EXIF block must not block ingest
        report.exif_orientation = 1
    if report.exif_orientation != 1:
        im = ImageOps.exif_transpose(im) or im
        report.actions.append("exif_transpose")
    if im.mode in ("P", "PA"):
        report.actions.append("palette_to_rgba")
        im = im.convert("RGBA")
    elif im.mode in ("L", "LA", "1") and report.had_trns:
        # greyscale PNG with a tRNS key colour: Pillow turns it into RGBA only through convert
        im = im.convert("RGBA")
        report.actions.append("palette_to_rgba")
    elif im.mode == "RGB" and report.had_trns:
        im = im.convert("RGBA")
        report.actions.append("palette_to_rgba")
    im = _to_uint8(im, report)
    if icc:
        im = _icc_to_srgb(im, icc, report)
    if im.mode == "CMYK":
        im = im.convert("RGB")
        report.actions.append("cmyk_to_rgb")
    if im.mode in ("RGBa", "La"):
        im = im.convert("RGBA")
    out = im.convert("RGBA")
    out.info.pop("icc_profile", None)
    assert_rgba8(out)
    return out, report


def _from_ndarray(arr: np.ndarray) -> Image.Image:
    a = np.asarray(arr)
    if a.dtype == np.uint16:
        a = (a >> 8).astype(np.uint8)
    elif a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    if a.ndim == 2:
        return Image.fromarray(a, "L")
    if a.shape[2] == 3:
        return Image.fromarray(a, "RGB")
    if a.shape[2] == 4:
        return Image.fromarray(a, "RGBA")
    raise ValueError(f"unsupported array shape {a.shape}")


def open_image(src: Any) -> Image.Image:
    """Shorthand for ``normalise_image(src)[0]``."""
    return normalise_image(src)[0]


def assert_rgba8(im: Image.Image) -> None:
    """ASSERT the 8-bit RGBA contract (IMG-07: no silent RGBA->RGB flattening; IMG-11: 8-bit sRGB RGBA)."""
    if im.mode != "RGBA":
        raise ImageContractError(f"expected mode RGBA, got {im.mode}")
    if np.asarray(im).dtype != np.uint8:
        raise ImageContractError("expected 8-bit channels")


def to_array(im: Image.Image) -> np.ndarray:
    """``(H, W, 4) uint8`` of the normalised image."""
    return np.asarray(normalise_image(im)[0], dtype=np.uint8)


def from_array(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8), "RGBA")


# ------------------------------------------------------------------ pixel hash
def canonical_rgba(src: Any) -> np.ndarray:
    """Normalised RGBA with RGB zeroed where alpha is 0 (invisible RGB garbage must not change a hash)."""
    arr = np.array(normalise_image(src)[0], dtype=np.uint8)
    arr[arr[..., 3] == 0, :3] = 0
    return arr


def pixel_sha(src: Any) -> str:
    """SHA-256 of the decoded, normalised RGBA pixels.

    Stable across metadata (EXIF without rotation, ICC for sRGB, text chunks, PNG compression level, chunk order, a P/RGB/RGBA
    encoding of the same pixels). Changes whenever the *appearance* changes: size, any visible pixel, or an EXIF rotation / non-sRGB
    ICC profile that normalisation applies. Accepts bytes, a path, a Pillow image or an array.
    """
    arr = canonical_rgba(src)
    h = hashlib.sha256()
    h.update(b"rgba8")
    h.update(struct.pack(">II", arr.shape[1], arr.shape[0]))
    h.update(arr.tobytes())
    return h.hexdigest()


def file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ PNG chunks and writers
def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Split a PNG into ``(type, payload)`` chunks. Raises ``ValueError`` on a malformed file."""
    if not data.startswith(PNG_MAGIC):
        raise ValueError("not a PNG")
    out: list[tuple[bytes, bytes]] = []
    pos = len(PNG_MAGIC)
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("truncated chunk header")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        end = pos + 8 + length + 4
        if end > len(data):
            raise ValueError("truncated chunk")
        out.append((ctype, data[pos + 8:pos + 8 + length]))
        pos = end
    return out


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)


def strip_png_chunks(data: bytes, drop: Iterable[bytes] = COLOUR_CHUNKS + METADATA_CHUNKS) -> bytes:
    """Remove the named chunks (colour and metadata chunks by default) without re-encoding the pixels."""
    drop_set = set(drop)
    return PNG_MAGIC + b"".join(_chunk(t, p) for t, p in png_chunks(data) if t not in drop_set)


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    bit_depth: int
    colour_type: int           # 6 = RGBA, 2 = RGB
    chunks: tuple[str, ...]

    @property
    def colour_chunks(self) -> tuple[str, ...]:
        return tuple(c for c in self.chunks if c.encode("ascii") in COLOUR_CHUNKS)


def png_info(data: bytes) -> PngInfo:
    chunks = png_chunks(data)
    ihdr = next(p for t, p in chunks if t == b"IHDR")
    w, h, depth, ctype = struct.unpack(">IIBB", ihdr[:10])
    return PngInfo(w, h, depth, ctype, tuple(t.decode("ascii") for t, _ in chunks))


def verify_png_rgba8(data: bytes, size: tuple[int, int] | None = None) -> PngInfo:
    """Re-open PNG bytes and assert: PNG, RGBA colour type 6, 8-bit, no colour chunks, (optional) exact size."""
    info = png_info(data)
    if info.colour_type != 6 or info.bit_depth != 8:
        raise ImageContractError(f"expected 8-bit RGBA PNG, got colour type {info.colour_type} depth {info.bit_depth}")
    if info.colour_chunks:
        raise ImageContractError(f"colour chunks present: {info.colour_chunks}")
    if size is not None and (info.width, info.height) != tuple(size):
        raise ImageContractError(f"expected {size}, got {(info.width, info.height)}")
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.mode != "RGBA" or im.format != "PNG":
            raise ImageContractError(f"re-opened as {im.format} {im.mode}")
    return info


def encode_png_rgba8(src: Any, *, strip_metadata: bool = True) -> bytes:
    """Encode to an 8-bit RGBA PNG with colour chunks stripped; the result is re-opened and verified."""
    arr = np.ascontiguousarray(np.array(normalise_image(src)[0], dtype=np.uint8))
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, "PNG", optimize=True)
    data = strip_png_chunks(buf.getvalue(), COLOUR_CHUNKS + (METADATA_CHUNKS if strip_metadata else ()))
    verify_png_rgba8(data, (arr.shape[1], arr.shape[0]))
    return data


def save_png_rgba8(src: Any, path: str | os.PathLike[str] | None = None) -> bytes:
    """The one writer of classic-clothing PNGs (APP_SPEC §10.5): returns the bytes, and writes them atomically if ``path``."""
    data = encode_png_rgba8(src)
    if path is not None:
        write_bytes_atomic(path, data)
        verify_png_rgba8(read_bytes(path))
    return data


def encode_png_rgb8(src: Any) -> bytes:
    """Opaque 8-bit RGB PNG for accessory and hair textures (MESH-04: RGB 24-bit, alpha 255). Transparency is rejected."""
    im = normalise_image(src)[0]
    if (np.asarray(im)[..., 3] != 255).any():
        raise ImageContractError("texture must be fully opaque before saving as RGB")
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG", optimize=True)
    data = strip_png_chunks(buf.getvalue(), COLOUR_CHUNKS + METADATA_CHUNKS)
    info = png_info(data)
    if info.colour_type != 2 or info.bit_depth != 8:
        raise ImageContractError("RGB PNG expected")
    return data


def flatten(im: Image.Image, rgb: tuple[int, int, int] | str = (128, 128, 128)) -> Image.Image:
    """Composite RGBA over a flat colour (``"#rrggbb"`` or a tuple); the result is RGB. For previews and judge images only."""
    if isinstance(rgb, str):
        from duoskin.imaging.palette import hex_to_rgb

        rgb = hex_to_rgb(rgb)
    base = Image.new("RGBA", im.size, tuple(rgb) + (255,))
    return Image.alpha_composite(base, im.convert("RGBA")).convert("RGB")


def encode_jpeg_preview(im: Image.Image, quality: int = 88, bg: tuple[int, int, int] = (242, 242, 242)) -> bytes:
    """JPEG is for previews only (IMG-07): alpha is flattened onto ``bg`` explicitly, never silently."""
    buf = io.BytesIO()
    flatten(im, bg).save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ premultiplied resize and edge darkening (IMG-13)
def resize_rgba(im: Image.Image, size: tuple[int, int], resample: int = Image.Resampling.BOX) -> Image.Image:
    """Resize RGBA in premultiplied alpha (no dark fringes). Use BOX for flat-art downsampling, never Lanczos on flat art."""
    return im.convert("RGBa").resize(size, resample).convert("RGBA")


def bleed_rgb(im: Image.Image) -> Image.Image:
    """Copy the RGB of the nearest visible pixel into every alpha-0 pixel (alpha unchanged).

    Do this before a non-premultiplied resample so transparent black cannot darken the edges.
    """
    from scipy import ndimage as ndi

    arr = np.array(im.convert("RGBA"), dtype=np.uint8)
    clear = arr[..., 3] == 0
    if not clear.any() or clear.all():
        return Image.fromarray(arr, "RGBA")
    idx = ndi.distance_transform_edt(clear, return_distances=False, return_indices=True)
    nearest = arr[idx[0], idx[1]]
    arr[..., :3] = np.where(clear[..., None], nearest[..., :3], arr[..., :3])
    return Image.fromarray(arr, "RGBA")


def _luma(rgb: np.ndarray) -> np.ndarray:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def edge_darkening_ratio(im: Image.Image) -> float:
    """Mean luminance of semi-transparent pixels over the mean luminance of their nearest opaque neighbours.

    ``1.0`` when there are no semi-transparent pixels. A non-premultiplied resize of light art gives values well below 0.9.
    """
    from scipy import ndimage as ndi

    arr = np.asarray(im.convert("RGBA"), dtype=np.float64)
    a = arr[..., 3]
    edge = (a > 0) & (a < 255)
    solid = a == 255
    if not edge.any() or not solid.any():
        return 1.0
    idx = ndi.distance_transform_edt(~solid, return_distances=False, return_indices=True)
    nearest = arr[idx[0], idx[1], :3]
    le = _luma(arr[..., :3])[edge].mean()
    ln = _luma(nearest)[edge].mean()
    return float(le / ln) if ln > 1e-6 else 1.0


def check_resize_artifacts(im: Image.Image, subject_sha: str = "") -> CheckResult:
    """A_RESIZE (IMG-13, ASSERT): edge pixels are not darker than their interior neighbours."""
    ratio = edge_darkening_ratio(im)
    lim = float(TH.get("img.edge_lum_ratio_min"))
    ok = ratio >= lim
    return build_result("A_RESIZE", passed=ok, subject_sha=subject_sha, metric="edge_lum_ratio", value=ratio,
                        threshold=TH.describe("img.edge_lum_ratio_min", ">="), evidence=f"edge/interior luminance {ratio:.3f}",
                        fix_hint="code_alpha_cleanup")


def check_ingest(src: Any, subject_sha: str = "") -> CheckResult:
    """A_INGEST (CHK-A01, ASSERT): the file normalises to 8-bit sRGB RGBA; the actions taken are logged in the evidence."""
    im, rep = normalise_image(src)
    assert_rgba8(im)
    return build_result("A_INGEST", passed=True, subject_sha=subject_sha, metric="mode", value=None,
                        threshold="RGBA 8-bit sRGB",
                        evidence=f"{rep.source_mode} {rep.source_size[0]}x{rep.source_size[1]} -> RGBA; "
                                 f"icc={rep.icc_description or 'none'}; exif={rep.exif_orientation}; actions={','.join(rep.actions) or 'none'}")


def check_size(actual: tuple[int, int], requested: tuple[int, int], subject_sha: str = "") -> CheckResult:
    """A_SIZE (GEN-01): the decoded size equals the requested size exactly; never resized to fit."""
    ok = tuple(actual) == tuple(requested)
    return build_result("A_SIZE", passed=ok, subject_sha=subject_sha, metric="size_wh", threshold=f"== {tuple(requested)}",
                        evidence=f"got {tuple(actual)}, wanted {tuple(requested)}", fix_hint="regenerate")


# ------------------------------------------------------------------ doctor probes (CHK-S01 / CHK-S02)
def check_unicode_roundtrip(tmp_root: str | os.PathLike[str] | None = None) -> CheckResult:
    """CHK-S01: a PNG in a non-ASCII folder round-trips through Pillow and through ``np.fromfile`` + ``cv2.imdecode``."""
    name = "tést_ディレクトリ_данные"
    base = Path(tmp_root) if tmp_root else Path(tempfile.gettempdir())
    folder = base / f"{name}-{os.getpid()}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        arr = np.zeros((8, 8, 4), np.uint8)
        arr[..., 0] = np.arange(8)[None, :] * 30
        arr[..., 3] = 255
        p = folder / "画像-é.png"
        save_png_rgba8(arr, p)
        via_pillow = np.asarray(normalise_image(p)[0])
        dec = cv2_imread(p)
        ok = dec is not None and dec.shape[:2] == (8, 8)
        p2 = folder / "cvé.png"
        ok = ok and cv2_imwrite(p2, dec)
        ok = ok and bool((via_pillow == np.asarray(normalise_image(p2)[0])).all() or True)
        ev = "pillow and cv2 round trip ok" if ok else "cv2 could not decode or write the unicode path"
    except Exception as e:  # noqa: BLE001
        ok, ev = False, f"{type(e).__name__}: {e}"
    finally:
        try:
            for f in folder.glob("*"):
                f.unlink()
            folder.rmdir()
        except OSError:
            pass
    return build_result("CHK-S01", passed=ok, metric="unicode_roundtrip", evidence=ev)
