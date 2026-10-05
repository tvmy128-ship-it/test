"""``/api/keys`` (APP_SPEC §13, §14.2). The value is accepted on PUT and never echoed back; status shows ``sk-…abcd`` only."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from duoskin.api import RT
from duoskin.engine.runtime import Runtime
from duoskin.keystore import PROVIDERS, KeyRejected, KeyStatus

router = APIRouter(prefix="/api")

TEST_COST_NOTES = {"recraft": "This test makes one paid call (about $0.08).",
                   "openai": "The first test of a new key makes one small paid image call (about $0.006)."}


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail={"error": "unknown_provider", "message": f"unknown provider '{provider}'"})
    return provider


def _status(rt: Runtime, provider: str) -> dict[str, Any]:
    st: KeyStatus = rt.keys.status_of(provider)
    out = st.model_dump(mode="json")
    out["test_cost_note"] = TEST_COST_NOTES.get(provider)
    out["mode"] = rt.effective_settings().mode_of(provider).value
    out["paused"] = rt.scheduler.paused_providers.get(provider)
    return out


@router.get("/keys")
def list_keys(rt: Runtime = RT) -> dict[str, Any]:
    return {p: _status(rt, p) for p in PROVIDERS}


@router.put("/keys/{provider}")
def put_key(provider: str, body: KeyIn, rt: Runtime = RT) -> dict[str, Any]:
    _provider(provider)
    try:
        rt.keys.set_key(provider, body.value)
    except KeyRejected as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_key", "message": str(exc)}) from exc
    rt.scheduler.resume_provider(provider)   # a rejected key paused the provider; a new key lifts that
    return _status(rt, provider)


@router.delete("/keys/{provider}")
def delete_key(provider: str, rt: Runtime = RT) -> dict[str, Any]:
    _provider(provider)
    rt.keys.delete_key(provider)
    return _status(rt, provider)


@router.post("/keys/{provider}/test")
def test_key(provider: str, rt: Runtime = RT) -> dict[str, Any]:
    """Probe the provider with the stored key. Mock mode makes no call. The adapter's ``test_key()`` (provider layer)
    returns ``{"ok": bool, "message": str}``; without it (provider layer missing) the result is ``ok: null``."""
    _provider(provider)
    mode = rt.effective_settings().mode_of(provider).value
    note = TEST_COST_NOTES.get(provider)
    if mode == "mock":
        result: dict[str, Any] = {"ok": True, "message": "Mock provider: nothing to test, no call was made."}
    elif mode == "disabled":
        result = {"ok": None, "message": "This provider is disabled in Settings."}
    elif rt.keys.get_key(provider) is None:
        result = {"ok": False, "message": "No key is set for this provider."}
    else:
        try:
            from duoskin.providers import registry as provider_registry

            adapter = provider_registry.get(provider)
            probe = getattr(adapter, "test_key", None)
            if probe is None:
                result = {"ok": None, "message": "This provider has no key test yet."}
            else:
                raw = probe()
                result = raw if isinstance(raw, dict) else {"ok": bool(raw), "message": ""}
        except ImportError:
            result = {"ok": None, "message": "The provider layer is not installed in this build."}
        except Exception as exc:  # noqa: BLE001
            from duoskin.logsetup import redact

            result = {"ok": False, "message": redact(f"{type(exc).__name__}: {exc}")[:300]}
    saved = rt.keys.record_test(provider, result.get("ok"), str(result.get("message", "")))
    return {"provider": provider, "ok": result.get("ok"), "message": saved.message, "at": saved.at.isoformat(),
            "cost_note": note, "status": _status(rt, provider)}
