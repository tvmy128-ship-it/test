"""Run one mesh worker job from a step handler (APP_SPEC §5.1, §10.9).

The worker is a subprocess (a native crash or a runaway allocation can only kill the child); ``StepContext.run_subprocess`` records its pid
so a crash of the app never leaves an orphan. ``DUOSKIN_MESH_INPROC=1`` runs the job in this process instead (fast tests; the same code).

``run_mesh_job`` never raises for a bad model: a crash, a timeout or an unreadable result is a ``MeshResult`` with ``ok=False`` and a failed
CHK-M01, exactly like ``mesh.worker.run_job``. ``Cancelled`` still propagates.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duoskin.engine.errors import StepFailure
from duoskin.mesh.types import MeshJob, MeshResult

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext

APP_ROOT = str(Path(__file__).resolve().parents[2])


def in_process() -> bool:
    return os.environ.get("DUOSKIN_MESH_INPROC", "") == "1"


def _failed(job: MeshJob, error: str, message: str) -> MeshResult:
    from duoskin.mesh import worker

    return worker._failed(job, error, message)


def run_mesh_job(ctx: StepContext | None, job: MeshJob, *, overrides: dict[str, Any] | None = None, timeout_s: float = 300.0) -> MeshResult:
    """Run ``job`` and return its ``MeshResult``. ``overrides`` are threshold overrides applied inside the worker."""
    from duoskin.checks import thresholds
    from duoskin.mesh import worker

    out = Path(job.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if in_process() or ctx is None:
        with thresholds.overrides(overrides or {}):
            return worker.execute(job)
    job_path = out / "job.json"
    job_path.write_text(job.model_dump_json(indent=1), encoding="utf-8")
    env = {"PYTHONPATH": APP_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "DUOSKIN_THRESHOLD_OVERRIDES": json.dumps(overrides or {})}
    try:
        res = ctx.run_subprocess([sys.executable, "-m", "duoskin.pipeline.meshproc", str(job_path)], timeout_s=timeout_s, env=env, cwd=APP_ROOT)
    except StepFailure as exc:
        if exc.kind == "timeout":
            return _failed(job, "timeout", f"The 3D worker took longer than {int(timeout_s)} seconds and was stopped. Try a smaller model.")
        raise
    if res["result"] is not None:
        try:
            return MeshResult.model_validate(res["result"])
        except ValueError as exc:
            return _failed(job, "bad_result", f"The 3D worker wrote an unreadable result ({exc}).")
    tail = (res["stderr"] or res["stdout"] or "")[-600:]
    return _failed(job, "worker_crashed", f"The 3D worker stopped unexpectedly (exit code {res['returncode']}). {tail}".strip())


def mesh_overrides_for(rt: Any, hair: bool = False) -> dict[str, Any]:
    """Threshold overrides a mesh job needs in this run (mock Tripo only; a real Tripo run has none).

    * The mock multiview images are procedural renders of the mock mesh that the downloaded model shows unlit, so the palette distance of the
      views is not a quality signal: ``acc.view_palette_de_max`` is loosened.
    * The mock Tripo hair is a thin cap with the grey head cube (and the views show the cube too), painted in the grey mean colour of its input:
      once ``hair.register`` cut the head out, the silhouettes and the thickness are not the real ones and the hair itself reads as the guide grey,
      so the view match, the thickness, the visible-face and the guide-colour share limits are loosened for hair (the registration facts still say
      whether the head was found and cut: the e2e test asserts them).
    The result of such a run is marked mock (every file name carries MOCK, CHK-E01 blocks it), so a loosened limit never reaches a real kit."""
    from duoskin.pipeline import common

    if not common.is_mock(rt, "tripo"):
        return {}
    out: dict[str, Any] = {"acc.view_palette_de_max": 45.0}
    if hair:
        out.update({"mesh.orient_iou_min": 0.5, "acc.front_iou_min": 0.5, "acc.view_iou_min": 0.5, "mesh.thickness_min": 0.005, "hair.front_face_visible_min": 0.3,
                    "hair.guide_texel_share_max": 1.0})
    return out
