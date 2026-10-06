from __future__ import annotations

import os
import time

from pfix import dump_steps, make_project, part_states, wait_for


def test_parts_smoke(rt, client):
    p, rec = make_project(rt)
    from duoskin.pipeline import parts

    only = os.environ.get("SMOKE_PARTS", "a.face").split(",")
    parts.ensure_parts(rt, p.id, rec.spec)
    t0 = time.time()
    parts.start_parts_job(rt, p.id, only=only)
    try:
        wait_for(lambda: all(part_states(rt, p.id).get(i) in ("ready", "needs_human", "failed") for i in only), timeout=100, message="parts")
    finally:
        print(dump_steps(rt, p.id))
        for st in rt.repo.list_steps(project_id=p.id, limit=500):
            if st.kind == "img.gate_a":
                for e in st.result.get("per", []):
                    if not e.get("hard_ok"):
                        print("GATE_A FAIL", st.params["loop"]["role"], e.get("fails"), [r for r in e.get("results", []) if not r["passed"]])
        print({k: v for k, v in part_states(rt, p.id).items() if k in only}, time.time() - t0)
        for i in only:
            pt = rt.repo.get_part(p.id, i)
            print(i, pt.flags, sorted(pt.board_assets), rt.repo.kv_get(f"tile:{p.id}:{i}"))


def test_dump_failures(rt, client):
    pass
