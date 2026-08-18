"""Verification agent — re-query real Prometheus / health endpoints."""
from __future__ import annotations

from typing import Any

import httpx

from agent.config import settings
from agent.state import IncidentState, VerificationResult
from tools.prometheus_tools import get_service_error_rate


def _health(url: str) -> dict:
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(f"{url.rstrip('/')}/health")
            return {"ok": resp.status_code == 200, "body": resp.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _extract_error_rate(prom_result: dict) -> float | None:
    try:
        result = (prom_result.get("data") or {}).get("result") or []
        if not result:
            return None
        value = result[0].get("value")
        if value and len(value) >= 2:
            return float(value[1])
    except Exception:
        return None
    return None


def verification_node(state: IncidentState) -> dict[str, Any]:
    event = state.get("event") or {}
    service = event.get("service") or "payments-api"
    url = (
        settings.payments_api_url
        if "payment" in service
        else settings.orders_api_url
    )

    health = _health(url)
    prom = get_service_error_rate.invoke({"service": service})
    err = _extract_error_rate(prom if isinstance(prom, dict) else {})

    # Also clear-fault success path: if actions included rollback/restart and health ok
    actions = state.get("executed_actions") or []
    had_success = any(
        a.get("status") in {"success", "dry_run"}
        and a.get("action_type") in {"restart_service", "rollback_deploy"}
        for a in actions
    )

    recovered = False
    notes = []
    if health.get("ok"):
        notes.append("health ok")
    else:
        notes.append(f"health failed: {health}")

    if err is not None:
        notes.append(f"error_rate={err}")
        if err < 0.1:
            recovered = True
    elif health.get("ok") and had_success:
        # When Prometheus empty (local demo), accept health + successful action
        recovered = True
        notes.append("prometheus empty — accepted health + successful action")

    # If DRY_RUN and actions succeeded as dry_run, mark recovered for demo continuity
    # only when health is ok — still honest about dry-run
    if settings.dry_run and health.get("ok") and had_success:
        recovered = True
        notes.append("DRY_RUN mode: treating successful dry-run + healthy as recovered for demo")

    result = VerificationResult(
        recovered=recovered,
        error_rate=err,
        notes="; ".join(notes),
        evidence=[str(health), str(prom)[:500]],
    )

    retries = int(state.get("verification_retries") or 0)
    if not recovered:
        retries += 1

    return {
        "verification": result.model_dump(),
        "verification_retries": retries,
        "confidence": float(state.get("confidence") or 0)
        if recovered
        else max(0.0, float(state.get("confidence") or 0) - 0.15),
        "audit_log": [
            {
                "agent": "verification",
                "event_type": "checked",
                "payload": result.model_dump(),
            }
        ],
    }
