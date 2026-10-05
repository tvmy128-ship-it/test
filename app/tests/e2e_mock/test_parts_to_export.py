from __future__ import annotations

import time

import pytest
from pfix import dump_steps, part_states, post_decision, wait_gate, wait_gate_checked


@pytest.mark.timeout(900)
def test_board_to_gate3(board):
    rt, client, p = board
    gate = wait_gate(rt, p.id, "part_board", timeout=30)
    r = post_decision(client, gate.id, "a.colours", "approve_all")
    assert r.status_code == 200, r.text
    t1 = time.time()
    try:
        g3 = wait_gate_checked(rt, p.id, "final_pick", timeout=600)
        print("gate3", time.time() - t1, part_states(rt, p.id), len(g3.tiles))
    finally:
        print(dump_steps(rt, p.id, states=("failed", "waiting_user", "waiting_remote")))
