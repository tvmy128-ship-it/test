"""Launcher of the mesh worker subprocess: ``python -m duoskin.pipeline.meshproc job.json``.

It does what ``python -m duoskin.mesh.worker`` does and one thing more: the threshold overrides of the parent (a calibrated or test value,
passed as JSON in ``DUOSKIN_THRESHOLD_OVERRIDES``) are applied inside the child, because the overrides live in a context variable and do not
cross a process boundary. The result goes to ``DUOSKIN_RESULT_JSON`` (the path ``StepContext.run_subprocess`` hands out), else next to the job.
"""
from __future__ import annotations

import faulthandler
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m duoskin.pipeline.meshproc job.json", file=sys.stderr)
        return 2
    faulthandler.enable()
    from duoskin.checks import thresholds
    from duoskin.mesh import worker
    from duoskin.mesh.types import MeshJob

    job = MeshJob.model_validate(json.loads(Path(argv[0]).read_text(encoding="utf-8")))
    overrides = json.loads(os.environ.get("DUOSKIN_THRESHOLD_OVERRIDES") or "{}")
    with thresholds.overrides(overrides):
        res = worker.execute(job)
    target = Path(os.environ.get("DUOSKIN_RESULT_JSON") or (job.result_path or str(Path(job.out_dir) / "result.json")))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(res.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
