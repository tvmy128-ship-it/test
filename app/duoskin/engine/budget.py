"""Estimates, reservations, the cost ledger and the BUDGET gate trigger (APP_SPEC §8.8, ENG-03, ENG-05).

Public interface (APP_SPEC §5.4)::

    reserve(project_id, est: Estimate, *, step_id=None, attempt=0) -> Reservation
    commit(res, actual: CostEntry) -> CostEntry | None
    release(res) -> None
    remaining(project_id) -> float
    check(project_id, est_usd, ...) -> BudgetCheck       # says whether the BUDGET gate must open

Ledger rules
------------
* One row per ``(step_id, attempt, operation)`` (unique index ``cost_once``): a duplicate ``add_entry`` is a no-op, so a
  retry or a double commit never records (or pays) twice. **A handler that makes several billed calls in one attempt must
  give each a distinct ``operation``** (``"images.edit:I2#3"``).
* ``reserved`` rows hold the estimate while a paid step runs; actual entries added with ``add_entry`` shrink that hold.
  ``settle_step`` closes what is left: released, committed at the estimate (call billed) or ``orphan`` (maybe billed).
* ``spent`` = committed + orphan rows; ``reserved`` = reserved rows; ``remaining = cap - spent - reserved``.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from duoskin.db.db import Database
from duoskin.engine.bus import EventBus
from duoskin.models.common import iso_utc, new_id, utcnow
from duoskin.models.cost import Budget, CostEntry, Estimate, Reservation
from duoskin.models.job import Step
from duoskin.models.settings import Settings

log = logging.getLogger("duoskin.budget")

NO_CAP_USD = 1e9
TRIPO_USD_PER_CREDIT = 0.01      # [third-party] 1 credit ~ $0.01
LOW_THRESHOLDS = (0.20, 0.05)    # budget.low events when the remaining share drops below these


def _r6(x: float) -> float:
    return round(float(x), 6)


class BudgetCheck:
    """Result of ``BudgetService.check``: ``ok`` or the reason the BUDGET gate must open (``cap``, ``ask``, ``credits``)."""

    def __init__(self, ok: bool, reason: str | None, est_usd: float, remaining: float, cap: float, ask_above: float,
                 spent: float, credits_available: float | None = None) -> None:
        self.ok = ok
        self.reason = reason
        self.est_usd = est_usd
        self.remaining = remaining
        self.cap = cap
        self.ask_above = ask_above
        self.spent = spent
        self.credits_available = credits_available

    def facts(self) -> dict[str, Any]:
        return {"reason": self.reason, "estimate_usd": self.est_usd, "remaining_usd": self.remaining,
                "cap_usd": self.cap, "ask_above_usd": self.ask_above, "spent_usd": self.spent,
                "tripo_credits_available": self.credits_available}


class BudgetService:
    def __init__(self, db: Database, bus: EventBus, settings: Callable[[], Settings],
                 project_loader: Callable[[str], Any | None]) -> None:
        self.db = db
        self.bus = bus
        self._settings = settings
        self._project = project_loader
        self._lock = threading.Lock()
        self._low_sent: set[tuple[str, float]] = set()

    # ------------------------------------------------------------------------------------------------ queries
    def _sum(self, project_id: str | None, states: tuple[str, ...]) -> float:
        marks = ",".join("?" for _ in states)
        if project_id is None:
            sql, args = f"SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE state IN ({marks})", list(states)
        else:
            sql, args = f"SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE project_id=? AND state IN ({marks})", [project_id, *states]
        return float(self.db.conn().execute(sql, args).fetchone()[0])

    def spent(self, project_id: str | None) -> float:
        return _r6(self._sum(project_id, ("committed", "orphan")))

    def reserved(self, project_id: str | None) -> float:
        return _r6(self._sum(project_id, ("reserved",)))

    def tripo_available_credits(self) -> float | None:
        row = self.db.conn().execute(
            "SELECT json FROM cost_ledger WHERE provider='tripo' AND state='committed' ORDER BY ts DESC, id DESC LIMIT 20").fetchall()
        for r in row:
            after = json.loads(r["json"]).get("balance_after")
            if isinstance(after, (int, float)):
                return float(after)
        return None

    def budget(self, project_id: str | None) -> Budget:
        s = self._settings()
        cap, ask = s.budgets.per_duo_usd, s.budgets.ask_above_usd
        if project_id is None:
            cap = NO_CAP_USD
        else:
            p = self._project(project_id)
            if p is not None:
                cap, ask = p.settings.budget_usd, p.settings.ask_above_usd
        return Budget(project_id=project_id, cap_usd=cap, spent_usd=self.spent(project_id),
                      reserved_usd=self.reserved(project_id), ask_above_usd=ask,
                      tripo_available_credits=self.tripo_available_credits())

    def remaining(self, project_id: str | None) -> float:
        return self.budget(project_id).remaining

    def spent_today(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.conn().execute(
            "SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE state IN ('committed','orphan') AND ts >= ? AND ts < ?",
            (iso_utc(start), iso_utc(start + timedelta(days=1)))).fetchone()
        return _r6(row[0])

    def daily_cap_reached(self) -> bool:
        cap = self._settings().budgets.daily_cap_usd
        return cap is not None and self.spent_today() >= cap

    def check(self, project_id: str | None, est_usd: float, *, budget_ok: bool = False, provider: str | None = None) -> BudgetCheck:
        """Would a paid step with this estimate need the BUDGET gate? ``budget_ok`` = the user already said continue."""
        b = self.budget(project_id)
        credits = b.tripo_available_credits if provider == "tripo" else None
        base = {"est_usd": est_usd, "remaining": b.remaining, "cap": b.cap_usd, "ask_above": b.ask_above_usd,
                "spent": b.spent_usd, "credits_available": credits}
        if budget_ok:
            return BudgetCheck(True, None, **base)
        if est_usd > b.remaining + 1e-9:
            return BudgetCheck(False, "cap", **base)
        if credits is not None and est_usd / TRIPO_USD_PER_CREDIT > credits + 1e-9:
            return BudgetCheck(False, "credits", **base)
        if est_usd > b.ask_above_usd + 1e-9:
            return BudgetCheck(False, "ask", **base)
        return BudgetCheck(True, None, **base)

    # ------------------------------------------------------------------------------------------------ reservations
    def check_and_reserve(self, project_id: str | None, est: Estimate, *, step_id: str | None = None, attempt: int = 0,
                          budget_ok: bool = False) -> tuple[BudgetCheck, Reservation | None]:
        """``check`` and ``reserve`` as one atomic step (``BEGIN IMMEDIATE``). Without this two steps could both see room
        for the last dollar and together cross the cap. Returns the check and, when it is OK, the reservation."""
        with self.db.tx():
            chk = self.check(project_id, est.usd, budget_ok=budget_ok, provider=est.provider)
            if not chk.ok:
                return chk, None
            return chk, self.reserve(project_id, est, step_id=step_id, attempt=attempt)

    def reserve(self, project_id: str | None, est: Estimate | float, *, step_id: str | None = None, attempt: int = 0) -> Reservation:
        if not isinstance(est, Estimate):
            est = Estimate(usd=float(est))
        row_id = new_id("cst")
        entry = CostEntry(id=row_id, ts=utcnow(), project_id=project_id, step_id=step_id, attempt=attempt,
                          provider=est.provider, model="", operation="reserve", usd=_r6(est.usd), credits=est.credits,
                          basis="estimate", state="reserved", price_table=est.price_table)
        with self.db.tx() as c:
            # a reservation for the same (step, attempt) replaces an earlier one (a crash left it behind)
            if step_id is not None:
                c.execute("DELETE FROM cost_ledger WHERE step_id=? AND attempt=? AND operation='reserve'", (step_id, attempt))
            self._insert(c, entry)
        return Reservation(id=row_id, project_id=project_id, step_id=step_id, attempt=attempt, usd=entry.usd)

    @staticmethod
    def _insert(c: Any, e: CostEntry) -> int:
        cur = c.execute(
            "INSERT INTO cost_ledger (id, project_id, step_id, attempt, operation, provider, state, usd, credits, request_id, json, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(step_id, attempt, operation) WHERE step_id IS NOT NULL DO NOTHING",
            (e.id, e.project_id, e.step_id, e.attempt, e.operation, e.provider, e.state, _r6(e.usd), e.credits,
             e.request_id, e.model_dump_json(), iso_utc(e.ts)))
        return cur.rowcount

    def release(self, res: Reservation) -> None:
        self._set_state(res.id, "released", usd=0.0)

    def commit(self, res: Reservation, actual: CostEntry) -> CostEntry | None:
        """Turn the reservation into the real cost. Returns None when ``actual`` was already recorded (ENG-03)."""
        with self.db.tx() as c:
            c.execute("DELETE FROM cost_ledger WHERE id=?", (res.id,))
            return self.add_entry(actual.model_copy(update={"project_id": actual.project_id or res.project_id,
                                                            "step_id": actual.step_id or res.step_id,
                                                            "attempt": actual.attempt or res.attempt}))

    def _set_state(self, row_id: str, state: str, *, usd: float | None = None, basis: str | None = None) -> None:
        with self.db.tx() as c:
            row = c.execute("SELECT json FROM cost_ledger WHERE id=?", (row_id,)).fetchone()
            if row is None:
                return
            data = json.loads(row["json"])
            data["state"] = state
            if usd is not None:
                data["usd"] = _r6(usd)
            if basis is not None:
                data["basis"] = basis
            c.execute("UPDATE cost_ledger SET state=?, usd=?, json=? WHERE id=?",
                      (state, _r6(data["usd"]), json.dumps(data), row_id))

    # ------------------------------------------------------------------------------------------------ entries
    def add_entry(self, entry: CostEntry) -> CostEntry | None:
        """Record an actual cost. Idempotent per ``(step_id, attempt, operation)``; returns None for a duplicate."""
        if not entry.id:
            entry = entry.model_copy(update={"id": new_id("cst")})
        entry = entry.model_copy(update={"usd": _r6(entry.usd)})
        with self.db.tx() as c:
            if not self._insert(c, entry):
                return None
            if entry.step_id is not None and entry.state in ("committed", "orphan"):
                c.execute(
                    "UPDATE cost_ledger SET usd=MAX(usd-?,0), json=json_set(json,'$.usd',MAX(usd-?,0)) "
                    "WHERE step_id=? AND attempt=? AND state='reserved'",
                    (entry.usd, entry.usd, entry.step_id, entry.attempt))
            if entry.project_id is not None and entry.state in ("committed", "orphan"):
                spent = _r6(float(c.execute(
                    "SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE project_id=? AND state IN ('committed','orphan')",
                    (entry.project_id,)).fetchone()[0]))
                c.execute("UPDATE projects SET json=json_set(json,'$.spent_usd',?) WHERE id=?", (spent, entry.project_id))
        if entry.state in ("committed", "orphan"):
            self.bus.emit("cost.added", {"project_id": entry.project_id, "step_id": entry.step_id, "usd": entry.usd,
                                         "provider": entry.provider, "operation": entry.operation,
                                         "basis": entry.basis, "entry_id": entry.id}, entry.project_id)
            self._maybe_low(entry.project_id)
        return entry

    def _maybe_low(self, project_id: str | None) -> None:
        if project_id is None:
            return
        b = self.budget(project_id)
        if b.cap_usd <= 0 or b.cap_usd >= NO_CAP_USD:
            return
        share = b.remaining / b.cap_usd
        for threshold in LOW_THRESHOLDS:
            key = (project_id, threshold)
            if share < threshold:
                with self._lock:
                    if key in self._low_sent:
                        continue
                    self._low_sent.add(key)
                self.bus.emit("budget.low", {"project_id": project_id, "remaining_usd": _r6(b.remaining),
                                             "cap_usd": b.cap_usd, "threshold": threshold}, project_id)
            else:
                with self._lock:
                    self._low_sent.discard(key)

    def settle_step(self, step: Step, *, outcome: str, billed: str = "unknown") -> None:
        """Close the reservation of ``step``'s current attempt after it ended.

        ``outcome``: ``success`` | ``failed`` | ``cancelled`` | ``waiting`` (nothing to settle yet). ``billed`` is the
        error's ``billed`` flag (``no`` | ``yes`` | ``unknown``)."""
        if outcome == "waiting":
            return
        with self.db.tx() as c:
            rows = c.execute("SELECT id, usd FROM cost_ledger WHERE step_id=? AND attempt=? AND state='reserved'",
                             (step.id, step.attempt)).fetchall()
            recorded = c.execute(
                "SELECT COUNT(*) FROM cost_ledger WHERE step_id=? AND attempt=? AND state IN ('committed','orphan')",
                (step.id, step.attempt)).fetchone()[0]
            for r in rows:
                hold = float(r["usd"])
                if outcome == "success":
                    new_state = "released" if recorded or hold <= 0 else "committed"
                elif billed == "no":
                    new_state = "released"
                elif billed == "yes":
                    new_state = "released" if recorded or hold <= 0 else "committed"
                else:
                    new_state = "released" if hold <= 0 else "orphan"
                basis = "estimate" if new_state == "committed" else ("orphan" if new_state == "orphan" else None)
                self._set_state(r["id"], new_state, usd=0.0 if new_state == "released" else hold, basis=basis)
        if step.project_id:
            self._refresh_spent(step.project_id)

    def mark_orphan(self, step_id: str, attempt: int) -> int:
        """Recovery: the call may have been billed before the crash. Returns how many reservations became orphans."""
        n = 0
        with self.db.tx() as c:
            for r in c.execute("SELECT id FROM cost_ledger WHERE step_id=? AND attempt=? AND state='reserved'",
                               (step_id, attempt)).fetchall():
                self._set_state(r["id"], "orphan", basis="orphan")
                n += 1
        return n

    def _refresh_spent(self, project_id: str) -> None:
        spent = self.spent(project_id)
        with self.db.tx() as c:
            c.execute("UPDATE projects SET json=json_set(json,'$.spent_usd',?) WHERE id=?", (spent, project_id))

    # ------------------------------------------------------------------------------------------------ reports
    def ledger(self, project_id: str | None = None, ts_from: str | None = None, ts_to: str | None = None,
               limit: int = 1000) -> list[CostEntry]:
        sql, args = "SELECT json FROM cost_ledger WHERE state != 'released'", []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if ts_from:
            sql += " AND ts >= ?"
            args.append(ts_from)
        if ts_to:
            sql += " AND ts <= ?"
            args.append(ts_to)
        rows = self.db.conn().execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
        return [CostEntry.model_validate_json(r["json"]) for r in rows]

    def totals(self, project_id: str | None = None, ts_from: str | None = None, ts_to: str | None = None) -> dict[str, Any]:
        sql, args = "SELECT provider, state, COALESCE(SUM(usd),0) AS usd, COUNT(*) AS n FROM cost_ledger WHERE 1=1", []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if ts_from:
            sql += " AND ts >= ?"
            args.append(ts_from)
        if ts_to:
            sql += " AND ts <= ?"
            args.append(ts_to)
        by_provider: dict[str, float] = {}
        by_state: dict[str, float] = {}
        for r in self.db.conn().execute(sql + " GROUP BY provider, state", args).fetchall():
            by_state[r["state"]] = by_state.get(r["state"], 0.0) + float(r["usd"])
            if r["state"] in ("committed", "orphan"):
                by_provider[r["provider"]] = by_provider.get(r["provider"], 0.0) + float(r["usd"])
        return {"by_provider": {k: _r6(v) for k, v in sorted(by_provider.items())},
                "by_state": {k: _r6(v) for k, v in sorted(by_state.items())},
                "spent_usd": _r6(sum(by_provider.values())), "reserved_usd": _r6(by_state.get("reserved", 0.0)),
                "orphan_usd": _r6(by_state.get("orphan", 0.0)), "today_usd": self.spent_today()}
