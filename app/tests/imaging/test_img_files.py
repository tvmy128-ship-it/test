"""imaging/files.py: unicode-safe I/O, ingest normalisation, pixel_sha, PNG writer."""
from __future__ import annotations

import io
import struct
import zlib

import numpy as np
import pytest
from PIL import Image, ImageCms, PngImagePlugin

from duoskin.imaging import files as F


def _rgba(w=24, h=16, seed=3):
    rng = np.random.RandomState(seed)
    a = rng.randint(0, 256, (h, w, 4)).astype(np.uint8)
    a[..., 3] = np.where(rng.rand(h, w) > 0.3, 255, 0)
    a[a[..., 3] == 0, :3] = 0
    return a


def _png_bytes(arr, **kw):
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, "PNG", **kw)
    return buf.getvalue()


# ---------------------------------------------------------------- unicode paths
def test_unicode_path_roundtrip_pillow_and_cv2(tmp_path):
    folder = tmp_path / "tést_ディレクトリ_данные_\U0001f600"
    p = folder / "画像.png"
    arr = _rgba()
    F.save_png_rgba8(arr, p)
    assert p.is_file()
    assert (np.asarray(F.open_image(p)) == arr).all()
    dec = F.cv2_imread(p)
    assert dec.shape == (16, 24, 4) and (dec[..., 2] == arr[..., 0]).all()           # cv2 decodes BGRA
    p2 = folder / "cvé.png"
    assert F.cv2_imwrite(p2, dec)
    assert F.pixel_sha(p2) == F.pixel_sha(arr)


def test_doctor_unicode_probe_passes():
    r = F.check_unicode_roundtrip()
    assert r.check_id == "CHK-S01" and r.passed and r.ran


def test_write_bytes_atomic_replaces_and_leaves_no_temp(tmp_path):
    p = tmp_path / "a" / "b.bin"
    F.write_bytes_atomic(p, b"one")
    F.write_bytes_atomic(p, b"two")
    assert p.read_bytes() == b"two"
    assert [x.name for x in p.parent.iterdir()] == ["b.bin"]


def test_sniff_image_type_by_magic_bytes():
    assert F.sniff_image_type(_png_bytes(_rgba())) == "png"
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "JPEG")
    assert F.sniff_image_type(buf.getvalue()) == "jpeg"
    assert F.sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert F.sniff_image_type(b"GIF89a....") == "gif"
    assert F.sniff_image_type(b"<svg></svg>") is None


# ---------------------------------------------------------------- normalisation
def test_normalise_always_returns_8bit_rgba():
    for mode, colour in (("RGB", (1, 2, 3)), ("L", 7), ("LA", (7, 200)), ("1", 1), ("RGBA", (1, 2, 3, 4)), ("CMYK", (0, 0, 0, 0))):
        im, rep = F.normalise_image(Image.new(mode, (5, 4), colour))
        assert im.mode == "RGBA" and np.asarray(im).dtype == np.uint8 and rep.source_mode == mode
        F.assert_rgba8(im)


def test_exif_orientation_is_applied():
    base = Image.new("RGB", (40, 20), (255, 0, 0))
    base.paste((0, 0, 255), (0, 0, 10, 10))
    ex = base.getexif()
    ex[0x0112] = 6                                                    # rotate 90 CW to display
    buf = io.BytesIO()
    base.save(buf, "JPEG", exif=ex, quality=100)
    im, rep = F.normalise_image(buf.getvalue())
    assert im.size == (20, 40) and rep.exif_orientation == 6 and "exif_transpose" in rep.actions


def test_palette_image_with_trns_keeps_transparency():
    p = Image.new("P", (6, 6), 0)
    p.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    p.paste(1, (3, 0, 6, 6))
    buf = io.BytesIO()
    p.save(buf, "PNG", transparency=0)
    im, rep = F.normalise_image(buf.getvalue())
    a = np.asarray(im)
    assert rep.had_trns and (a[:, :3, 3] == 0).all() and (a[:, 3:, 3] == 255).all()
    assert tuple(a[0, 4, :3]) == (0, 255, 0)


def test_16bit_grey_is_shifted_not_clipped():
    arr = np.array([[0, 256, 32768, 65535]], np.uint16)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    im, rep = F.normalise_image(buf.getvalue())
    g = np.asarray(im)[0, :, 0]
    assert list(g) == [0, 1, 128, 255] and "i16_shift" in rep.actions and rep.bit_depth == 16


def _s15(v):
    return struct.pack(">i", round(v * 65536))


def make_matrix_icc(desc, primaries, gamma, wtpt=(0.9642, 1.0, 0.8249)):
    """A minimal ICC v2 matrix/TRC RGB profile (enough for lcms): lets the tests use a real non-sRGB profile offline."""
    def xyz(v):
        return b"XYZ \0\0\0\0" + b"".join(_s15(c) for c in v)

    ascii_desc = desc.encode() + b"\0"
    tags = {
        b"desc": b"desc\0\0\0\0" + struct.pack(">I", len(ascii_desc)) + ascii_desc + b"\0" * (4 + 4 + 2 + 1 + 67),
        b"cprt": b"text\0\0\0\0" + b"none\0",
        b"wtpt": xyz(wtpt),
        b"rXYZ": xyz(primaries[0]), b"gXYZ": xyz(primaries[1]), b"bXYZ": xyz(primaries[2]),
    }
    curv = b"curv\0\0\0\0" + struct.pack(">I", 1) + struct.pack(">H", round(gamma * 256)) + b"\0\0"
    tags[b"rTRC"] = tags[b"gTRC"] = tags[b"bTRC"] = curv
    n = len(tags)
    off = 128 + 4 + 12 * n
    table, body = b"", b""
    cache = {}
    for sig, data in tags.items():
        data += b"\0" * (-len(data) % 4)
        if data not in cache:
            cache[data] = off + len(body)
            body += data
        table += sig + struct.pack(">II", cache[data], len(data))
    header = struct.pack(">I4sI4s4s4s", 128 + 4 + 12 * n + len(body), b"lcms", 0x02400000, b"mntr", b"RGB ", b"XYZ ")
    header += b"\0" * 12 + b"acsp" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8 + b"\0" * 4
    header += _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"\0" * 4 + b"\0" * 16 + b"\0" * 28
    assert len(header) == 128, len(header)
    return header + struct.pack(">I", n) + table + body


P3_PRIMARIES = ((0.5151, 0.2412, -0.0011), (0.2920, 0.6922, 0.0419), (0.1571, 0.0666, 0.7841))


def test_icc_profile_converted_to_srgb_and_srgb_profile_is_a_noop():
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    img = Image.new("RGB", (4, 4), (10, 120, 200))
    img.info["icc_profile"] = srgb
    out, rep = F.normalise_image(img)
    assert "icc_is_srgb" in rep.actions and "icc_to_srgb" not in rep.actions
    assert tuple(np.asarray(out)[0, 0, :3]) == (10, 120, 200)
    p3 = make_matrix_icc("Test Display P3", P3_PRIMARIES, 2.2)
    img2 = Image.new("RGB", (4, 4), (100, 200, 80))
    img2.info["icc_profile"] = p3
    out2, rep2 = F.normalise_image(img2)
    assert "icc_to_srgb" in rep2.actions and rep2.icc_description == "Test Display P3"
    moved = np.abs(np.asarray(out2)[0, 0, :3].astype(int) - np.array([100, 200, 80])).max()
    assert moved >= 4 and np.asarray(out2).dtype == np.uint8
    assert out2.info.get("icc_profile") is None                       # the output claims no profile: it IS sRGB now


def test_icc_conversion_keeps_alpha_and_changes_pixel_sha():
    p3 = make_matrix_icc("Test Display P3", P3_PRIMARIES, 2.2)
    arr = np.zeros((6, 6, 4), np.uint8)
    arr[..., :3] = (100, 200, 80)
    arr[:, :3, 3] = 255
    arr[:, 3:, 3] = 77
    im = Image.fromarray(arr, "RGBA")
    tagged = _png_bytes(arr, icc_profile=p3)
    out, _ = F.normalise_image(tagged)
    assert (np.asarray(out)[..., 3] == arr[..., 3]).all()
    assert F.pixel_sha(tagged) != F.pixel_sha(im)                    # the profile changes what the pixels mean


def test_corrupt_icc_never_blocks_ingest():
    img = Image.new("RGB", (4, 4), (10, 120, 200))
    img.info["icc_profile"] = b"not a profile at all"
    out, rep = F.normalise_image(img)
    assert out.mode == "RGBA" and any(a.startswith("icc_failed") for a in rep.actions)


def test_ndarray_and_bytes_inputs():
    arr = _rgba()
    assert (np.asarray(F.open_image(arr)) == arr).all()
    assert F.open_image(arr[..., :3].astype(np.uint16) << 8).mode == "RGBA"
    assert F.open_image(_png_bytes(arr)).size == (24, 16)
    with pytest.raises(ValueError):
        F.open_image(np.zeros((4, 4, 5), np.uint8))


def test_assert_rgba8_rejects_flattening():
    with pytest.raises(F.ImageContractError):
        F.assert_rgba8(Image.new("RGB", (2, 2)))


# ---------------------------------------------------------------- pixel_sha
def test_pixel_sha_is_stable_across_metadata_and_encoding():
    arr = _rgba()
    base = F.pixel_sha(_png_bytes(arr))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "hello é")
    assert F.pixel_sha(_png_bytes(arr, pnginfo=info, dpi=(300, 300))) == base           # text chunks, dpi
    assert F.pixel_sha(_png_bytes(arr, compress_level=0)) == base                       # compression level
    assert F.pixel_sha(_png_bytes(arr, optimize=True)) == base
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    assert F.pixel_sha(_png_bytes(arr, icc_profile=srgb)) == base                       # an sRGB ICC chunk
    im = Image.fromarray(arr, "RGBA")
    ex = Image.Exif()
    ex[0x0112] = 1                                                                      # orientation "normal"
    buf = io.BytesIO()
    im.save(buf, "PNG", exif=ex)
    assert F.pixel_sha(buf.getvalue()) == base
    assert F.pixel_sha(im) == F.pixel_sha(arr) == base                                  # PIL image, array, bytes
    assert len(base) == 64


def test_pixel_sha_ignores_rgb_under_alpha_zero_but_not_visible_changes():
    arr = _rgba()
    other = arr.copy()
    other[arr[..., 3] == 0, :3] = (9, 9, 9)
    assert F.pixel_sha(other) == F.pixel_sha(arr)
    changed = arr.copy()
    y, x = np.argwhere(arr[..., 3] == 255)[0]
    changed[y, x, 0] ^= 1
    assert F.pixel_sha(changed) != F.pixel_sha(arr)
    wider = np.concatenate([arr, arr[:, :1]], axis=1)
    assert F.pixel_sha(wider) != F.pixel_sha(arr)


def test_pixel_sha_follows_appearance_for_rotation_and_indexed_encodings():
    arr = _rgba()
    im = Image.fromarray(arr, "RGBA")
    ex = Image.Exif()
    ex[0x0112] = 3                                                                      # displays rotated by 180: a different picture
    buf = io.BytesIO()
    im.save(buf, "PNG", exif=ex)
    assert F.pixel_sha(buf.getvalue()) != F.pixel_sha(arr)
    assert F.pixel_sha(buf.getvalue()) == F.pixel_sha(np.asarray(im.rotate(180)))
    rgb = Image.new("RGB", (8, 8), (10, 20, 30))
    pal = Image.new("P", (8, 8), 0)
    pal.putpalette([10, 20, 30] + [0] * 765)
    assert F.pixel_sha(rgb) == F.pixel_sha(pal) == F.pixel_sha(Image.new("RGBA", (8, 8), (10, 20, 30, 255)))


# ---------------------------------------------------------------- PNG writer
def _chunk(t, p):
    return struct.pack(">I", len(p)) + t + p + struct.pack(">I", zlib.crc32(t + p) & 0xFFFFFFFF)


def _png_with_colour_chunks(arr):
    raw = _png_bytes(arr)
    chunks = F.png_chunks(raw)
    out = F.PNG_MAGIC
    for t, p in chunks:
        out += _chunk(t, p)
        if t == b"IHDR":
            out += _chunk(b"gAMA", struct.pack(">I", 45455)) + _chunk(b"sRGB", b"\x00") + _chunk(b"cHRM", b"\x00" * 32)
            out += _chunk(b"tEXt", b"Comment\x00x") + _chunk(b"pHYs", b"\x00" * 9)
    return out


def test_save_png_rgba8_strips_colour_chunks_and_verifies(tmp_path):
    arr = _rgba()
    dirty = _png_with_colour_chunks(arr)
    assert {"gAMA", "sRGB", "cHRM"} <= set(F.png_info(dirty).chunks)
    with pytest.raises(F.ImageContractError):
        F.verify_png_rgba8(dirty)
    clean = F.save_png_rgba8(dirty, tmp_path / "x.png")
    info = F.verify_png_rgba8(clean, (24, 16))
    assert info.colour_type == 6 and info.bit_depth == 8 and not info.colour_chunks
    assert set(info.chunks) <= {"IHDR", "IDAT", "IEND"}
    assert (tmp_path / "x.png").read_bytes() == clean
    assert (np.asarray(F.open_image(clean)) == arr).all()


def test_verify_rejects_wrong_size_mode_and_garbage():
    rgb = io.BytesIO()
    Image.new("RGB", (4, 4)).save(rgb, "PNG")
    with pytest.raises(F.ImageContractError):
        F.verify_png_rgba8(rgb.getvalue())
    ok = F.encode_png_rgba8(_rgba())
    with pytest.raises(F.ImageContractError):
        F.verify_png_rgba8(ok, (585, 559))
    with pytest.raises(ValueError):
        F.png_chunks(b"not a png")
    with pytest.raises(ValueError):
        F.png_chunks(F.PNG_MAGIC + b"\x00\x00\x00")
    with pytest.raises(ValueError):
        F.png_chunks(F.PNG_MAGIC + struct.pack(">I", 999) + b"IDAT")


def test_encode_is_deterministic_and_rgb_texture_must_be_opaque():
    a = _rgba()
    assert F.encode_png_rgba8(a) == F.encode_png_rgba8(Image.fromarray(a, "RGBA"))
    opaque = a.copy()
    opaque[..., 3] = 255
    data = F.encode_png_rgb8(opaque)
    assert F.png_info(data).colour_type == 2
    with pytest.raises(F.ImageContractError):
        F.encode_png_rgb8(a)


def test_jpeg_preview_flattens_alpha_explicitly():
    im = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
    jpg = F.encode_jpeg_preview(im, bg=(242, 242, 242))
    assert F.sniff_image_type(jpg) == "jpeg"
    px = Image.open(io.BytesIO(jpg)).getpixel((8, 8))
    assert all(abs(c - 242) <= 3 for c in px)


# ---------------------------------------------------------------- resize artefacts (IMG-13)
def _light_disc_on_clear(size=200):
    a = np.zeros((size, size, 4), np.uint8)
    yy, xx = np.mgrid[:size, :size]
    a[((yy - 100) ** 2 + (xx - 100) ** 2) < 80 ** 2] = (250, 250, 250, 255)
    a[..., :3][a[..., 3] == 0] = 0                                       # transparent black: the classic fringe maker
    return Image.fromarray(a, "RGBA")


def test_premultiplied_resize_has_no_dark_fringe_but_naive_resize_does():
    src = _light_disc_on_clear()
    good = F.resize_rgba(src, (50, 50), Image.Resampling.BOX)
    import cv2

    naive = Image.fromarray(cv2.resize(np.asarray(src), (50, 50), interpolation=cv2.INTER_AREA), "RGBA")   # OpenCV ignores alpha
    assert F.edge_darkening_ratio(good) >= 0.95
    assert F.edge_darkening_ratio(naive) < 0.9
    assert F.check_resize_artifacts(good).passed
    r = F.check_resize_artifacts(naive)
    assert not r.passed and r.kind == "assert" and r.check_id == "A_RESIZE"
    assert F.edge_darkening_ratio(Image.new("RGBA", (8, 8), (1, 2, 3, 255))) == 1.0


def test_bleed_rgb_fills_transparent_pixels_without_touching_alpha():
    src = _light_disc_on_clear()
    out = F.bleed_rgb(src)
    a = np.asarray(out)
    assert (a[..., 3] == np.asarray(src)[..., 3]).all()
    assert tuple(a[0, 0, :3]) == (250, 250, 250)
    assert F.bleed_rgb(Image.new("RGBA", (4, 4), (0, 0, 0, 0))).size == (4, 4)


def test_check_ingest_and_check_size_results():
    r = F.check_ingest(_png_bytes(_rgba()))
    assert r.check_id == "A_INGEST" and r.passed and r.kind == "assert"
    assert F.check_size((1024, 1024), (1024, 1024)).passed
    bad = F.check_size((1000, 1024), (1024, 1024))
    assert not bad.passed and bad.kind == "hard" and "1000" in bad.evidence
