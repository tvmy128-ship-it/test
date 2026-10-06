"""The one pixel limit of the app, and the call that applies it to Pillow.

No image the app decodes may have more pixels than ``MAX_IMAGE_PIXELS`` (a 4 MB PNG of zeros can claim 100 000 x 100 000 pixels = 40 GB of
RGBA). Pillow's own default (89 MP) only *warns*, and raises at twice that; this lowers it so that an image above the limit is an error in
every process that calls ``apply_image_limits`` (the server, through the content store, and the mesh worker, which decodes the textures of
untrusted GLB files).
"""
from __future__ import annotations

MAX_IMAGE_PIXELS = 64_000_000


def apply_image_limits() -> None:
    """Lower Pillow's decompression-bomb limit to ``MAX_IMAGE_PIXELS`` (it raises ``DecompressionBombError`` above twice that). Idempotent."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
