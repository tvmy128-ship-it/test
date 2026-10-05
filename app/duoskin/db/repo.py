"""Typed CRUD over the SQLite tables (APP_SPEC §5.4 ``db.repo``). One ``Repo`` per ``Database``.

JSON columns hold ``model_dump_json()`` output; the indexed columns next to them are kept in sync by ``save_*``.
Optimistic locks: ``save_project``, ``save_part`` and tile versions raise ``ConflictError`` (HTTP 409) when the stored
``version`` differs from ``expected_version``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from pydantic import BaseModel

from duoskin.checks.model import CheckResult
from duoskin.db.db import Database
from duoskin.db.errors import ConflictError, NotFound
from duoskin.models.asset import AssetLink
from duoskin.models.common import iso_utc, new_id, utcnow
from duoskin.models.gate import ChangeRequest, Gate, GateDecision
from duoskin.models.job import Job, Step
from duoskin.models.part import ApprovalRecord, Part
from duoskin.models.project import Project, Stage, slugify
from duoskin.models.spec_record import SpecRecord

M = TypeVar("M", bound=BaseModel)

_SHA_RE = re.compile(r"[0-9a-f]{64}")


def shas_in(text: str) -> set[str]:
    """Every 64-hex string that appears in ``text`` (used by GC to find references inside JSON blobs)."""
    return set(_SHA_RE.findall(text))


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ----------------------------------------------------------------------------------------------- projects
    def create_project(self, p: Project) -> Project:
        with self.db.tx() as c:
            c.execute("INSERT INTO projects (id, json, stage, updated_at, version) VALUES (?,?,?,?,?)",
                      (p.id, p.model_dump_json(), p.stage.value, iso_utc(p.updated_at), p.version))
        return p

    def find_project(self, project_id: str) -> Project | None:
        row = self.db.conn().execute("SELECT json FROM projects WHERE id=?", (project_id,)).fetchone()
        return Project.model_validate_json(row["json"]) if row else None

    def get_project(self, project_id: str) -> Project:
        p = self.find_project(project_id)
        if p is None:
            raise NotFound("project", project_id)
        return p

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        rows = self.db.conn().execute("SELECT json FROM projects ORDER BY updated_at DESC, rowid DESC").fetchall()
        out = [Project.model_validate_json(r["json"]) for r in rows]
        return out if include_archived else [p for p in out if not p.archived]

    def unique_slug(self, name: str) -> str:
        base = slugify(name)
        existing = {r[0] for r in self.db.conn().execute("SELECT json_extract(json,'$.slug') FROM projects").fetchall()}
        slug, n = base, 1
        while slug in existing:
            n += 1
            suffix = f"-{n}"
            slug = base[: 41 - len(suffix)].rstrip("-") + suffix
        return slug

    def save_project(self, p: Project, expected_version: int | None = None) -> Project:
        """Write ``p`` and bump ``version``. Pass ``expected_version`` for a user edit (409 on mismatch)."""
        with self.db.tx() as c:
            row = c.execute("SELECT version FROM projects WHERE id=?", (p.id,)).fetchone()
            if row is None:
                raise NotFound("project", p.id)
            if expected_version is not None and int(row["version"]) != expected_version:
                raise ConflictError("the project changed since you loaded it", self.get_project(p.id))
            new = p.model_copy(update={"version": int(row["version"]) + 1, "updated_at": utcnow()})
            c.execute("UPDATE projects SET json=?, stage=?, updated_at=?, version=? WHERE id=?",
                      (new.model_dump_json(), new.stage.value, iso_utc(new.updated_at), new.version, new.id))
        return new

    def mutate_project(self, project_id: str, fn: Callable[[Project], None], *, bump: bool = True) -> Project:
        """Read-modify-write under ``BEGIN IMMEDIATE``. ``bump=False`` leaves ``version`` alone (system bookkeeping)."""
        with self.db.tx() as c:
            p = self.get_project(project_id)
            fn(p)
            if bump:
                p = p.model_copy(update={"version": p.version + 1, "updated_at": utcnow()})
            else:
                p = p.model_copy(update={"updated_at": utcnow()})
            c.execute("UPDATE projects SET json=?, stage=?, updated_at=?, version=? WHERE id=?",
                      (p.model_dump_json(), p.stage.value, iso_utc(p.updated_at), p.version, p.id))
        return p

    def set_project_stage(self, project_id: str, stage: Stage, *, bus: Any = None) -> Project:
        before = self.get_project(project_id).stage
        p = self.mutate_project(project_id, lambda pr: setattr(pr, "stage", stage))
        if bus is not None and before != stage:
            bus.emit("project.stage", {"project_id": project_id, "stage": stage.value, "previous": before.value},
                     project_id)
        return p

    # ----------------------------------------------------------------------------------------------- specs
    def add_spec(self, rec: SpecRecord) -> SpecRecord:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO specs (id, project_id, plan_set_id, version, status, sha256, json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rec.id, rec.project_id, rec.plan_set_id, rec.version, rec.status, rec.sha256, rec.model_dump_json(),
                 iso_utc(utcnow())))
        return rec

    def get_spec(self, spec_id: str) -> SpecRecord:
        row = self.db.conn().execute("SELECT json FROM specs WHERE id=?", (spec_id,)).fetchone()
        if row is None:
            raise NotFound("spec", spec_id)
        return SpecRecord.model_validate_json(row["json"])

    def list_specs(self, project_id: str) -> list[SpecRecord]:
        rows = self.db.conn().execute("SELECT json FROM specs WHERE project_id=? ORDER BY created_at, rowid",
                                      (project_id,)).fetchall()
        return [SpecRecord.model_validate_json(r["json"]) for r in rows]

    def set_spec_status(self, spec_id: str, status: str) -> SpecRecord:
        with self.db.tx() as c:
            rec = self.get_spec(spec_id).model_copy(update={"status": status})
            c.execute("UPDATE specs SET status=?, json=? WHERE id=?", (status, rec.model_dump_json(), spec_id))
        return rec

    def save_dna_card(self, spec_id: str, version: int, project_id: str, card: dict[str, Any]) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO dna_cards (spec_id, version, project_id, json, created_at) VALUES (?,?,?,?,?)",
                      (spec_id, version, project_id, json.dumps(card, ensure_ascii=False), iso_utc(utcnow())))

    def get_dna_card(self, spec_id: str, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            row = self.db.conn().execute("SELECT json FROM dna_cards WHERE spec_id=? ORDER BY version DESC LIMIT 1",
                                         (spec_id,)).fetchone()
        else:
            row = self.db.conn().execute("SELECT json FROM dna_cards WHERE spec_id=? AND version=?",
                                         (spec_id, version)).fetchone()
        return json.loads(row["json"]) if row else None

    def list_dna_cards(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.conn().execute("SELECT json FROM dna_cards WHERE project_id=? ORDER BY created_at, version",
                                      (project_id,)).fetchall()
        return [json.loads(r["json"]) for r in rows]

    # ----------------------------------------------------------------------------------------------- parts
    def find_part(self, project_id: str, part_id: str) -> Part | None:
        row = self.db.conn().execute("SELECT json FROM parts WHERE project_id=? AND id=?", (project_id, part_id)).fetchone()
        return Part.model_validate_json(row["json"]) if row else None

    def get_part(self, project_id: str, part_id: str) -> Part:
        p = self.find_part(project_id, part_id)
        if p is None:
            raise NotFound("part", f"{project_id}/{part_id}")
        return p

    def list_parts(self, project_id: str) -> list[Part]:
        rows = self.db.conn().execute("SELECT json FROM parts WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [Part.model_validate_json(r["json"]) for r in rows]

    def save_part(self, p: Part, expected_version: int | None = None) -> Part:
        """Insert (``expected_version=None`` and the part is new) or update with an optimistic lock (409)."""
        with self.db.tx() as c:
            row = c.execute("SELECT version FROM parts WHERE project_id=? AND id=?", (p.project_id, p.id)).fetchone()
            if row is None:
                new = p.model_copy(update={"version": 0 if expected_version is None else expected_version})
                c.execute("INSERT INTO parts (project_id, id, state, json, version) VALUES (?,?,?,?,?)",
                          (new.project_id, new.id, new.state.value, new.model_dump_json(), new.version))
                return new
            if expected_version is not None and int(row["version"]) != expected_version:
                raise ConflictError("the part changed since you loaded it", self.get_part(p.project_id, p.id))
            new = p.model_copy(update={"version": int(row["version"]) + 1})
            c.execute("UPDATE parts SET state=?, json=?, version=? WHERE project_id=? AND id=?",
                      (new.state.value, new.model_dump_json(), new.version, new.project_id, new.id))
        return new

    def mutate_part(self, project_id: str, part_id: str, fn: Callable[[Part], None]) -> Part:
        with self.db.tx():
            p = self.get_part(project_id, part_id)
            fn(p)
            return self.save_part(p)

    # ----------------------------------------------------------------------------------------------- asset links
    def list_links(self, project_id: str | None = None, part_id: str | None = None, role: str | None = None,
                   asset_sha: str | None = None) -> list[AssetLink]:
        sql, args = "SELECT json FROM asset_links WHERE 1=1", []
        for col, val in (("project_id", project_id), ("part_id", part_id), ("role", role), ("asset_sha", asset_sha)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        rows = self.db.conn().execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [AssetLink.model_validate_json(r["json"]) for r in rows]

    def set_link_status(self, link_id: str, status: str) -> None:
        with self.db.tx() as c:
            row = c.execute("SELECT json FROM asset_links WHERE id=?", (link_id,)).fetchone()
            if row is None:
                raise NotFound("asset link", link_id)
            link = AssetLink.model_validate_json(row["json"]).model_copy(update={"status": status})
            c.execute("UPDATE asset_links SET status=?, json=? WHERE id=?", (status, link.model_dump_json(), link_id))

    # ----------------------------------------------------------------------------------------------- jobs
    def insert_job(self, job: Job) -> Job:
        with self.db.tx() as c:
            c.execute("INSERT INTO jobs (id, project_id, kind, state, json, created_at) VALUES (?,?,?,?,?,?)",
                      (job.id, job.project_id, job.kind.value, job.state.value, job.model_dump_json(),
                       iso_utc(job.created_at)))
        return job

    def find_job(self, job_id: str) -> Job | None:
        row = self.db.conn().execute("SELECT json FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row["json"]) if row else None

    def get_job(self, job_id: str) -> Job:
        j = self.find_job(job_id)
        if j is None:
            raise NotFound("job", job_id)
        return j

    def save_job(self, job: Job) -> Job:
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET project_id=?, kind=?, state=?, json=? WHERE id=?",
                      (job.project_id, job.kind.value, job.state.value, job.model_dump_json(), job.id))
        return job

    def list_jobs(self, project_id: str | None = None, state: str | None = None, limit: int = 200) -> list[Job]:
        sql, args = "SELECT json FROM jobs WHERE 1=1", []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if state is not None:
            sql += " AND state=?"
            args.append(state)
        rows = self.db.conn().execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        return [Job.model_validate_json(r["json"]) for r in rows]

    # ----------------------------------------------------------------------------------------------- steps
    @staticmethod
    def _step_row(s: Step) -> tuple[Any, ...]:
        return (s.id, s.job_id, s.project_id, s.part_id, s.kind, s.state.value, s.pool, s.priority,
                iso_utc(s.not_before) if s.not_before else None, iso_utc(s.lease_until) if s.lease_until else None,
                s.lease_owner, s.remote_ref, s.cache_key, 1 if s.paid else 0, s.model_dump_json(), iso_utc(s.created_at))

    def insert_steps(self, steps: Iterable[Step]) -> None:
        steps = list(steps)
        with self.db.tx() as c:
            c.executemany(
                "INSERT INTO steps (id, job_id, project_id, part_id, kind, state, pool, priority, not_before, lease_until,"
                " lease_owner, remote_ref, cache_key, paid, json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._step_row(s) for s in steps])
            deps = [(s.id, d) for s in steps for d in s.deps]
            if deps:
                c.executemany("INSERT OR IGNORE INTO step_deps (step_id, dep_id) VALUES (?,?)", deps)

    def find_step(self, step_id: str) -> Step | None:
        row = self.db.conn().execute("SELECT json FROM steps WHERE id=?", (step_id,)).fetchone()
        return Step.model_validate_json(row["json"]) if row else None

    def get_step(self, step_id: str) -> Step:
        s = self.find_step(step_id)
        if s is None:
            raise NotFound("step", step_id)
        return s

    def save_step(self, s: Step) -> Step:
        with self.db.tx() as c:
            c.execute(
                "UPDATE steps SET job_id=?, project_id=?, part_id=?, kind=?, state=?, pool=?, priority=?, not_before=?,"
                " lease_until=?, lease_owner=?, remote_ref=?, cache_key=?, paid=?, json=? WHERE id=?",
                (*self._step_row(s)[1:15], s.id))
        return s

    def mutate_step(self, step_id: str, fn: Callable[[Step], None], *, guard: Callable[[Step], bool] | None = None) -> Step | None:
        """Read-modify-write under ``BEGIN IMMEDIATE``. ``guard`` returns False -> nothing is written and None returned."""
        with self.db.tx():
            s = self.get_step(step_id)
            if guard is not None and not guard(s):
                return None
            fn(s)
            return self.save_step(s)

    def list_steps(self, *, job_id: str | None = None, project_id: str | None = None, state: str | None = None,
                   limit: int = 1000) -> list[Step]:
        sql, args = "SELECT json FROM steps WHERE 1=1", []
        for col, val in (("job_id", job_id), ("project_id", project_id), ("state", state)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        rows = self.db.conn().execute(sql + " ORDER BY created_at, rowid LIMIT ?", (*args, limit)).fetchall()
        return [Step.model_validate_json(r["json"]) for r in rows]

    def step_counts(self, *, job_id: str | None = None, project_id: str | None = None) -> dict[str, int]:
        sql, args = "SELECT state, COUNT(*) AS n FROM steps WHERE 1=1", []
        if job_id is not None:
            sql += " AND job_id=?"
            args.append(job_id)
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        return {r["state"]: int(r["n"]) for r in self.db.conn().execute(sql + " GROUP BY state", args).fetchall()}

    # ----------------------------------------------------------------------------------------------- gates
    def insert_gate(self, g: Gate) -> Gate:
        with self.db.tx() as c:
            c.execute("INSERT INTO gates (id, project_id, job_id, kind, state, json, opened_at, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                      (g.id, g.project_id, g.job_id, g.kind.value, g.state, g.model_dump_json(), iso_utc(g.opened_at),
                       iso_utc(g.decided_at) if g.decided_at else None))
        return g

    def find_gate(self, gate_id: str) -> Gate | None:
        row = self.db.conn().execute("SELECT json FROM gates WHERE id=?", (gate_id,)).fetchone()
        return Gate.model_validate_json(row["json"]) if row else None

    def get_gate(self, gate_id: str) -> Gate:
        g = self.find_gate(gate_id)
        if g is None:
            raise NotFound("gate", gate_id)
        return g

    def save_gate(self, g: Gate) -> Gate:
        with self.db.tx() as c:
            c.execute("UPDATE gates SET state=?, json=?, decided_at=? WHERE id=?",
                      (g.state, g.model_dump_json(), iso_utc(g.decided_at) if g.decided_at else None, g.id))
        return g

    def list_gates(self, project_id: str | None = None, state: str | None = None) -> list[Gate]:
        sql, args = "SELECT json FROM gates WHERE 1=1", []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if state is not None:
            sql += " AND state=?"
            args.append(state)
        rows = self.db.conn().execute(sql + " ORDER BY opened_at, rowid", args).fetchall()
        return [Gate.model_validate_json(r["json"]) for r in rows]

    def insert_decision(self, d: GateDecision) -> GateDecision:
        with self.db.tx() as c:
            c.execute("INSERT INTO decisions (id, gate_id, tile_id, action, client_decision_id, json, decided_at) VALUES (?,?,?,?,?,?,?)",
                      (d.id, d.gate_id, d.tile_id, d.action.value, d.client_decision_id, d.model_dump_json(),
                       iso_utc(d.decided_at)))
        return d

    def save_decision(self, d: GateDecision) -> GateDecision:
        with self.db.tx() as c:
            c.execute("UPDATE decisions SET json=? WHERE id=?", (d.model_dump_json(), d.id))
        return d

    def find_decision(self, decision_id: str) -> GateDecision | None:
        row = self.db.conn().execute("SELECT json FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return GateDecision.model_validate_json(row["json"]) if row else None

    def find_decision_by_client_id(self, client_decision_id: str) -> GateDecision | None:
        row = self.db.conn().execute("SELECT json FROM decisions WHERE client_decision_id=?", (client_decision_id,)).fetchone()
        return GateDecision.model_validate_json(row["json"]) if row else None

    def delete_decision(self, decision_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM decisions WHERE id=?", (decision_id,))

    def list_decisions(self, gate_id: str) -> list[GateDecision]:
        rows = self.db.conn().execute("SELECT json FROM decisions WHERE gate_id=? ORDER BY decided_at, rowid", (gate_id,)).fetchall()
        return [GateDecision.model_validate_json(r["json"]) for r in rows]

    # ----------------------------------------------------------------------------------------------- approvals
    def upsert_approval(self, project_id: str, rec: ApprovalRecord, *, valid: bool = True) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO approvals (project_id, part_id, approval_hash, decision_id, valid, json, created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id, part_id, approval_hash) DO UPDATE SET "
                "decision_id=excluded.decision_id, valid=excluded.valid, json=excluded.json",
                (project_id, rec.part_id, rec.approval_hash, rec.decision_id, 1 if valid else 0, rec.model_dump_json(),
                 iso_utc(rec.approved_at)))

    def invalidate_approvals(self, project_id: str, part_id: str) -> int:
        with self.db.tx() as c:
            cur = c.execute("UPDATE approvals SET valid=0 WHERE project_id=? AND part_id=? AND valid=1", (project_id, part_id))
            return cur.rowcount

    def valid_approval(self, project_id: str, part_id: str) -> ApprovalRecord | None:
        row = self.db.conn().execute(
            "SELECT json FROM approvals WHERE project_id=? AND part_id=? AND valid=1 ORDER BY created_at DESC LIMIT 1",
            (project_id, part_id)).fetchone()
        return ApprovalRecord.model_validate_json(row["json"]) if row else None

    # ----------------------------------------------------------------------------------------------- changes
    def save_change(self, ch: ChangeRequest) -> ChangeRequest:
        with self.db.tx() as c:
            c.execute("INSERT INTO changes (id, project_id, status, json, created_at) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET status=excluded.status, json=excluded.json",
                      (ch.id, ch.project_id, ch.status, ch.model_dump_json(), iso_utc(utcnow())))
        return ch

    def get_change(self, change_id: str) -> ChangeRequest:
        row = self.db.conn().execute("SELECT json FROM changes WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise NotFound("change", change_id)
        return ChangeRequest.model_validate_json(row["json"])

    # ----------------------------------------------------------------------------------------------- checks
    def insert_checks(self, results: Iterable[CheckResult], *, project_id: str | None, step_id: str | None) -> list[str]:
        ids: list[str] = []
        now = iso_utc(utcnow())
        with self.db.tx() as c:
            for r in results:
                row_id = new_id("chk")
                c.execute("INSERT INTO checks (id, project_id, step_id, subject_sha, check_id, kind, passed, json, created_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?)",
                          (row_id, project_id, step_id, r.subject_sha, r.check_id, r.kind, 1 if r.passed else 0,
                           r.model_dump_json(), now))
                ids.append(row_id)
        return ids

    def list_checks(self, *, step_id: str | None = None, project_id: str | None = None,
                    subject_sha: str | None = None) -> list[tuple[str, CheckResult]]:
        sql, args = "SELECT id, json FROM checks WHERE 1=1", []
        for col, val in (("step_id", step_id), ("project_id", project_id), ("subject_sha", subject_sha)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        rows = self.db.conn().execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [(r["id"], CheckResult.model_validate_json(r["json"])) for r in rows]

    # ----------------------------------------------------------------------------------------------- labels, kv, procs
    def insert_label(self, kind: str, source: str, subject_ids: list[str], value: Any) -> str:
        label_id = new_id("lbl")
        payload = {"id": label_id, "kind": kind, "source": source, "subject_ids": subject_ids, "value": value}
        with self.db.tx() as c:
            c.execute("INSERT INTO labels (id, kind, source, json, ts) VALUES (?,?,?,?,?)",
                      (label_id, kind, source, json.dumps(payload, ensure_ascii=False), iso_utc(utcnow())))
        return label_id

    def kv_get(self, key: str) -> Any | None:
        row = self.db.conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                      "value=excluded.value, updated_at=excluded.updated_at",
                      (key, json.dumps(value, ensure_ascii=False, default=str), iso_utc(utcnow())))

    def add_child_proc(self, pid: int, create_time: float, step_id: str | None) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO child_procs (pid, create_time, step_id, started_at) VALUES (?,?,?,?)",
                      (pid, create_time, step_id, iso_utc(utcnow())))

    def remove_child_proc(self, pid: int, create_time: float) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM child_procs WHERE pid=? AND create_time=?", (pid, create_time))

    def list_child_procs(self) -> list[sqlite3.Row]:
        return self.db.conn().execute("SELECT pid, create_time, step_id, started_at FROM child_procs").fetchall()
