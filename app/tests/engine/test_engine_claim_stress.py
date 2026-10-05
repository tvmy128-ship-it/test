"""ENG-13 / CHK-X07: many threads claiming steps never double-claim and never hit "database is locked"."""
from __future__ import annotations

import threading
import time

from duoskin.models.common import new_id, utcnow
from duoskin.models.job import Step, StepState


def _bulk_ready_steps(rt, job_id: str, n: int, pool: str = "cpu", kind: str = "t.stress") -> list[str]:
    now = utcnow()
    steps = [Step(id=new_id("stp"), job_id=job_id, kind=kind, pool=pool, state=StepState.READY, created_at=now,
                  priority=100) for _ in range(n)]
    rt.repo.insert_steps(steps)
    return [s.id for s in steps]


def _claim_all(rt, threads: int, per_thread_cap: int | None = None, pool: str = "cpu"):
    errors: list[BaseException] = []
    claimed: list[list[str]] = [[] for _ in range(threads)]
    barrier = threading.Barrier(threads)

    def worker(i: int) -> None:
        barrier.wait()
        while per_thread_cap is None or len(claimed[i]) < per_thread_cap:
            try:
                s = rt.ops.claim_one(pool, f"owner-{i}", emit=False)
            except BaseException as exc:   # noqa: BLE001
                errors.append(exc)
                return
            if s is None:
                return
            claimed[i].append(s.id)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    started = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    return claimed, errors, time.monotonic() - started


def test_8_threads_x_1000_claims_zero_double_claims_zero_lock_errors(rt):
    job = rt.scheduler.submit_job("parts", None, {})
    ids = _bulk_ready_steps(rt, job.id, 8000)
    claimed, errors, took = _claim_all(rt, 8)
    flat = [sid for per in claimed for sid in per]
    assert errors == []                                     # 0 lock errors
    assert len(flat) == len(set(flat)) == 8000              # 0 double claims, nothing left behind
    assert set(flat) == set(ids)
    assert all(len(per) > 0 for per in claimed)             # every thread really took part
    assert rt.ops.claim_one("cpu", "late", emit=False) is None
    # the database agrees: every row is running and its lease owner is the thread that claimed it
    owners = {sid: f"owner-{i}" for i, per in enumerate(claimed) for sid in per}
    rows = rt.db.conn().execute("SELECT id, state, lease_owner, json_extract(json,'$.attempt') AS a FROM steps").fetchall()
    assert all(r["state"] == "running" and r["lease_owner"] == owners[r["id"]] and r["a"] == 1 for r in rows)
    print(f"8000 claims by 8 threads in {took:.1f}s, lock retries: {rt.db.lock_retries}")


def test_concurrent_claims_while_other_threads_write(rt):
    """Claims and unrelated writes (events, new steps) interleave without errors."""
    job = rt.scheduler.submit_job("parts", None, {})
    _bulk_ready_steps(rt, job.id, 1500)
    stop = threading.Event()
    write_errors: list[BaseException] = []

    def writer() -> None:
        n = 0
        while not stop.is_set():
            try:
                rt.bus.emit("toast", {"n": n})
                n += 1
            except BaseException as exc:   # noqa: BLE001
                write_errors.append(exc)
                return

    w = threading.Thread(target=writer)
    w.start()
    claimed, errors, _ = _claim_all(rt, 6)
    stop.set()
    w.join(10)
    flat = [sid for per in claimed for sid in per]
    assert errors == [] and write_errors == []
    assert len(flat) == len(set(flat)) == 1500


def test_pool_and_exclusions_are_respected(rt):
    job = rt.scheduler.submit_job("parts", None, {})
    cpu = _bulk_ready_steps(rt, job.id, 3, pool="cpu", kind="k.cpu")
    api = _bulk_ready_steps(rt, job.id, 3, pool="api", kind="k.api")
    assert rt.ops.claim_one("proc", "x", emit=False) is None
    got = rt.ops.claim_one("api", "x", emit=False, exclude_kinds=["k.api"])
    assert got is None
    got = rt.ops.claim_one("api", "x", emit=False)
    assert got.id in api and got.kind == "k.api"
    assert rt.ops.claim_one("cpu", "x", emit=False).id in cpu


def test_not_before_in_the_future_is_not_claimable(rt):
    job = rt.scheduler.submit_job("parts", None, {})
    (sid,) = _bulk_ready_steps(rt, job.id, 1)
    from datetime import timedelta

    rt.repo.mutate_step(sid, lambda s: setattr(s, "not_before", utcnow() + timedelta(seconds=60)))
    assert rt.ops.claim_one("cpu", "x", emit=False) is None
    later = utcnow() + timedelta(seconds=61)
    assert rt.ops.claim_one("cpu", "x", emit=False, now=later).id == sid


def test_paused_projects_are_excluded(rt):
    from helpers_f import make_project

    p = make_project(rt)
    job = rt.scheduler.submit_job("parts", p.id, {})
    now = utcnow()
    rt.repo.insert_steps([Step(id=new_id("stp"), job_id=job.id, project_id=p.id, kind="k", pool="cpu", state=StepState.READY,
                               created_at=now)])
    assert rt.ops.claim_one("cpu", "x", emit=False, exclude_projects=[p.id]) is None
    assert rt.ops.claim_one("cpu", "x", emit=False) is not None
