"""Gate routes (APP_SPEC §9.1, §13): read a gate, decide, confirm ("approve anyway"), withdraw ("go back").

The generic lifecycle lives in ``engine.gates``: idempotent decisions (``client_decision_id``), optimistic locking
(409 with the current gate), SOFT warnings withheld until the first choice, provisional approvals. What a decision *does*
for each gate kind comes from appliers that the pipeline registers with ``rt.gates.register_applier(kind, fn)``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from duoskin.api import get_rt
from duoskin.engine.gates import GateError
from duoskin.engine.runtime import Runtime
from duoskin.models.gate import Gate, GateDecision, GateDecisionIn

router = APIRouter(prefix="/api")


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    override_warnings: list[str] = Field(default_factory=list)


def _wrap(fn):   # noqa: ANN001, ANN202
    try:
        return fn()
    except GateError as exc:
        raise HTTPException(status_code=exc.status, detail={"error": exc.code, "message": str(exc)}) from exc


@router.get("/gates")
def list_gates(project_id: str | None = None, state: str | None = "open", rt: Runtime = Depends(get_rt)) -> list[Gate]:
    return [rt.gates.view(g) for g in rt.repo.list_gates(project_id, state)]


@router.get("/gates/{gate_id}")
def get_gate(gate_id: str, rt: Runtime = Depends(get_rt)) -> Gate:
    return rt.gates.view(gate_id)


@router.post("/gates/{gate_id}/decisions")
def decide(gate_id: str, body: GateDecisionIn, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    outcome = _wrap(lambda: rt.gates.decide(gate_id, body))
    return {"decision": outcome.decision.model_dump(mode="json"), "released_warnings": outcome.released_warnings,
            "provisional": outcome.decision.provisional, "replay": outcome.replay}


@router.post("/gates/{gate_id}/decisions/{decision_id}/confirm")
def confirm(gate_id: str, decision_id: str, body: ConfirmIn, rt: Runtime = Depends(get_rt)) -> GateDecision:
    return _wrap(lambda: rt.gates.confirm(gate_id, decision_id, body.override_warnings))


@router.delete("/gates/{gate_id}/decisions/{decision_id}", status_code=204)
def withdraw(gate_id: str, decision_id: str, rt: Runtime = Depends(get_rt)) -> Response:
    _wrap(lambda: rt.gates.withdraw(gate_id, decision_id))
    return Response(status_code=204)
