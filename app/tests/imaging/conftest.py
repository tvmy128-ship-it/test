"""Shared fixtures for the imaging tests: small synthetic assets (no network, no real images)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw


def make_sticker(size: int = 512, body=(200, 40, 40), line=(20, 20, 20), core=(250, 220, 50)) -> Image.Image:
    """A hard-edged cut-out: red disc, dark outline, yellow centre, fully transparent border (a well-behaved print)."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 512
    d.ellipse([100 * s, 100 * s, 400 * s, 400 * s], fill=body + (255,), outline=line + (255,), width=max(1, round(6 * s)))
    d.ellipse([200 * s, 200 * s, 300 * s, 300 * s], fill=core + (255,))
    return im


def make_checker(size: int = 512, cell: int = 16, c1=(255, 255, 255), c2=(204, 204, 204)) -> Image.Image:
    yy, xx = np.mgrid[:size, :size]
    on = ((yy // cell) + (xx // cell)) % 2 == 0
    a = np.zeros((size, size, 3), np.uint8)
    a[on] = c1
    a[~on] = c2
    return Image.fromarray(a).convert("RGBA")


@pytest.fixture
def sticker() -> Image.Image:
    return make_sticker()


@pytest.fixture
def checker() -> Image.Image:
    return make_checker()


@pytest.fixture
def rng() -> np.random.RandomState:
    return np.random.RandomState(1234)
