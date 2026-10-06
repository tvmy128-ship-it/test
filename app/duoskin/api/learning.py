"""Learning routes (APP_SPEC §3.9, §12, §13): the weekly report, the threshold tuner, demoted checks, the regression job and version promotion.

Nothing here applies a threshold or a model version by itself: the tuner returns proposals, ``POST /api/learning/thresholds/accept`` runs the
variety guard on them first, and ``POST /api/versions/promote`` is refused unless the latest regression run of that candidate passed the guard.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from duoskin.api import RT
from duoskin.engine import calibration as cal
from duoskin.engine.runtime import Runtime
from duoskin.pipeline import regression as reg

router = APIRouter(prefix="/api")


def _problem(exc: reg.RegressionError) -> JSONResponse:
    status = 409 if exc.code in ("busy", "guard", "no_regression", "no_baseline", "not_a_candidate") else 422
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=status)


# ---------------------------------------------------------------------------------------------------------- the report
@router.get("/learning/report")
def report(week: str | None = None, rt: Runtime = RT) -> dict[str, Any]:
    """The weekly report (``?week=2026-W41``, a date, or ``all``; default: this week) with the regression summary next to it."""
    try:
        out = cal.weekly_report(rt, week)
    except ValueError as exc:
        raise HTTPException(422, detail={"error": "bad_week", "message": f"not a week: {week}"}) from exc
    out["regression"] = regression_summary(rt)
    return out


def regression_summary(rt: Runtime) -> dict[str, Any]:
    runs = reg.list_runs(rt)
    return {"versions": reg.versions_view(rt), "runs": [reg.run_summary(r) for r in runs[:8]], "estimate": reg.estimate(rt, "plan"),
            "sample": int(reg.TH.get("calib.regression_sample")), "briefs": int(reg.TH.get("calib.regression_briefs"))}


# ---------------------------------------------------------------------------------------------------------- the tuner
class TunerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] | None = None
    preview: bool = False


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] = Field(min_length=1)


@router.get("/learning/tuner")
def tuner_last(rt: Runtime = RT) -> dict[str, Any]:
    """The last tuner run (proposals only) and the thresholds that are active because the person accepted them."""
    return {**cal.tuner_status(rt), "report": rt.repo.kv_get(cal.PROPOSALS_KEY), "history": rt.repo.kv_get(cal.HISTORY_KEY) or []}


@router.post("/learning/tuner/run")
def tuner_run(body: TunerIn, rt: Runtime = RT) -> dict[str, Any]:
    """Compute proposals now. Nothing is applied: each proposal is a bad-tail bound (about the 5th percentile of the approved duos)."""
    return cal.tune_thresholds(rt, keys=body.keys, preview=body.preview).as_dict()


@router.post("/learning/thresholds/accept", response_model=None)
def thresholds_accept(body: AcceptIn, rt: Runtime = RT) -> dict[str, Any] | JSONResponse:
    """Accept proposals from the last tuner run: the variety guard judges the set first (the baseline plans linted with the new values)."""
    last = rt.repo.kv_get(cal.PROPOSALS_KEY) or {}
    wanted = {p["key"]: p["proposed"] for p in last.get("proposals", []) if p.get("status") == "proposed" and p["key"] in body.keys}
    missing = [k for k in body.keys if k not in wanted]
    if missing:
        return JSONResponse({"error": "no_proposal", "message": f"there is no open proposal for {', '.join(missing)}"}, status_code=422)
    guard = reg.evaluate_threshold_candidate(rt, wanted)
    if not guard.accepted:
        return JSONResponse({"error": "guard", "message": "The variety guard did not accept these thresholds. " + " ".join(guard.reasons), "guard": guard.as_dict()},
                            status_code=409)
    out = cal.accept_proposals(rt, wanted, guard_passed=True, guard_ref=guard.baseline_id)
    return {**out, "guard": guard.as_dict()}


# ---------------------------------------------------------------------------------------------------------- demoted checks
class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: str = Field(min_length=1, max_length=40)


@router.post("/learning/demotions/restore", status_code=204)
def demotion_restore(body: CheckIn, rt: Runtime = RT) -> None:
    """The person turns a demoted check back on (it keeps its history; the weekly report may demote it again)."""
    cal.restore_check(rt, body.check_id)


# ---------------------------------------------------------------------------------------------------------- regression
class RegressionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["plan", "parts"] = "plan"
    templates: list[str] = Field(default_factory=list, max_length=12)
    sample: int | None = Field(default=None, ge=1, le=100)
    candidate_versions: dict[str, str] = Field(default_factory=dict)
    compare: bool = False
    label: str = Field(default="", max_length=60)


@router.get("/regression/estimate")
def regression_estimate(stage: Literal["plan", "parts"] = "plan", sample: int | None = None, templates: int = 1, rt: Runtime = RT) -> dict[str, Any]:
    n = sample if sample else (int(reg.TH.get("calib.regression_briefs")) if stage == "plan" else len(reg.load_parts_set(rt)) or 10)
    return reg.estimate(rt, stage, briefs=n, specs=n, templates=max(1, templates))


@router.get("/regression/runs")
def regression_runs(stage: Literal["plan", "parts"] | None = None, rt: Runtime = RT) -> dict[str, Any]:
    return {"runs": [reg.run_summary(r) for r in reg.list_runs(rt, stage)], "versions": reg.versions_view(rt)}


@router.get("/regression/runs/{run_id}")
def regression_run(run_id: str, rt: Runtime = RT) -> dict[str, Any]:
    run = reg.load_run(rt, run_id)
    if run is None:
        raise HTTPException(404, detail={"error": "not_found", "message": "unknown regression run"})
    return reg.run_summary(run, detail=True)


@router.post("/regression/run", response_model=None)
def regression_start(body: RegressionIn, rt: Runtime = RT) -> dict[str, Any] | JSONResponse:
    """Start a REGRESSION job. It carries its estimate; above ``budgets.regression_ask_usd`` a BUDGET gate asks first (APP_SPEC §3.9)."""
    try:
        job = reg.start_regression(rt, stage=body.stage, sample=body.sample, templates=body.templates, candidate_versions=body.candidate_versions,
                                   label=body.label, compare=body.compare)
    except reg.RegressionError as exc:
        return _problem(exc)
    return {**job.model_dump(mode="json"), "estimate": job.params.get("estimate")}


@router.post("/regression/runs/{run_id}/adopt", response_model=None)
def regression_adopt(run_id: str, rt: Runtime = RT) -> dict[str, Any] | JSONResponse:
    try:
        return reg.adopt_run(rt, run_id)
    except reg.RegressionError as exc:
        return _problem(exc)


# ---------------------------------------------------------------------------------------------------------- versions
class PromoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=40)
    version: str = Field(min_length=1, max_length=120)


@router.get("/versions")
def versions(rt: Runtime = RT) -> dict[str, Any]:
    return reg.versions_view(rt)


@router.post("/versions/promote", response_model=None)
def versions_promote(body: PromoteIn, rt: Runtime = RT) -> dict[str, Any] | JSONResponse:
    """Make a candidate version the default. Refused (409) unless the latest regression run of exactly this candidate passed the variety guard."""
    try:
        return reg.promote(rt, body.role, body.version)
    except reg.RegressionError as exc:
        return _problem(exc)
