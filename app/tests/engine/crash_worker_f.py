"""Run as a child process by test_engine_recovery: starts a runtime, runs two steps and crashes (os._exit) in the middle."""
import json
import os
import sys
import time
from pathlib import Path

home = Path(sys.argv[1])
counter = home / "calls.txt"


def note(text: str) -> None:
    with open(counter, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


from duoskin.engine.registry import register_handler
from duoskin.engine.runtime import Runtime


def remote_run(ctx, p, inputs):
    for _ in range(200):                     # wait until the other paid call is in flight too
        if counter.exists() and "paid-call" in counter.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    note("remote-submit")                    # the paid submit happens
    ctx.set_remote_ref("remote-xyz")         # ...its id is committed at once...
    os._exit(17)                             # ...and the process dies before it can return Pending


def paid_run(ctx, p, inputs):
    note("paid-call")                        # a paid call is in flight, no remote id exists
    time.sleep(20)                           # (the other step crashes the process while this one is still running)


register_handler("t.remote", remote_run, pool="api", paid=True, provider="tripo", estimate=lambda p: 1.1)
register_handler("t.paid", paid_run, pool="api", paid=True, provider="openai", estimate=lambda p: 0.5)

rt = Runtime.create(home, providers_mode="mock")
rt.startup(start_threads=True, backup=False)
job = rt.scheduler.submit_job("build", None, {})
a = rt.ops.new_step("t.remote", job_id=job.id)
b = rt.ops.new_step("t.paid", job_id=job.id)
rt.scheduler.spawn(job.id, [a, b])
print(json.dumps({"job": job.id, "remote": a.id, "paid": b.id}), flush=True)
time.sleep(30)
sys.exit(3)
