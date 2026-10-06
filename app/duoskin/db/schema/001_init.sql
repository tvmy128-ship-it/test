PRAGMA foreign_keys = ON;
CREATE TABLE projects    (id TEXT PRIMARY KEY, json TEXT NOT NULL, stage TEXT NOT NULL, updated_at TEXT NOT NULL,
                          version INTEGER NOT NULL DEFAULT 0) STRICT;
CREATE TABLE specs       (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), plan_set_id TEXT NOT NULL,
                          version INTEGER NOT NULL, status TEXT NOT NULL, sha256 TEXT NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE dna_cards   (spec_id TEXT NOT NULL, version INTEGER NOT NULL, project_id TEXT NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL, PRIMARY KEY (spec_id, version)) STRICT;
CREATE TABLE parts       (project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, state TEXT NOT NULL,
                          json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (project_id, id)) STRICT;
CREATE TABLE assets      (sha256 TEXT PRIMARY KEY, pixel_sha TEXT, kind TEXT NOT NULL, mime TEXT NOT NULL,
                          bytes INTEGER NOT NULL, width INTEGER, height INTEGER, tris INTEGER, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE asset_links (id TEXT PRIMARY KEY, asset_sha TEXT NOT NULL REFERENCES assets(sha256), project_id TEXT,
                          part_id TEXT, step_id TEXT, role TEXT NOT NULL, status TEXT NOT NULL, stream TEXT NOT NULL,
                          rank INTEGER, json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE INDEX asset_links_part ON asset_links(project_id, part_id, role);
CREATE TABLE jobs        (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL,
                          json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE TABLE steps       (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), project_id TEXT, part_id TEXT,
                          kind TEXT NOT NULL, state TEXT NOT NULL, pool TEXT NOT NULL, priority INTEGER NOT NULL,
                          not_before TEXT, lease_until TEXT, lease_owner TEXT, remote_ref TEXT, cache_key TEXT,
                          paid INTEGER NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE INDEX steps_claim ON steps(state, pool, priority, not_before);
CREATE TABLE step_deps   (step_id TEXT NOT NULL, dep_id TEXT NOT NULL, PRIMARY KEY (step_id, dep_id)) STRICT;
CREATE TABLE cache       (cache_key TEXT PRIMARY KEY, step_kind TEXT NOT NULL, outputs TEXT NOT NULL, result TEXT NOT NULL,
                          created_at TEXT NOT NULL, last_hit_at TEXT) STRICT;
CREATE TABLE checks      (id TEXT PRIMARY KEY, project_id TEXT, step_id TEXT, subject_sha TEXT NOT NULL,
                          check_id TEXT NOT NULL, kind TEXT NOT NULL, passed INTEGER NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE gates       (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                          state TEXT NOT NULL, json TEXT NOT NULL, opened_at TEXT NOT NULL, decided_at TEXT) STRICT;
CREATE TABLE decisions   (id TEXT PRIMARY KEY, gate_id TEXT NOT NULL REFERENCES gates(id), tile_id TEXT NOT NULL,
                          action TEXT NOT NULL, client_decision_id TEXT NOT NULL UNIQUE, json TEXT NOT NULL,
                          decided_at TEXT NOT NULL) STRICT;
CREATE TABLE approvals   (project_id TEXT NOT NULL, part_id TEXT NOT NULL,
                          stamp TEXT NOT NULL CHECK (stamp IN ('approval', 'build')),     -- two stamps (APP_SPEC 9.7)
                          stamp_hash TEXT NOT NULL,
                          decision_id TEXT NOT NULL, valid INTEGER NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL,
                          PRIMARY KEY (project_id, part_id, stamp, stamp_hash)) STRICT;
CREATE TABLE changes     (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE cost_ledger (id TEXT PRIMARY KEY, project_id TEXT, step_id TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                          operation TEXT NOT NULL, provider TEXT NOT NULL, state TEXT NOT NULL, usd REAL NOT NULL,
                          credits REAL, request_id TEXT, json TEXT NOT NULL, ts TEXT NOT NULL) STRICT;
CREATE UNIQUE INDEX cost_once ON cost_ledger(step_id, attempt, operation) WHERE step_id IS NOT NULL;   -- ENG-03
CREATE TABLE events      (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, project_id TEXT, type TEXT NOT NULL,
                          payload TEXT NOT NULL) STRICT;
CREATE TABLE registry_face  (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, part_role TEXT NOT NULL, asset_sha TEXT NOT NULL, pixel_sha TEXT NOT NULL,
                             phash TEXT NOT NULL, embedding BLOB, json TEXT NOT NULL, duo_seq INTEGER NOT NULL,
                             listed INTEGER NOT NULL DEFAULT 0, registered_at TEXT NOT NULL) STRICT;
CREATE TABLE registry_print (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, part_role TEXT NOT NULL, asset_sha TEXT NOT NULL, pixel_sha TEXT NOT NULL,
                             phash TEXT NOT NULL, embedding BLOB, json TEXT NOT NULL, duo_seq INTEGER NOT NULL,
                             listed INTEGER NOT NULL DEFAULT 0, registered_at TEXT NOT NULL) STRICT;
CREATE TABLE duo_memory  (project_id TEXT PRIMARY KEY, embedding BLOB, json TEXT NOT NULL, approved_at TEXT NOT NULL) STRICT;
CREATE TABLE labels      (id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, json TEXT NOT NULL, ts TEXT NOT NULL) STRICT;
CREATE TABLE check_stats (check_id TEXT NOT NULL, window_start TEXT NOT NULL, json TEXT NOT NULL,
                          PRIMARY KEY (check_id, window_start)) STRICT;
CREATE TABLE inbox       (path TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, state TEXT NOT NULL, assigned_part TEXT,
                          json TEXT NOT NULL, seen_at TEXT NOT NULL) STRICT;
CREATE TABLE child_procs (pid INTEGER NOT NULL, create_time REAL NOT NULL, step_id TEXT, started_at TEXT NOT NULL,
                          PRIMARY KEY (pid, create_time)) STRICT;
