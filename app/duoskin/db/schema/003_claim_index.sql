-- Additive migration (foundation track): the claim query orders by priority, created_at, id inside (state, pool).
-- With this index SQLite walks the queue in order and stops at the first claimable row instead of sorting every READY step.
CREATE INDEX steps_claim_order ON steps(state, pool, priority, created_at, id);
CREATE INDEX steps_job ON steps(job_id, state);
