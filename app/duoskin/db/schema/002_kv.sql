-- Additive migration (foundation track): a tiny key/value table for the doctor report, instance info and similar
-- small facts that APP_SPEC §6.14 does not give a table of their own. 001_init.sql stays exactly as specified.
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
