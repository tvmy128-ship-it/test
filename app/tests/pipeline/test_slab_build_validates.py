"""A sticker-slab or primitive accessory passes the file gate (``mesh.validate``) although it has no approved 3D views.

Found by running the whole app with faults (tests/e2e_ui): the BUILD of a duo whose accessory is a sticker slab failed at ``mesh.validate`` with
"CHK-M08 / CHK-M13 did not run: no approved views supplied", the model "failed", and the part was sent to a Tripo pack that cannot be made for a
part without views. The slab worker op already knew it builds by code; the validate op that re-opens the exported files from the store did not.
"""
from __future__ import annotations

import inspect

from PIL import Image, ImageDraw

from duoskin.mesh import worker
from duoskin.mesh.types import MeshJob
from duoskin.pipeline import common, meshsteps


def slab_art(tmp_path):
    art = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(art).ellipse((30, 30, 226, 226), fill=(200, 80, 90, 255), outline=(40, 20, 60, 255), width=10)
    path = tmp_path / "art.png"
    art.save(path)
    return path


def job(op: str, tmp_path, name: str, input_path: str = "", **params) -> MeshJob:
    return MeshJob(op=op, input_path=input_path, out_dir=str(tmp_path / name), asset_type="Waist", attachment="WaistFrontAttachment",   # type: ignore[arg-type]
                   target_studs=(1.0, 1.0, 0.2), tris_target=3800, texture_px=512, params=params, asset_id="a.acc.0", licence="n/a")   # type: ignore[arg-type]


def built_slab(tmp_path):
    art = str(slab_art(tmp_path))
    built = worker.execute(job("slab", tmp_path, "built", art, art_path=art, size_studs=1.0, thickness=0.1, back="plain", kind="sticker_slab"))
    assert built.ok and "gltf" in built.files
    return built


def blocked_by_views(res) -> set[str]:
    return {c.check_id for c in common.hard_failures(list(res.checks))} & {"CHK-M08", "CHK-M13"}


def test_a_slab_built_by_code_validates_with_the_flag_the_build_step_passes(tmp_path):
    again = worker.execute(job("validate", tmp_path, "again", built_slab(tmp_path).files["gltf"], expect_slab=True, code_built=True))
    assert not blocked_by_views(again), [(c.check_id, c.evidence) for c in common.hard_failures(list(again.checks))]
    assert "built by code" in next(c for c in again.checks if c.check_id == "CHK-M08").evidence


def test_without_the_flag_a_slab_is_blocked_for_views_it_never_had(tmp_path):
    again = worker.execute(job("validate", tmp_path, "again", built_slab(tmp_path).files["gltf"], expect_slab=True))
    assert blocked_by_views(again) == {"CHK-M08", "CHK-M13"}              # the behaviour before the fix: the flag is what makes the difference


def test_the_validate_step_marks_slabs_and_primitives_as_built_by_code():
    assert '"code_built": p.source.get("kind") in ("slab", "primitive")' in inspect.getsource(meshsteps.run_validate)
