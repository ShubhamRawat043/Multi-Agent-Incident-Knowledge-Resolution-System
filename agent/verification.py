"""Verification agent — drive real traffic, then re-query health / Prometheus."""
from __future__ import annotations

from typing import Any

import httpx

from agent.config import settings
from agent.state import IncidentState, VerificationResult
from tools.prometheus_tools import get_service_error_rate

PROBE_COUNT = 4
PROBE_TIMEOUT = 10.0
RECOVERED_ERROR_RATE = 0.1


def _health(url: str) -> dict:
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(f"{url.rstrip('/')}/health")
            return {"ok": resp.status_code == 200, "body": resp.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _probe(service: str, url: str) -> dict:
    """
    Send a few real requests through the business endpoint.

    /health is a static 200 and the Prometheus error-rate gauge only moves when
    a request hits /pay or /orders, so without this the post-remediation
    metrics are whatever the incident left behind.
    """
    if "payment" in service:
        path = "/pay"
        body = {"order_id": "verify-probe", "amount": 1.0, "currency": "USD"}
    else:
        path = "/orders"
        body = {"item": "verify-probe", "amount": 1.0, "currency": "USD"}

    sent = 0
    failed = 0
    errors: list[str] = []
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            for _ in range(PROBE_COUNT):
                sent += 1
                try:
                    resp = client.post(f"{url.rstrip('/')}{path}", json=body)
                    if resp.status_code >= 500:
                        failed += 1
                        errors.append(f"HTTP {resp.status_code}: {resp.text[:120]}")
                except Exception as exc:
                    failed += 1
                    errors.append(f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return {
            "sent": sent,
            "failed": failed,
            "error_rate": None,
            "detail": f"probe unavailable: {exc}",
            "errors": errors[:3],
        }

    return {
        "sent": sent,
        "failed": failed,
        "error_rate": (failed / sent) if sent else None,
        "errors": errors[:3],
    }


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

    probe = _probe(service, url)
    health = _health(url)
    prom = get_service_error_rate.invoke({"service": service})
    prom_err = _extract_error_rate(prom if isinstance(prom, dict) else {})
    probe_err = probe.get("error_rate")

    # Only this round's actions — executed_actions accumulates across rounds.
    actions = state.get("last_executed_actions")
    if actions is None:
        actions = state.get("executed_actions") or []
    had_success = any(
        a.get("status") == "success"
        and a.get("action_type") in {"restart_service", "rollback_deploy"}
        for a in actions
    )

    notes: list[str] = []
    notes.append("health ok" if health.get("ok") else f"health failed: {health}")

    if probe_err is not None:
        notes.append(
            f"probe: {probe['failed']}/{probe['sent']} requests failed "
            f"(error_rate={probe_err:.2f})"
        )
        recovered = bool(probe_err < RECOVERED_ERROR_RATE and health.get("ok"))
    elif prom_err is not None:
        notes.append(f"prometheus error_rate={prom_err} (probe unavailable)")
        recovered = bool(prom_err < RECOVERED_ERROR_RATE and health.get("ok"))
    else:
        notes.append(
            "no probe or Prometheus signal — cannot confirm recovery"
        )
        if probe.get("detail"):
            notes.append(str(probe["detail"]))
        recovered = False

    if prom_err is not None and probe_err is not None:
        notes.append(f"prometheus error_rate={prom_err}")

    if settings.dry_run and any(a.get("status") == "dry_run" for a in actions):
        notes.append(
            "DRY_RUN: remediation was simulated, not applied — "
            "any recovery here is not attributable to the plan"
        )
    elif recovered and not had_success:
        notes.append(
            "recovered without a successful remediation action — may be transient"
        )

    result = VerificationResult(
        recovered=recovered,
        error_rate=probe_err if probe_err is not None else prom_err,
        notes="; ".join(notes),
        evidence=[str(health), str(probe), str(prom)[:500]],
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
