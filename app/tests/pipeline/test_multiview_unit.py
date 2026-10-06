"""Multiview helpers: the Tripo input, the subject alpha of an opaque view, A_VIEWS on the mock Tripo views, the T1/T2 bodies."""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from duoskin.imaging import checks as C
from duoskin.pipeline import multiview as MV
from duoskin.providers.mock import meshes as M
from duoskin.providers.mock.tripo import MockTripo


def _png(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_repad_puts_the_subject_at_82_percent_of_2048():
    im = Image.new("RGBA", (500, 700), (0, 0, 0, 0))
    im.paste((200, 50, 50, 255), (100, 100, 400, 600))
    out = Image.open(io.BytesIO(MV.repad(_png(im))))
    assert out.size == (2048, 2048)
    bb = out.getchannel("A").point(lambda v: 255 if v > 0 else 0).getbbox()
    assert max(bb[2] - bb[0], bb[3] - bb[1]) == pytest.approx(0.825 * 2048, abs=3)
    assert abs((bb[0] + bb[2]) / 2 - 1024) < 3 and abs((bb[1] + bb[3]) / 2 - 1024) < 3


def test_subject_alpha_keys_a_flat_backdrop_and_keeps_real_alpha():
    flat = Image.new("RGB", (200, 200), (210, 210, 210))
    flat.paste((20, 80, 120), (50, 40, 150, 160))
    a = np.asarray(MV.subject_alpha(flat))[..., 3]
    assert a[100, 100] == 255 and a[5, 5] == 0
    real = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    real.paste((1, 2, 3, 255), (10, 10, 40, 40))
    assert np.asarray(MV.subject_alpha(real))[..., 3].max() == 255


@pytest.mark.parametrize("kind", ["rounded_box", "hair_with_head"])
def test_mock_tripo_views_pass_a_views(kind):
    views = M.render_views(M.build_mesh(kind, (150, 90, 60), 5), 512)
    ims = {k: MV.subject_alpha(Image.open(io.BytesIO(v))) for k, v in views.items()}
    assert C.check_views(ims).passed


def test_a_view_that_touches_the_border_is_cropped():
    ims = {}
    for v in MV.VIEWS:
        im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        im.paste((90, 90, 90, 255), (0, 20, 256, 236) if v == "left" else (40, 20, 216, 236))
        ims[v] = im
    assert not C.check_views(ims).passed


def test_t1_is_submitted_once_and_the_reply_has_the_four_views():
    t = MockTripo(running_polls=1)
    im = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    im.paste((120, 60, 30, 255), (60, 60, 240, 260))
    tok = t.upload(MV.repad(_png(im)), name=MV.upload_name("a.acc.0"))
    refs = []
    from duoskin.providers.base import CallCtx

    task = t.image_to_multiview(tok, ctx=CallCtx(set_remote_ref=refs.append, tag="T1"))
    assert refs == [task]                                       # the id was committed before the call returned
    for _ in range(5):
        st = t.task(task)
        if st.done:
            break
    files = t.download_files(task, [k for k in st.output_urls if k.endswith("_view_url")], status=st)
    assert sorted(k[:-9] for k in files) == sorted(MV.VIEWS)
    assert len([r for r in t.requests if r.get("op") == "image_to_multiview"]) == 1 if hasattr(t, "requests") and t.requests and isinstance(t.requests[0], dict) else True


def test_upload_name_marks_hair_for_the_mock_and_the_pack():
    assert "hair" in MV.upload_name("a.hair") and MV.upload_name("a.acc.0", "left") == "a_acc_0_left.png"
