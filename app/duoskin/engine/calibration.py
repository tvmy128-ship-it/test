"""Calibration and the weekly learning loop (APP_SPEC §3.1, §3.7, §3.8, §3.9, §6.13; FAILURE_MODES ENG-10 and T10).

What is here
------------
* **Labels** (``labels`` table): ``log_label`` / ``list_labels`` / ``label_counts``. Gate decisions and warning overrides are logged
  with ``source="gate"`` by the gate service (``on_decision``); drills log ``source="drill"``; the judge sets ``source="calibration"``.
  **Drill labels are at most 25% of the calibration set** (``calib.drill_share_max``): ``drill_weight`` down-weights them when they
  exceed it and ``drill_room`` tells the drill session how many more fit under the cap.
* **Check statistics** (``check_stats``, one row per check and ISO week): how often a check flagged a duo the person APPROVED, how
  often it flagged one they REJECTED, how often it was shown and how often the person overrode it. ``*gate:<kind>`` rows hold the
  denominators (the tile decisions of that gate kind).
* **``warning_visibility(check_id)``**: a warning overridden in more than 25% of its last 20 showings hides itself until it is re-tuned
  (``gate.warning_override_hide``, ``calib.override_window``; never on fewer than ``calib.warning_min_showings`` showings).
* **``weekly_report()``**: per check the flag rate on approved duos and the catch rate on rejected ones, the hidden and demoted
  checks, the HARD reject rate on approved duos (alert above 10%), structure use, the wildcard pick rate, Gate 1 first-try approval,
  nearest-duo distances, the registry and plan-lint reject rates, cost per duo, the failure causes by FAILURE_MODES id, the labels and
  the revisit triggers of the deferred "12 concepts" upgrade.
* **Demotion**: a demotable HARD check whose fire rate is above ``calib.demote_fire_rate`` (30%) is switched to SOFT in
  ``settings.checks.check_overrides``. Checks of the classes roblox, ip, stray_text, security and integrity are never demoted.
* **The threshold tuner** (``tune_thresholds``): returns *proposals*, never applies anything. It only cuts the bad tail: a bound sits
  at about the 5th percentile (``calib.tail_percentile``) of the approved duos (the 95th for an upper bound), after at least
  ``calib.target_labels`` labels and at least ``calib.tuner_min_values`` approved values per threshold. There is no target value
  anywhere. ``accept_proposals`` is a separate, explicit step that the variety guard must have passed (``pipeline/regression.py``).

Everything takes a *handle*: a ``Runtime`` (anything with ``.db``), a ``Database``, or nothing (the default database).
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np

from duoskin.checks import policy
from duoskin.checks import thresholds as TH
from duoskin.db.db import Database
from duoskin.db.db import default as default_database
from duoskin.models.common import iso_utc, new_id, parse_iso, utcnow
from duoskin.models.registry import CheckStat, Label

log = logging.getLogger("duoskin.calibration")

#: hidden projects made by the regression job carry this prefix: they are never counted as the person's duos
REGRESSION_PREFIX = "[regression] "
LABEL_KINDS = ("clone_real_stranger", "like_dislike", "rule_verdict", "warning_override")
LABEL_SOURCES = ("gate", "drill", "calibration")
DRILL_ANSWERS = ("clone", "real_duo", "strangers")
JUDGE_ANSWERS = ("pass", "fail")
APPROVING = frozenset({"approve", "approve_all", "pick"})            # a tile decision that keeps the thing
REJECTING = frozenset({"reimagine", "new_plan", "back_to_concept"})   # a tile decision that throws it away ("change" is neither)
RECENT_PREFIX = "calib:recent:"                                       # kv: the outcomes (1 = overridden) of a check's last showings
ACTIVE_KEY = "calib:thresholds:active"
HISTORY_KEY = "calib:thresholds:history"
PROPOSALS_KEY = "calib:proposals:latest"
DEMOTIONS_KEY = "calib:demotions"
GATE_STAGES = {"gate1": ("concept",), "G0": ("concept",), "gate2": ("part_board",), "gate3": ("final_pick",)}
ALL_GATES = ("concept", "part_board", "final_pick")


def _thr(name: str) -> Any:
    return TH.get(name)


def _database(handle: Any = None) -> Database:
    if handle is None:
        return default_database()
    inner = getattr(handle, "db", None)
    return inner if inner is not None else handle


def _repo(handle: Any = None):
    from duoskin.db.repo import Repo

    return getattr(handle, "repo", None) or Repo(_database(handle))


def is_regression_name(name: str) -> bool:
    return str(name or "").startswith(REGRESSION_PREFIX)


# ======================================================================================================================
# approved duos
# ======================================================================================================================
def approved_duo_ids(handle: Any = None) -> list[str]:
    """Projects whose duo was picked at Gate 3 (they have a ``duo_memory`` row), oldest first; regression projects never count."""
    rows = _database(handle).conn().execute(
        "SELECT m.project_id AS pid FROM duo_memory m JOIN projects p ON p.id = m.project_id "
        "WHERE json_extract(p.json, '$.name') NOT LIKE ? ORDER BY m.approved_at, m.rowid", (REGRESSION_PREFIX + "%",)).fetchall()
    return [r["pid"] for r in rows]


def approved_duo_count(handle: Any = None) -> int:
    return len(approved_duo_ids(handle))


# ======================================================================================================================
# labels
# ======================================================================================================================
def log_label(handle: Any, kind: str, source: str, subject_ids: Sequence[str], value: Any) -> str:
    """Store one label (APP_SPEC §6.13) and return its id. ``kind`` and ``source`` are checked against the closed lists."""
    if kind not in LABEL_KINDS:
        raise ValueError(f"unknown label kind {kind!r}")
    if source not in LABEL_SOURCES:
        raise ValueError(f"unknown label source {source!r}")
    return _repo(handle).insert_label(kind, source, [str(s) for s in subject_ids], value)


def list_labels(handle: Any = None, *, kind: str | None = None, source: str | None = None, limit: int | None = None) -> list[Label]:
    """Labels, oldest first (``limit`` keeps the newest ones)."""
    sql, args = "SELECT json, ts FROM labels WHERE 1=1", []
    if kind is not None:
        sql += " AND kind=?"
        args.append(kind)
    if source is not None:
        sql += " AND source=?"
        args.append(source)
    rows = _database(handle).conn().execute(sql + " ORDER BY ts, rowid", args).fetchall()
    out: list[Label] = []
    for r in rows:
        try:
            doc = json.loads(r["json"])
            out.append(Label(id=doc["id"], kind=doc["kind"], source=doc["source"], subject_ids=list(doc.get("subject_ids", [])),
                             value=doc.get("value"), ts=parse_iso(r["ts"])))
        except (ValueError, KeyError):
            continue
    return out[-limit:] if limit else out


def drill_room(n_drill: int, n_other: int, cap: float | None = None) -> int:
    """How many more drill labels fit under the cap at full weight (drill share of the whole set <= ``calib.drill_share_max``)."""
    share = float(cap if cap is not None else _thr("calib.drill_share_max"))
    if share >= 1.0:
        return 10 ** 9
    allowed = math.floor(n_other * share / (1.0 - share) + 1e-9)
    return max(0, allowed - n_drill)


def drill_weight(n_drill: int, n_other: int, cap: float | None = None) -> float:
    """The weight each drill label gets in the calibration set: 1 under the cap, else just enough to keep their effective share at the cap
    (APP_SPEC §3.8: "down-weights drill labels when they exceed 25%")."""
    if n_drill <= 0:
        return 1.0
    share = float(cap if cap is not None else _thr("calib.drill_share_max"))
    if share >= 1.0:
        return 1.0
    allowed = n_other * share / (1.0 - share)
    return max(0.0, min(1.0, allowed / n_drill))


@dataclass
class LabelCounts:
    by_source: dict[str, int]
    by_kind: dict[str, int]
    total: int
    drill: int
    other: int
    drill_share: float                  # raw share of drill labels
    drill_weight: float                 # weight each drill label gets
    effective_total: float              # the size of the calibration set after down-weighting
    effective_drill_share: float
    drill_room: int                     # more drill labels that fit at full weight
    cap: float
    target: int                         # about this many labels before the [DES] values are re-tuned

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cap_reached"] = self.drill_room <= 0
        d["progress"] = min(1.0, self.effective_total / self.target) if self.target else 1.0
        return d


def label_counts(handle: Any = None) -> LabelCounts:
    conn = _database(handle).conn()
    by_source = {s: 0 for s in LABEL_SOURCES}
    by_kind = {k: 0 for k in LABEL_KINDS}
    for r in conn.execute("SELECT source, kind, COUNT(*) AS n FROM labels GROUP BY source, kind").fetchall():
        by_source[r["source"]] = by_source.get(r["source"], 0) + int(r["n"])
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + int(r["n"])
    drill = by_source.get("drill", 0)
    other = sum(n for s, n in by_source.items() if s != "drill")
    total = drill + other
    cap = float(_thr("calib.drill_share_max"))
    w = drill_weight(drill, other, cap)
    eff = other + w * drill
    return LabelCounts(by_source, by_kind, total, drill, other, drill / total if total else 0.0, w, eff,
                       (w * drill) / eff if eff else 0.0, drill_room(drill, other, cap), cap, int(_thr("calib.target_labels")))


def weighted_labels(handle: Any = None, **filters: Any) -> list[tuple[Label, float]]:
    """Every label with the weight it has in the calibration set (drill labels carry ``drill_weight``)."""
    counts = label_counts(handle)
    return [(lab, counts.drill_weight if lab.source == "drill" else 1.0) for lab in list_labels(handle, **filters)]


# ======================================================================================================================
# check statistics
# ======================================================================================================================
def week_start(when: datetime | None = None) -> str:
    """The Monday (UTC) of the ISO week, ``YYYY-MM-DD``: the ``window_start`` of ``check_stats``."""
    d = (when or utcnow()).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def iso_week_label(window_start: str) -> str:
    y, w, _ = datetime.fromisoformat(window_start).isocalendar()
    return f"{y}-W{w:02d}"


def parse_week(text: str | None) -> str | None:
    """``"2026-W41"`` or a date (any day of the week) to the Monday ``YYYY-MM-DD``; ``None``/``"all"`` to ``None``."""
    if not text or str(text).lower() in ("all", "any"):
        return None
    t = str(text).strip()
    m = re.fullmatch(r"(\d{4})-?W(\d{1,2})", t, re.IGNORECASE)
    if m:
        return datetime.fromisocalendar(int(m.group(1)), int(m.group(2)), 1).date().isoformat()
    return week_start(datetime.fromisoformat(t))


def _load_stat(conn, check_id: str, window: str) -> CheckStat:
    row = conn.execute("SELECT json FROM check_stats WHERE check_id=? AND window_start=?", (check_id, window)).fetchone()
    if row is None:
        return CheckStat(check_id=check_id, window_start=window)
    return CheckStat.model_validate_json(row["json"])


def bump(handle: Any, check_id: str, *, window: str | None = None, **inc: int) -> CheckStat:
    """Add to the counters of one check in one week (created on first use)."""
    window = window or week_start()
    db = _database(handle)
    with db.tx() as c:
        stat = _load_stat(c, check_id, window)
        data = stat.model_dump()
        for k, v in inc.items():
            data[k] = int(data[k]) + int(v)
        stat = CheckStat(**data)
        c.execute("INSERT INTO check_stats (check_id, window_start, json) VALUES (?,?,?) ON CONFLICT(check_id, window_start) "
                  "DO UPDATE SET json=excluded.json", (check_id, window, stat.model_dump_json()))
    return stat


def stat_rows(handle: Any = None, check_id: str | None = None, *, window: str | None = None) -> list[CheckStat]:
    sql, args = "SELECT json FROM check_stats WHERE 1=1", []
    if check_id is not None:
        sql += " AND check_id=?"
        args.append(check_id)
    if window is not None:
        sql += " AND window_start=?"
        args.append(window)
    return [CheckStat.model_validate_json(r["json"]) for r in _database(handle).conn().execute(sql + " ORDER BY window_start", args).fetchall()]


def _sum(rows: Iterable[CheckStat]) -> dict[str, int]:
    out = Counter()
    for r in rows:
        for k in ("flagged_approved", "flagged_rejected", "shown", "overridden", "approved_total", "rejected_total"):
            out[k] += getattr(r, k)
    return {k: out[k] for k in ("flagged_approved", "flagged_rejected", "shown", "overridden", "approved_total", "rejected_total")}


def check_id_of(warning_id: str) -> str:
    """The check id inside a warning id. Tiles name warnings ``<part>:<check>`` (``a.face:F_ZONES``), ``<check>:<char>``
    (``CHK-G1-11:a``), ``duo:<check>``, ``change:<check>`` or just ``<check>``."""
    parts = [p for p in str(warning_id).split(":") if p]
    known = [p for p in parts if policy.is_registered(p)]
    if known:
        return max(known, key=len)
    shaped = [p for p in parts if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*([_-][A-Za-z0-9]+)+", p) and "." not in p]
    return shaped[-1] if shaped else (parts[-1] if parts else "")


def _gate_kinds_for(check_id: str) -> tuple[str, ...]:
    return GATE_STAGES.get(policy.meta(check_id).stage, ALL_GATES)


def totals_for(handle: Any, check_id: str, *, window: str | None = None) -> dict[str, int]:
    """Counters of one check (one week, or all time) with the decision totals of the gates the check can apply to."""
    own = _sum(stat_rows(handle, check_id, window=window))
    denom = _sum(r for kind in _gate_kinds_for(check_id) for r in stat_rows(handle, f"*gate:{kind}", window=window))
    own["approved_total"], own["rejected_total"] = denom["approved_total"], denom["rejected_total"]
    return own


def flag_rate(handle: Any, check_id: str, *, window: str | None = None) -> float:
    t = totals_for(handle, check_id, window=window)
    return t["flagged_approved"] / t["approved_total"] if t["approved_total"] else 0.0


def catch_rate(handle: Any, check_id: str, *, window: str | None = None) -> float:
    """How often the check flagged a tile the person REJECTED (the ranking key for the (<= 2) warnings a gate shows)."""
    t = totals_for(handle, check_id, window=window)
    return t["flagged_rejected"] / t["rejected_total"] if t["rejected_total"] else 0.0


def outcome_of(action: str) -> Literal["approved", "rejected"] | None:
    a = str(getattr(action, "value", action))
    return "approved" if a in APPROVING else "rejected" if a in REJECTING else None


def flagged_check_ids(facts: Mapping[str, Any]) -> list[str]:
    """The checks that flagged a tile: its SOFT warnings (all of them, shown or not) and its HARD failures."""
    ids: list[str] = []
    for w in facts.get("warnings") or []:
        if isinstance(w, Mapping) and w.get("id"):
            ids.append(check_id_of(str(w["id"])))
    for hf in facts.get("hard_failures") or []:
        ids.append(check_id_of(str(hf.get("id", "")) if isinstance(hf, Mapping) else str(hf)))
    return list(dict.fromkeys(i for i in ids if i))


def record_gate_outcome(handle: Any, gate_kind: str, outcome: str, flagged: Iterable[str], *, when: datetime | None = None) -> None:
    """One tile decision: add to the gate's denominators and to every flagging check's ``flagged_approved`` or ``flagged_rejected``."""
    window = week_start(when)
    side = "approved" if outcome == "approved" else "rejected"
    with _database(handle).tx():
        bump(handle, f"*gate:{gate_kind}", window=window, **{f"{side}_total": 1})
        for cid in dict.fromkeys(flagged):
            bump(handle, cid, window=window, **{f"flagged_{side}": 1})


def record_showings(handle: Any, outcomes: Sequence[tuple[str, bool]], *, when: datetime | None = None) -> None:
    """Warnings that were shown after a first choice: ``(check_id, overridden)``. Keeps the last ``calib.override_window`` outcomes per check."""
    if not outcomes:
        return
    window = week_start(when)
    keep = int(_thr("calib.override_window"))
    repo = _repo(handle)
    with _database(handle).tx():
        for cid, overridden in outcomes:
            bump(handle, cid, window=window, shown=1, overridden=1 if overridden else 0)
            recent = [int(x) for x in (repo.kv_get(RECENT_PREFIX + cid) or [])]
            recent = (recent + [1 if overridden else 0])[-keep:]
            repo.kv_set(RECENT_PREFIX + cid, recent)


# ======================================================================================================================
# warning visibility (auto-hide)
# ======================================================================================================================
@dataclass(frozen=True)
class WarningVisibility:
    check_id: str
    visible: bool
    showings: int           # outcomes in the window (at most ``calib.override_window``)
    overridden: int
    override_rate: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def warning_visibility(check_id: str, rt: Any = None) -> WarningVisibility:
    """Is this warning still shown? It hides itself when it was overridden in MORE than ``gate.warning_override_hide`` (25%) of its last
    ``calib.override_window`` (20) showings, and only once it has been shown ``calib.warning_min_showings`` times. It stays hidden until
    the check is re-tuned (``reset_recent``): a hidden warning is never shown, so it can never earn a better record by itself."""
    recent = [int(x) for x in (_repo(rt).kv_get(RECENT_PREFIX + check_id) or [])][-int(_thr("calib.override_window")):]
    n, over = len(recent), sum(recent)
    rate = over / n if n else 0.0
    limit = float(_thr("gate.warning_override_hide"))
    if n >= int(_thr("calib.warning_min_showings")) and rate > limit:
        return WarningVisibility(check_id, False, n, over, rate, f"overridden {over} of its last {n} showings ({rate:.0%}, above {limit:.0%})")
    return WarningVisibility(check_id, True, n, over, rate, "shown" if n else "not shown yet")


def hidden_warnings(handle: Any = None) -> list[WarningVisibility]:
    rows = _database(handle).conn().execute("SELECT key FROM kv WHERE key LIKE ?", (RECENT_PREFIX + "%",)).fetchall()
    out = [warning_visibility(r["key"][len(RECENT_PREFIX):], handle) for r in rows]
    return sorted((v for v in out if not v.visible), key=lambda v: v.check_id)


def reset_recent(handle: Any, check_ids: Iterable[str]) -> int:
    """Forget the showing history of these checks (they were re-tuned): hidden warnings come back."""
    repo = _repo(handle)
    n = 0
    for cid in check_ids:
        if repo.kv_get(RECENT_PREFIX + cid):
            repo.kv_set(RECENT_PREFIX + cid, [])
            n += 1
    return n


def annotate_warning(handle: Any, warning: Mapping[str, Any]) -> dict[str, Any]:
    """A tile warning with its calibrated ``catch_rate`` and ``visible`` flag (what ``GateService`` ranks and releases). Never raises."""
    out = dict(warning)
    try:
        cid = check_id_of(str(warning.get("id", "")))
        out["check_id"] = cid
        out["catch_rate"] = max(float(warning.get("catch_rate") or 0.0), catch_rate(handle, cid))
        if not warning_visibility(cid, handle).visible:
            out["visible"] = False
    except Exception:
        log.exception("could not annotate warning %s", warning.get("id"))
    return out


# ======================================================================================================================
# gate hooks (called by engine.gates.GateService)
# ======================================================================================================================
@dataclass
class DecisionSnapshot:
    """What a gate decision looked at, taken BEFORE the applier changes the tiles."""
    gate_id: str
    gate_kind: str
    project_id: str
    tile_ids: list[str]
    flagged: dict[str, list[str]]       # tile id -> check ids that flagged it
    outcome: str | None


def snapshot(gate: Any, decision: Any) -> DecisionSnapshot:
    """Which tiles a decision approves or rejects and which checks flagged them (``approve_all`` approves every READY tile without a hard failure)."""
    action = str(getattr(decision.action, "value", decision.action))
    kind = str(getattr(gate.kind, "value", gate.kind))
    if action == "approve_all":
        tiles = [t for t in gate.tiles if str(getattr(t.state, "value", t.state)) == "ready" and not t.facts.get("hard_failures")]
    else:
        tiles = [t for t in gate.tiles if t.tile_id == decision.tile_id]
    return DecisionSnapshot(gate.id, kind, gate.project_id, [t.tile_id for t in tiles], {t.tile_id: flagged_check_ids(t.facts) for t in tiles},
                            outcome_of(action))


def on_decision(handle: Any, snap: DecisionSnapshot, decision: Any) -> None:
    """A final gate decision: log the real choice as a label, update the flag counters and record how the shown warnings fared.

    ``decision.warnings_shown`` / ``warnings_overridden`` say which of the (<= 2) released warnings the person overrode. Overrides are
    already logged as ``warning_override`` labels by ``GateService.confirm``; this adds the statistics."""
    if snap.gate_kind not in ALL_GATES:
        return
    shown = list(decision.warnings_shown or [])
    overridden = set(decision.warnings_overridden or [])
    record_showings(handle, [(check_id_of(w), w in overridden) for w in shown])
    if snap.outcome is None:
        return
    for tile_id in snap.tile_ids:
        record_gate_outcome(handle, snap.gate_kind, snap.outcome, snap.flagged.get(tile_id, []))
    log_label(handle, "like_dislike", "gate", [snap.project_id, snap.gate_id, decision.tile_id, decision.id],
              {"liked": snap.outcome == "approved", "action": str(getattr(decision.action, "value", decision.action)), "gate": snap.gate_kind,
               "tiles": len(snap.tile_ids)})


def on_withdrawn(handle: Any, decision: Any) -> None:
    """The person went back instead of approving anyway: every warning that was shown counts as heeded (not overridden)."""
    record_showings(handle, [(check_id_of(w), False) for w in decision.warnings_shown or []])


# ======================================================================================================================
# fire rates and demotion
# ======================================================================================================================
def fire_rates(handle: Any = None) -> dict[str, tuple[int, int]]:
    """``check_id -> (duos where it failed at least once, duos where it ran)``. A duo is a project; regression projects, ``not_applicable``
    and ``not_run`` results are left out (a check that could not run is not a miscalibrated one)."""
    rows = _database(handle).conn().execute(
        "SELECT c.check_id AS cid, c.project_id AS pid, MIN(c.passed) AS ok FROM checks c JOIN projects p ON p.id = c.project_id "
        "WHERE json_extract(p.json, '$.name') NOT LIKE ? AND json_extract(c.json, '$.status') IN ('passed', 'failed') "
        "GROUP BY c.check_id, c.project_id", (REGRESSION_PREFIX + "%",)).fetchall()
    ran: Counter[str] = Counter()
    fired: Counter[str] = Counter()
    for r in rows:
        ran[r["cid"]] += 1
        fired[r["cid"]] += 0 if r["ok"] else 1
    return {cid: (fired[cid], n) for cid, n in ran.items()}


@dataclass(frozen=True)
class Demotion:
    check_id: str
    title: str
    policy_class: str
    fire_rate: float
    duos: int
    banner: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _banner(meta: policy.CheckMeta, rate: float) -> str:
    return (f"\"{meta.title or meta.check_id}\" flagged {rate:.0%} of your duos, so it was moved back to a warning. "
            "You can turn it back on in Settings, under Checks.")


def demotions_due(handle: Any = None, *, overrides: Mapping[str, str] | None = None) -> list[Demotion]:
    """Demotable HARD checks whose fire rate is above ``calib.demote_fire_rate`` over at least ``calib.demote_min_duos`` duos. Roblox, IP,
    stray-text, security and integrity checks are never listed, whatever their rate."""
    limit = float(_thr("calib.demote_fire_rate"))
    need = int(_thr("calib.demote_min_duos"))
    current = dict(overrides or {})
    out: list[Demotion] = []
    for cid, (fired, ran) in sorted(fire_rates(handle).items()):
        meta = policy.meta(cid)
        if not policy.is_registered(cid) or cid in current or meta.policy_class in policy.NEVER_DEMOTABLE or not policy.can_demote(cid):
            continue
        if ran >= need and fired / ran > limit:
            out.append(Demotion(cid, meta.title, meta.policy_class, fired / ran, ran, _banner(meta, fired / ran)))
    return out


def apply_demotions(rt: Any) -> list[Demotion]:
    """Switch every due check to SOFT in ``settings.checks.check_overrides`` and remember why (the banner reads ``active_demotions``)."""
    current = dict(rt.settings.checks.check_overrides)
    due = demotions_due(rt, overrides=current)
    if not due:
        return []
    rt.update_settings({"checks": {"check_overrides": {d.check_id: "soft" for d in due}}})
    history = list(rt.repo.kv_get(DEMOTIONS_KEY) or [])
    stamp = iso_utc(utcnow())
    history += [{**d.as_dict(), "at": stamp} for d in due]
    rt.repo.kv_set(DEMOTIONS_KEY, history)
    for d in due:
        log.warning("check %s demoted to a warning: fire rate %.0f%% over %d duos", d.check_id, d.fire_rate * 100, d.duos)
    return due


def active_demotions(rt: Any) -> list[dict[str, Any]]:
    """Checks that are SOFT now because of a demotion, with the reason that was recorded (the banner of Settings and the Learning page)."""
    overrides = dict(rt.settings.checks.check_overrides)
    latest: dict[str, dict[str, Any]] = {}
    for h in rt.repo.kv_get(DEMOTIONS_KEY) or []:
        latest[h["check_id"]] = h
    out = []
    for cid in sorted(overrides):
        meta = policy.meta(cid)
        row = latest.get(cid)
        out.append({"check_id": cid, "title": meta.title, "policy_class": meta.policy_class, "fire_rate": (row or {}).get("fire_rate"),
                    "duos": (row or {}).get("duos"), "at": (row or {}).get("at"),
                    "banner": (row or {}).get("banner") or f"\"{meta.title or cid}\" is set to warn only."})
    return out


def restore_check(rt: Any, check_id: str) -> None:
    """The person turns a demoted check back on: the override goes, and so does its showing history."""
    rt.update_settings({"checks": {"check_overrides": {check_id: None}}})
    reset_recent(rt, [check_id])


# ======================================================================================================================
# the weekly report
# ======================================================================================================================
def _approved_specs(rt: Any, limit: int = 200) -> list[dict[str, Any]]:
    from duoskin.pipeline import brief as BR

    return BR.approved_specs(rt, limit=limit)


def per_check_rows(rt: Any, window: str | None) -> list[dict[str, Any]]:
    """One row per check that has statistics: flag rate on approved duos, catch rate on rejected ones, shown, overridden, hidden, demoted."""
    ids = sorted({r.check_id for r in stat_rows(rt) if not r.check_id.startswith("*")})
    fires = fire_rates(rt)
    overrides = dict(rt.settings.checks.check_overrides)
    rows: list[dict[str, Any]] = []
    for cid in ids:
        meta = policy.meta(cid)
        t_week = totals_for(rt, cid, window=window)
        t_all = totals_for(rt, cid)
        vis = warning_visibility(cid, rt)
        fired, ran = fires.get(cid, (0, 0))
        rows.append({
            "check_id": cid, "title": meta.title or cid, "kind": policy.effective_kind(cid, overrides), "policy_class": meta.policy_class,
            "flag_rate_on_approved": t_week["flagged_approved"] / t_week["approved_total"] if t_week["approved_total"] else 0.0,
            "catch_rate_on_rejected": t_week["flagged_rejected"] / t_week["rejected_total"] if t_week["rejected_total"] else 0.0,
            "flagged_approved": t_week["flagged_approved"], "flagged_rejected": t_week["flagged_rejected"],
            "shown": t_week["shown"], "overridden": t_week["overridden"],
            "all_time_flag_rate_on_approved": t_all["flagged_approved"] / t_all["approved_total"] if t_all["approved_total"] else 0.0,
            "all_time_catch_rate_on_rejected": t_all["flagged_rejected"] / t_all["rejected_total"] if t_all["rejected_total"] else 0.0,
            "override_rate": vis.override_rate, "hidden": not vis.visible, "demoted": cid in overrides,
            "fire_rate": fired / ran if ran else 0.0, "duos_checked": ran})
    rows.sort(key=lambda r: (-r["flagged_approved"] - r["flagged_rejected"], r["check_id"]))
    return rows


def hard_reject_rate(rt: Any) -> dict[str, Any]:
    """The share of approved duos on which a HARD or ASSERT check failed at least once (all HARD checks together should stay under
    ``calib.hard_reject_rate_max``, about 10%). Demoted checks are SOFT now and do not count."""
    ids = approved_duo_ids(rt)
    overrides = dict(rt.settings.checks.check_overrides)
    rejected: dict[str, set[str]] = defaultdict(set)
    if ids:
        marks = ",".join("?" for _ in ids)
        rows = rt.db.conn().execute(
            f"SELECT project_id AS pid, check_id AS cid, json FROM checks WHERE project_id IN ({marks}) AND passed=0 "
            "AND json_extract(json, '$.status') = 'failed'", ids).fetchall()
        for r in rows:
            kind = policy.effective_kind(r["cid"], overrides, declared=json.loads(r["json"]).get("kind"))
            if kind in ("hard", "assert"):
                rejected[r["pid"]].add(policy.meta(r["cid"]).policy_class)
    by_class = Counter(c for classes in rejected.values() for c in classes)
    rate = len(rejected) / len(ids) if ids else 0.0
    enough = len(ids) >= int(_thr("calib.percentile_min_duos"))
    return {"rate": rate, "duos": len(ids), "rejected_duos": len(rejected), "by_class": dict(by_class), "max": float(_thr("calib.hard_reject_rate_max")),
            "alert": bool(enough and rate > float(_thr("calib.hard_reject_rate_max")))}


def wildcard_stats(rt: Any) -> dict[str, Any]:
    """How often the person picked the wildcard among their last 10 duos (APP_SPEC §3.5)."""
    from duoskin.pipeline import brief as BR

    last = BR.approved_specs(rt, limit=10)
    n = len(last)
    picked = sum(1 for a in last if a["spec"].get("is_wildcard"))
    rate = picked / n if n else 0.0
    limit = float(_thr("calib.wildcard_pick_rate"))
    return {"duos": n, "picked": picked, "rate": rate, "limit": limit,
            "suggest_novelty": bool(n >= int(_thr("calib.percentile_min_duos")) and rate >= limit)}


def gate1_first_try(rt: Any) -> dict[str, Any]:
    """Share of duos whose very first Gate 1 decision was Approve (no Reimagine, New plan or Change before it)."""
    projects: dict[str, Any] = {}
    for g in rt.repo.list_gates(state=None):
        if g.kind.value == "concept" and g.project_id not in projects:
            projects[g.project_id] = g
    first = total = 0
    for pid, gate in projects.items():
        proj = rt.repo.find_project(pid)
        if proj is None or is_regression_name(proj.name):
            continue
        decisions = [d for d in rt.repo.list_decisions(gate.id) if not d.provisional]
        if not decisions:
            continue
        total += 1
        first += 1 if decisions[0].action.value == "approve" else 0
    rate = first / total if total else 0.0
    floor = float(_thr("calib.first_try_approval_min"))
    return {"duos": total, "first_try": first, "rate": rate, "min": floor,
            "review_12_concepts": bool(total >= int(_thr("calib.percentile_min_duos")) * 2 and rate < floor)}


def nearest_duo_distances(rt: Any) -> dict[str, Any]:
    """For each approved duo, the distance to the nearest EARLIER duo (``duo_memory`` vectors; DreamSim cosine or normalised pHash)."""
    from duoskin.imaging import similarity

    rows = rt.db.conn().execute("SELECT m.project_id AS pid, m.json AS json FROM duo_memory m JOIN projects p ON p.id = m.project_id "
                                "WHERE json_extract(p.json, '$.name') NOT LIKE ? ORDER BY m.approved_at, m.rowid", (REGRESSION_PREFIX + "%",)).fetchall()
    vectors: list[tuple[str, dict[str, Any]]] = []
    for r in rows:
        try:
            vec = json.loads(r["json"]).get("vector")
        except ValueError:
            vec = None
        if vec:
            vectors.append((r["pid"], vec))
    series = []
    for i, (pid, vec) in enumerate(vectors):
        if i == 0:
            continue
        hit = similarity.nearest_past(vec, vectors[:i])
        if hit is not None:
            series.append({"project_id": pid, "nearest": hit[0], "distance": hit[1]})
    dists = [s["distance"] for s in series]
    shrinking = len(dists) >= 10 and float(np.mean(dists[-5:])) < float(np.mean(dists[-10:-5]))
    return {"series": series[-30:], "shrinking": bool(shrinking), "duos": len(vectors)}


def reject_rates(rt: Any) -> dict[str, Any]:
    """The registry reject rate and the plan-lint reject rate per block of 10 duos (APP_SPEC §3.7). Above ``calib.registry_reject_alert`` the
    Library page says "grow the kits"."""
    projects = [p for p in rt.repo.list_projects(include_archived=True) if not is_regression_name(p.name)]
    projects.sort(key=lambda p: (p.created_at, p.id))
    reg: dict[str, list[int]] = {}
    for r in rt.db.conn().execute("SELECT project_id AS pid, MIN(passed) AS ok FROM checks WHERE check_id IN ('A_REGISTRY') AND project_id IS NOT NULL "
                                  "GROUP BY project_id").fetchall():
        reg[r["pid"]] = [0 if r["ok"] else 1]
    lint: dict[str, list[int]] = defaultdict(list)
    for p in projects:
        for rec in rt.repo.list_specs(p.id):
            if rec.created_by == "planner" and rec.lint:
                lint[p.id].append(1 if any(x.kind == "hard" and not x.passed and x.ran for x in rec.lint) else 0)
    block = 10
    blocks = []
    for i in range(0, len(projects), block):
        chunk = projects[i:i + block]
        reg_vals = [v for p in chunk for v in reg.get(p.id, [])]
        lint_vals = [v for p in chunk for v in lint.get(p.id, [])]
        blocks.append({"duos": len(chunk), "registry_reject_rate": sum(reg_vals) / len(reg_vals) if reg_vals else 0.0,
                       "lint_reject_rate": sum(lint_vals) / len(lint_vals) if lint_vals else 0.0})
    limit = float(_thr("calib.registry_reject_alert"))
    latest = blocks[-1] if blocks else {"registry_reject_rate": 0.0, "lint_reject_rate": 0.0}
    return {"blocks": blocks, "latest_registry_reject_rate": latest["registry_reject_rate"], "latest_lint_reject_rate": latest["lint_reject_rate"],
            "limit": limit, "grow_the_kits": bool(latest["registry_reject_rate"] > limit or latest["lint_reject_rate"] > limit)}


def cost_per_duo(rt: Any) -> dict[str, Any]:
    ids = approved_duo_ids(rt)
    spent = [float(rt.budget.spent(pid)) for pid in ids]
    total = 0.0
    for p in rt.repo.list_projects(include_archived=True):
        if not is_regression_name(p.name):
            total += float(rt.budget.spent(p.id))
    return {"duos": len(ids), "per_approved_duo": sum(spent) / len(spent) if spent else 0.0,
            "all_spend_per_approved_duo": total / len(ids) if ids else 0.0, "total_usd": total}


def failure_causes(rt: Any, limit: int = 8) -> list[dict[str, Any]]:
    """The checks that failed most often, grouped by FAILURE_MODES id."""
    counts: Counter[str] = Counter()
    who: dict[str, set[str]] = defaultdict(set)
    for r in rt.db.conn().execute("SELECT check_id AS cid, json FROM checks WHERE passed=0 AND json_extract(json, '$.status') = 'failed'").fetchall():
        fm = json.loads(r["json"]).get("fm_ids") or policy.meta(r["cid"]).fm_ids or [r["cid"]]
        for f in fm:
            counts[f] += 1
            who[f].add(r["cid"])
    return [{"fm_id": f, "failures": n, "check_ids": sorted(who[f])} for f, n in counts.most_common(limit)]


def revisit_triggers(rt: Any) -> list[dict[str, str]]:
    """The deferred "12 concepts" upgrade (APP_SPEC §3.11) is looked at again after about 10 real duos if one of these holds."""
    from collections import Counter as C

    specs = _approved_specs(rt, 100)
    if len(specs) < int(_thr("calib.percentile_min_duos")) * 2:
        return []
    out: list[dict[str, str]] = []
    if nearest_duo_distances(rt)["shrinking"]:
        out.append({"id": "nearest_shrinking", "text": "Your newest duos are closer to your earlier ones than before."})
    limit = float(_thr("calib.structure_collapse_share"))
    for label, getter in (("pair style", lambda s: s.get("world", {}).get("pair_structure")), ("palette family", lambda s: s.get("world", {}).get("palette_family"))):
        shares = C(v for v in (getter(a["spec"]) for a in specs) if v)
        if shares:
            value, n = shares.most_common(1)[0]
            if n / sum(shares.values()) > limit:
                out.append({"id": f"{label.split()[0]}_share", "text": f"One {label} ({value.replace('_', ' ')}) is {n / sum(shares.values()):.0%} of your duos."})
    first = gate1_first_try(rt)
    if first["review_12_concepts"]:
        out.append({"id": "first_try_low", "text": f"Only {first['rate']:.0%} of concepts were approved on the first try."})
    return out


def weekly_report(rt: Any, week: str | None = None) -> dict[str, Any]:
    """The weekly learning report (APP_SPEC §3.9): everything the Learning page and ``calibrate-report`` show. ``week`` is ``"2026-W41"``,
    a date in that week, or ``None`` for the current week; the all-time numbers are always included next to the week's."""
    from duoskin.pipeline import brief as BR

    if week is None or str(week).strip() == "":
        window: str | None = week_start()
    elif str(week).strip().lower() in ("all", "any"):
        window = None
    else:
        window = parse_week(week)
    start = window or "all"
    end = (datetime.fromisoformat(window) + timedelta(days=6)).date().isoformat() if window else "now"
    counts = label_counts(rt)
    approved = approved_duo_count(rt)
    hidden = hidden_warnings(rt)
    hard = hard_reject_rate(rt)
    wild = wildcard_stats(rt)
    first = gate1_first_try(rt)
    nearest = nearest_duo_distances(rt)
    rejects = reject_rates(rt)
    shares = BR.structure_shares(rt)
    collapse = BR.structure_collapse(rt)
    rows = per_check_rows(rt, window)
    tuner = tuner_status(rt)
    return {
        "week": {"label": iso_week_label(window) if window else "all time", "start": start, "end": end},
        "approved_duos": approved,
        "decisions": {"approved": sum(r.approved_total for r in stat_rows(rt, window=window) if r.check_id.startswith("*gate:")),
                      "rejected": sum(r.rejected_total for r in stat_rows(rt, window=window) if r.check_id.startswith("*gate:"))},
        "per_check": rows,
        "hidden_warnings": [v.as_dict() for v in hidden],
        "demoted_checks": active_demotions(rt),
        "hard_reject_rate_on_approved": hard["rate"], "hard_reject": hard,
        "structure_use": shares, "structure_collapse": collapse,
        "structure_hint": ("One pair style is over 35% of your duos: turn on the 'least used recently' hint in the planner settings."
                           if collapse else ""),
        "wildcard_pick_rate": wild["rate"], "wildcard": wild,
        "wildcard_hint": "You often pick the wildcard: give novelty more weight in the critic." if wild["suggest_novelty"] else "",
        "gate1_first_try_approval": first["rate"], "gate1": first,
        "nearest_duo_distances": nearest,
        "registry_reject_rate": rejects["latest_registry_reject_rate"], "reject_rates": rejects,
        "grow_the_kits": rejects["grow_the_kits"],
        "cost_per_duo": cost_per_duo(rt)["per_approved_duo"], "cost": cost_per_duo(rt),
        "top_failure_causes": failure_causes(rt),
        "labels": counts.as_dict(),
        "revisit_triggers": revisit_triggers(rt),
        "tuner": tuner,
        "alerts": [a for a in (
            f"Required checks stopped {hard['rate']:.0%} of the duos you approved (the limit is {hard['max']:.0%})." if hard["alert"] else "",
            "Grow the kits: new designs keep colliding with earlier ones." if rejects["grow_the_kits"] else "") if a],
    }


# ======================================================================================================================
# the threshold tuner: proposals only, bad tail only
# ======================================================================================================================
@dataclass(frozen=True)
class Tunable:
    """A [DES] threshold the tuner may propose. ``tail`` is the BAD tail of the measured quantity: ``low`` when small values are the bad
    ones (a lower edge such as the clone band), ``high`` when large values are bad (an upper bound such as a silhouette overlap)."""
    key: str
    tail: Literal["low", "high"]
    check: tuple[tuple[str, str], ...] = ()       # (check id, metric) pairs whose stored value is the measured quantity
    pair: str | None = None                       # or: a clone-band pair metric computed from the stored renders ("dreamsim_mean", ...)
    negative: str | None = None                   # the drill answer that marks the wrong side of this bound ("clone", "strangers")
    what: str = ""


TUNABLES: tuple[Tunable, ...] = (
    Tunable("duo.dreamsim_clone_min", "low", pair="dreamsim_mean", negative="clone", what="how different A and B must look (clone band, DreamSim)"),
    Tunable("duo.degraded_phash_clone_max", "low", pair="phash_mean", negative="clone", what="clone band without DreamSim: picture hash distance"),
    Tunable("duo.degraded_palette_overlap_max", "high", pair="palette_overlap", negative="clone", what="clone band without DreamSim: shared colours"),
    Tunable("duo.strangers_dreamsim_max", "high", pair="dreamsim_mean", negative="strangers", what="how far apart a pair may look before it reads as strangers"),
    Tunable("pln.contrast_colour_de", "low", check=(("DUO-03", "main_colour_contrast"),), what="how different the two main colours must be"),
    Tunable("pln.anchor_colour_de_max", "high", check=(("CHK-G0-06", "anchor_colour"),), what="how far a shared colour may drift between the two"),
    Tunable("duo.silhouette_iou_cold", "high", check=(("DUO-04", "hair_accessory_silhouette_iou"),), what="how alike the two hair and accessory shapes may be"),
    Tunable("pln.kit_hair_iou_warn", "high", check=(("PLN-15", "kit_hair_pair_iou"),), what="how alike the two kit hairstyles may be"),
    Tunable("taste.layout_ari_warn", "high", check=(("TASTE_LAYOUT", "layout_ari"),), what="how alike the two garment layouts may be"),
)
TUNABLE_BY_KEY = {t.key: t for t in TUNABLES}


def weighted_percentile(values: Sequence[float], q: float, weights: Sequence[float] | None = None) -> float:
    """``q`` in 0..100. With equal weights this is ``numpy.percentile`` (linear interpolation); with unequal weights it interpolates the weighted
    mid-point distribution."""
    v = np.asarray(values, float)
    if v.size == 0:
        raise ValueError("no values")
    w = np.ones_like(v) if weights is None else np.asarray(weights, float)
    if np.allclose(w, w[0]):
        return float(np.percentile(v, q))
    order = np.argsort(v)
    v, w = v[order], w[order]
    cdf = (np.cumsum(w) - 0.5 * w) / w.sum()
    return float(np.interp(q / 100.0, cdf, v))


@dataclass
class Proposal:
    key: str
    what: str
    tail: str
    current: float
    proposed: float | None
    percentile: float
    status: Literal["proposed", "unchanged", "insufficient", "not_ready"]
    n_approved: int = 0
    n_drill: int = 0
    effective_n: float = 0.0
    needed: int = 0
    note: str = ""
    negatives: dict[str, Any] = field(default_factory=dict)
    preview: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TunerReport:
    ready: bool
    labels: float                       # the calibration set after down-weighting
    target: int
    approved_duos: int
    proposals: list[Proposal]
    generated_at: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["proposals"] = [p.as_dict() for p in self.proposals]
        return d


def pair_views(rt: Any, project_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A's and B's stored renders (the sides the duo render made) as PIL images, or ``None`` when the duo has none."""
    from duoskin.pipeline import common
    from duoskin.pipeline import duo as DUO

    renders = (rt.repo.kv_get(DUO.duo_key(project_id)) or {}).get("renders") or {}
    views: dict[str, dict[str, Any]] = {"a": {}, "b": {}}
    for c in ("a", "b"):
        for s in DUO.SIDES:
            sha = renders.get(f"{c}.{s}")
            if sha:
                views[c][s] = common.open_image(rt.cas.get(sha))
    if not views["a"] or not views["b"]:
        return None
    return views["a"], views["b"]


def pair_metrics(a_views: Mapping[str, Any], b_views: Mapping[str, Any], *, model: Any = None, bg_hex: str | None = "#f2f2f2") -> dict[str, float]:
    """The clone-band quantities of a pair (APP_SPEC §3.8 drills and the tuner use the same function, so a drill item and an approved duo are
    measured alike): mean pHash distance, mean palette overlap and, with a DreamSim model, the mean DreamSim distance."""
    from duoskin.imaging import palette as P
    from duoskin.imaging import similarity as S

    sides = [s for s in S.SIDES if s in a_views and s in b_views] or sorted(set(a_views) & set(b_views))
    if not sides:
        raise ValueError("no matching sides between A and B")
    de = float(TH.get("sim.palette_overlap_de"))
    k = int(TH.get("sim.palette_k"))
    merge = float(TH.get("sim.palette_merge_de"))
    ex = [bg_hex] if bg_hex else ()
    ph = [float(S.hamming(S.phash(a_views[s], crop_to_subject=True), S.phash(b_views[s], crop_to_subject=True))) for s in sides]
    ov = [P.palette_overlap(P.extract_palette(a_views[s], k=k, exclude_hex=ex, merge_de=merge),
                            P.extract_palette(b_views[s], k=k, exclude_hex=ex, merge_de=merge), de) for s in sides]
    out = {"phash_mean": float(np.mean(ph)), "palette_overlap": float(np.mean(ov))}
    if model is not None:
        out["dreamsim_mean"] = float(np.mean([model.distance(a_views[s], b_views[s]) for s in sides]))
    return out


def _dreamsim_model(rt: Any) -> Any:
    try:
        from duoskin.imaging import similarity
        from duoskin.pipeline import kits

        return similarity.load_dreamsim(kits.dreamsim_path(rt)) if kits.dreamsim_present(rt) else None
    except Exception:    # noqa: BLE001 - a broken model is the doctor's job; the tuner just has no DreamSim values
        return None


def approved_pair_metrics(rt: Any) -> dict[str, dict[str, float]]:
    """``project id -> pair metrics`` of every approved duo that has renders (cached in ``kv``, so a report never re-renders anything)."""
    model = _dreamsim_model(rt)
    mode = "dreamsim" if model is not None else "degraded"
    out: dict[str, dict[str, float]] = {}
    for pid in approved_duo_ids(rt):
        key = f"calib:pair:{pid}:{mode}"
        cached = rt.repo.kv_get(key)
        if cached:
            out[pid] = cached
            continue
        views = pair_views(rt, pid)
        if views is None:
            continue
        try:
            m = pair_metrics(views[0], views[1], model=model)
        except Exception:
            log.exception("pair metrics of %s failed", pid)
            continue
        rt.repo.kv_set(key, m)
        out[pid] = m
    return out


def check_values(rt: Any, project_ids: Sequence[str], pairs: Sequence[tuple[str, str]]) -> dict[str, float]:
    """The latest stored value of a check metric per project: from the ``checks`` table and from the approved plan's lint results."""
    if not project_ids:
        return {}
    wanted = {(c, m) for c, m in pairs}
    out: dict[str, float] = {}
    marks = ",".join("?" for _ in project_ids)
    rows = rt.db.conn().execute(f"SELECT project_id AS pid, json FROM checks WHERE project_id IN ({marks}) ORDER BY created_at, rowid", list(project_ids)).fetchall()
    for r in rows:
        doc = json.loads(r["json"])
        if (doc.get("check_id"), doc.get("metric")) in wanted and doc.get("value") is not None and doc.get("ran", True):
            out[r["pid"]] = float(doc["value"])
    for pid in project_ids:
        proj = rt.repo.find_project(pid)
        if proj is None or not proj.approved_spec_id or pid in out:
            continue
        try:
            rec = rt.repo.get_spec(proj.approved_spec_id)
        except Exception:    # noqa: BLE001 - one unreadable spec must not stop a report
            log.warning("approved spec of %s could not be read", pid)
            continue
        for res in rec.lint:
            if (res.check_id, res.metric) in wanted and res.value is not None and res.ran:
                out[pid] = float(res.value)
    return out


def _drill_values(rt: Any, tunable: Tunable, weight: float) -> tuple[list[float], list[float], dict[str, list[float]]]:
    """Drill answers as values of this tunable's quantity: positives (``real_duo`` and not disliked) with their weight, and the values of the
    pairs the person called ``clone`` or ``strangers`` (sanity bounds, never used for the percentile)."""
    metric = tunable.pair
    if metric is None:
        return [], [], {}
    dislikes: set[str] = set()
    items: list[tuple[str, str, float]] = []
    for lab in list_labels(rt, source="drill"):
        item = str(lab.subject_ids[0]) if lab.subject_ids else ""
        v = lab.value if isinstance(lab.value, dict) else {}
        if lab.kind == "like_dislike" and v.get("liked") is False:
            dislikes.add(item)
        elif lab.kind == "clone_real_stranger" and v.get("label") in DRILL_ANSWERS and metric in (v.get("metrics") or {}):
            items.append((item, str(v["label"]), float(v["metrics"][metric])))
    positives = [x for item, lab, x in items if lab == "real_duo" and item not in dislikes]
    negatives: dict[str, list[float]] = defaultdict(list)
    for _, lab, x in items:
        if lab in ("clone", "strangers"):
            negatives[lab].append(x)
    return positives, [weight] * len(positives), negatives


def _round_like(value: float, current: Any) -> float:
    return float(round(value)) if isinstance(current, int) and not isinstance(current, bool) else round(float(value), 3)


def tune_thresholds(rt: Any, *, keys: Iterable[str] | None = None, preview: bool = False) -> TunerReport:
    """Proposals for the [DES] thresholds, from the approved duos and the drill answers. **Nothing is applied.**

    * Only after ``calib.target_labels`` (200) labels in the calibration set (drill labels counted at their down-weighted size), unless
      ``preview=True``, which marks every proposal as a preview.
    * A threshold needs ``calib.tuner_min_values`` approved values (weighted drill positives count too), else it is ``insufficient``.
    * A lower edge goes to the ``calib.tail_percentile``-th (5th) percentile of the values, an upper bound to the 95th. There is no mean,
      median or target anywhere, and the proposal is always inside the range the approved duos actually span.
    * Pairs the person called ``clone`` or ``strangers`` only add a note ("N of M such pairs would still pass").
    """
    counts = label_counts(rt)
    ready = counts.effective_total >= counts.target
    q = float(_thr("calib.tail_percentile"))
    need = int(_thr("calib.tuner_min_values"))
    approved = approved_duo_ids(rt)
    pair_vals = approved_pair_metrics(rt)
    wanted = [t for t in TUNABLES if keys is None or t.key in set(keys)]
    proposals: list[Proposal] = []
    for t in wanted:
        current = TH.default(t.key)
        percentile = q if t.tail == "low" else 100.0 - q
        base = {"key": t.key, "what": t.what, "tail": t.tail, "current": current, "percentile": percentile, "needed": need,
                "preview": bool(preview and not ready)}
        if not ready and not preview:
            proposals.append(Proposal(**base, proposed=None, status="not_ready",
                                      note=f"{counts.effective_total:.0f} of {counts.target} labels so far"))
            continue
        if t.pair is not None:
            values = [m[t.pair] for m in pair_vals.values() if t.pair in m]
        else:
            values = list(check_values(rt, approved, t.check).values())
        pos, w_pos, negatives = _drill_values(rt, t, counts.drill_weight)
        all_values = [*values, *pos]
        weights = [1.0] * len(values) + list(w_pos)
        eff = float(sum(weights))
        if len(all_values) == 0 or eff < need:
            proposals.append(Proposal(**base, proposed=None, status="insufficient", n_approved=len(values), n_drill=len(pos), effective_n=eff,
                                      note=f"{eff:.0f} of the {need} approved values this bound needs"))
            continue
        bound = _round_like(weighted_percentile(all_values, percentile, weights), current)
        neg_note: dict[str, Any] = {}
        for label, xs in negatives.items():
            if label != t.negative:
                continue
            still_pass = sum(1 for x in xs if (x >= bound if t.tail == "low" else x <= bound))
            neg_note[label] = {"pairs": len(xs), "still_pass": still_pass}
        status = "unchanged" if _round_like(float(current), current) == bound else "proposed"
        note = ""
        if neg_note.get(t.negative or "", {}).get("still_pass"):
            n = neg_note[t.negative]
            note = f"{n['still_pass']} of {n['pairs']} pairs you called {t.negative} would still pass this bound"
        proposals.append(Proposal(**base, proposed=bound, status=status, n_approved=len(values), n_drill=len(pos), effective_n=eff, note=note,
                                  negatives=neg_note))
    report = TunerReport(ready=ready, labels=counts.effective_total, target=counts.target, approved_duos=len(approved), proposals=proposals,
                         generated_at=iso_utc(utcnow()),
                         note="" if ready else f"The tuner waits for about {counts.target} labels; there are {counts.effective_total:.0f}.")
    rt.repo.kv_set(PROPOSALS_KEY, report.as_dict())
    return report


def tuner_status(rt: Any) -> dict[str, Any]:
    """The small summary the weekly report carries (the last tuner run, without recomputing it)."""
    counts = label_counts(rt)
    last = rt.repo.kv_get(PROPOSALS_KEY) or {}
    return {"ready": counts.effective_total >= counts.target, "labels": counts.effective_total, "target": counts.target,
            "active": active_thresholds(rt), "last_run": last.get("generated_at"),
            "open_proposals": [p for p in last.get("proposals", []) if p.get("status") == "proposed"]}


def active_thresholds(rt: Any) -> dict[str, Any]:
    return dict(rt.repo.kv_get(ACTIVE_KEY) or {})


def load_calibrated(rt: Any) -> dict[str, Any]:
    """Install the accepted calibrated values from the database into this process (called at start-up). A value that is no longer tunable
    is ignored rather than failing the start."""
    values = {k: v for k, v in active_thresholds(rt).items() if k in TH.T and TH.is_tunable(k)}
    return TH.set_calibrated(values)


class GuardRequired(RuntimeError):
    """A threshold set may only be accepted after the variety guard passed for it."""


def accept_proposals(rt: Any, values: Mapping[str, float], *, guard_passed: bool, guard_ref: str = "") -> dict[str, Any]:
    """The explicit act that makes proposals real: store them, activate them in this process and let the affected warnings come back.

    ``guard_passed`` must be True (the caller ran ``regression.evaluate_threshold_candidate``); nothing is ever accepted silently."""
    if not guard_passed:
        raise GuardRequired("a threshold set is only accepted after it passed the regression and the variety guard")
    clean: dict[str, Any] = {}
    for k, v in values.items():
        if k not in TUNABLE_BY_KEY:
            raise ValueError(f"{k!r} is not a threshold the tuner may change")
        if not TH.is_tunable(k):
            raise ValueError(f"{k!r} has status {TH.status_of(k)} and cannot be calibrated")
        clean[k] = _round_like(float(v), TH.default(k))
    active = active_thresholds(rt)
    history = list(rt.repo.kv_get(HISTORY_KEY) or [])
    stamp = iso_utc(utcnow())
    for k, v in clean.items():
        history.append({"key": k, "old": active.get(k, TH.default(k)), "new": v, "at": stamp, "guard": guard_ref})
    active.update(clean)
    rt.repo.kv_set(ACTIVE_KEY, active)
    rt.repo.kv_set(HISTORY_KEY, history)
    TH.set_calibrated({k: v for k, v in active.items() if k in TH.T})
    affected = {cid for k in clean for cid, m in policy.registry().items() if k in m.threshold_keys}
    reset_recent(rt, affected)
    return {"active": active, "reset_warnings": sorted(affected), "id": new_id("cal")}


def format_report(report: Mapping[str, Any]) -> str:
    """The report as plain text (``python -m duoskin calibrate-report``)."""
    w = report["week"]
    lines = [f"DuoSkin Studio weekly report: {w['label']} ({w['start']} to {w['end']})",
             f"Approved duos: {report['approved_duos']}   Decisions: {report['decisions']['approved']} approved, {report['decisions']['rejected']} rejected",
             (f"Labels: {report['labels']['total']} (drill share {report['labels']['drill_share']:.0%}, calibration set "
              f"{report['labels']['effective_total']:.0f} of about {report['labels']['target']})")]
    for a in report["alerts"]:
        lines.append(f"ALERT: {a}")
    for b in report["demoted_checks"]:
        lines.append(f"DEMOTED: {b['banner']}")
    for h in report["hidden_warnings"]:
        lines.append(f"HIDDEN WARNING: {h['check_id']} ({h['reason']})")
    lines += ["", "Per check (flag rate on approved duos / catch rate on rejected duos):"]
    if not report["per_check"]:
        lines.append("  no check has statistics yet")
    for r in report["per_check"][:25]:
        lines.append(f"  {r['check_id']:<14} {r['kind']:<6} flags approved {r['flag_rate_on_approved']:.0%} ({r['flagged_approved']}), "
                     f"catches rejected {r['catch_rate_on_rejected']:.0%} ({r['flagged_rejected']}), shown {r['shown']}, overridden {r['overridden']}")
    lines += ["", f"Required checks stopped {report['hard_reject_rate_on_approved']:.0%} of approved duos",
              f"Wildcard picked in {report['wildcard_pick_rate']:.0%} of the last duos",
              f"Gate 1 approved on the first try: {report['gate1_first_try_approval']:.0%}",
              f"Cost per approved duo: ${report['cost_per_duo']:.2f}"]
    if report["structure_use"]:
        lines.append("Pair styles: " + ", ".join(f"{k} {v:.0%}" for k, v in report["structure_use"].items()))
    for t in report["revisit_triggers"]:
        lines.append(f"REVISIT: {t['text']}")
    return "\n".join(lines)
