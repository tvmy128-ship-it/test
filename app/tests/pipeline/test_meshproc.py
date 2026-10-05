"""The mesh worker subprocess launcher: ``python -m duoskin.pipeline.meshproc job.json`` (threshold overrides cross the process boundary)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from duoskin.mesh.types import MeshJob, MeshResult
from duoskin.pipeline import kits, meshrun


def run_child(tmp_path: Path, overrides: dict) -> MeshResult:
    job = MeshJob(op="validate", input_path=str(kits.DEMO_DIR / "hair" / "hair_short_crop_01" / "mesh.glb"), out_dir=str(tmp_path / "out"), asset_type="Hair",   # type: ignore[arg-type]
                  attachment="HairAttachment", target_studs=(3.0, 5.0, 3.5), tris_target=3600, texture_px=1024, params={}, asset_id="a.hair", licence="n/a")   # type: ignore[arg-type]
    (tmp_path / "job.json").write_text(job.model_dump_json(), encoding="utf-8")
    res_path = tmp_path / "result.json"
    env = {**os.environ, "PYTHONPATH": meshrun.APP_ROOT, "DUOSKIN_THRESHOLD_OVERRIDES": json.dumps(overrides), "DUOSKIN_RESULT_JSON": str(res_path)}
    done = subprocess.run([sys.executable, "-m", "duoskin.pipeline.meshproc", str(tmp_path / "job.json")], env=env, timeout=180, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()[-600:]
    return MeshResult.model_validate_json(res_path.read_text(encoding="utf-8"))


def check(res: MeshResult, cid: str):
    return next(c for c in res.checks if c.check_id == cid)


def test_the_child_writes_its_result_where_the_parent_asked(tmp_path):
    res = run_child(tmp_path, {})
    assert res.ok and check(res, "CHK-M14").passed and check(res, "CHK-M03").passed


def test_threshold_overrides_of_the_parent_apply_inside_the_child(tmp_path):
    res = run_child(tmp_path, {"mesh.gap_max": -1.0})                    # no mesh can be closer to the body than a negative distance
    m14 = check(res, "CHK-M14")
    assert not m14.passed and "floats" in m14.evidence


def test_the_launcher_needs_exactly_one_job_file():
    done = subprocess.run([sys.executable, "-m", "duoskin.pipeline.meshproc"], env={**os.environ, "PYTHONPATH": meshrun.APP_ROOT}, capture_output=True, timeout=60, check=False)
    assert done.returncode == 2 and b"usage" in done.stderr
