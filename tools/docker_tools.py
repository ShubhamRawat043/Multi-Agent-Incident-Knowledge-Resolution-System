"""
Demo-hardened Docker remediation tools.

Safety rails:
- DEMO_CONTAINER_ALLOWLIST + required label
- No generic shell/exec
- DRY_RUN default
- rollback only to known demo tags
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_core.tools import tool

from agent.config import settings

ALLOWED_ROLLBACK_TAGS = {"v1.0", "v1.7", "v1.8", "v1.6", "dev"}


def _client():
    import docker

    return docker.from_env()


def _parse_label_requirement() -> tuple[str, str]:
    raw = settings.demo_container_label  # e.g. incident-demo=true
    if "=" in raw:
        k, v = raw.split("=", 1)
        return k.strip(), v.strip()
    return raw.strip(), "true"


def _is_allowed(container) -> tuple[bool, str]:
    name = (container.name or "").strip()
    allow = settings.demo_container_allowlist
    name_ok = any(
        name == a or name.startswith(a) or a in name for a in allow
    )
    if not name_ok:
        return False, f"container '{name}' not in DEMO_CONTAINER_ALLOWLIST"

    key, val = _parse_label_requirement()
    labels = container.labels or {}
    if labels.get(key) != val:
        return False, f"container '{name}' missing required label {key}={val}"
    return True, "ok"


def _find_container(target: str):
    client = _client()
    # Try exact name, then fuzzy against allow-list
    candidates = [target, f"incident-{target}", target.replace("_", "-")]
    for cname in candidates:
        try:
            return client.containers.get(cname)
        except Exception:
            continue
    # Search by substring among running containers
    for c in client.containers.list(all=True):
        if target in (c.name or ""):
            return c
    raise ValueError(f"Container not found for target '{target}'")


def _guard(target: str):
    container = _find_container(target)
    ok, reason = _is_allowed(container)
    if not ok:
        raise PermissionError(reason)
    return container


@tool
def run_healthcheck(target: str) -> dict:
    """Run /health against a demo service container target (by service name). T0 read-only."""
    import time

    import httpx

    url_map = {
        "orders-api": settings.orders_api_url,
        "incident-orders-api": settings.orders_api_url,
        "payments-api": settings.payments_api_url,
        "incident-payments-api": settings.payments_api_url,
    }
    base = url_map.get(target, settings.payments_api_url if "payment" in target else settings.orders_api_url)

    # Docker reports a container "running" as soon as the process starts, but
    # the app inside (uvicorn) still needs a moment to bind its port — a real,
    # measured gap of roughly 1-1.5s right after container.restart() returns.
    # This tool is almost always called immediately after restart_service in
    # the heuristic remediation plan (agent/planner.py), including for the
    # two restart-curable fault types (db_pool_exhaustion, memory_leak), so a
    # single unretried request can misreport a genuinely-recovered service as
    # unreachable. Retry briefly instead of failing on the first miss.
    attempts = 4
    delay = 0.5
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{base.rstrip('/')}/health")
                return {
                    "action": "run_healthcheck",
                    "target": target,
                    "status": "success",
                    "http_status": resp.status_code,
                    "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
                    "dry_run": False,
                    "risk_tier": "T0",
                    "attempts": attempt + 1,
                }
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)

    return {
        "action": "run_healthcheck",
        "target": target,
        "status": "failed",
        "detail": str(last_exc),
        "risk_tier": "T0",
        "attempts": attempts,
    }


@tool
def restart_service(target: str) -> dict:
    """Restart an allow-listed demo container. T1. Honors DRY_RUN."""
    try:
        container = _guard(target)
    except Exception as exc:
        return {
            "action": "restart_service",
            "target": target,
            "status": "denied",
            "detail": str(exc),
            "dry_run": settings.dry_run,
            "risk_tier": "T1",
        }

    if settings.dry_run:
        return {
            "action": "restart_service",
            "target": container.name,
            "status": "dry_run",
            "detail": f"Would restart {container.name}",
            "dry_run": True,
            "risk_tier": "T1",
        }

    container.restart(timeout=20)
    return {
        "action": "restart_service",
        "target": container.name,
        "status": "success",
        "detail": f"Restarted {container.name}",
        "dry_run": False,
        "risk_tier": "T1",
    }


@tool
def rollback_deploy(target: str, image_tag: str) -> dict:
    """
    Roll back an allow-listed demo service to a known image tag (compose rebuild simulation).
    T2. Only tags in ALLOWED_ROLLBACK_TAGS. Honors DRY_RUN.
    """
    if image_tag not in ALLOWED_ROLLBACK_TAGS:
        return {
            "action": "rollback_deploy",
            "target": target,
            "status": "denied",
            "detail": f"image_tag '{image_tag}' not in allow-list {sorted(ALLOWED_ROLLBACK_TAGS)}",
            "dry_run": settings.dry_run,
            "risk_tier": "T2",
        }

    try:
        container = _guard(target)
    except Exception as exc:
        return {
            "action": "rollback_deploy",
            "target": target,
            "status": "denied",
            "detail": str(exc),
            "dry_run": settings.dry_run,
            "risk_tier": "T2",
        }

    # For the demo we set FAULT_MODE=none via the service /fault API and record the tag intent.
    # Full image swap would require compose rebuild; we simulate safely.
    import httpx

    url_map = {
        "orders-api": settings.orders_api_url,
        "incident-orders-api": settings.orders_api_url,
        "payments-api": settings.payments_api_url,
        "incident-payments-api": settings.payments_api_url,
    }
    base = url_map.get(target)
    if settings.dry_run:
        return {
            "action": "rollback_deploy",
            "target": container.name,
            "status": "dry_run",
            "detail": f"Would roll back {container.name} to tag {image_tag} and clear fault mode",
            "args": {"image_tag": image_tag},
            "dry_run": True,
            "risk_tier": "T2",
        }

    detail = f"Rollback requested to {image_tag}"
    if base:
        try:
            with httpx.Client(timeout=10.0) as client:
                client.delete(f"{base.rstrip('/')}/fault")
            detail += "; cleared fault_mode on service"
        except Exception as exc:
            detail += f"; fault clear failed: {exc}"

    return {
        "action": "rollback_deploy",
        "target": container.name,
        "status": "success",
        "detail": detail,
        "args": {"image_tag": image_tag},
        "dry_run": False,
        "risk_tier": "T2",
    }


@tool
def scale_service(target: str, replicas: int = 1) -> dict:
    """Scale is limited in compose demo — records intent / dry-run only unless explicitly enabled."""
    try:
        container = _guard(target)
    except Exception as exc:
        return {
            "action": "scale_service",
            "target": target,
            "status": "denied",
            "detail": str(exc),
            "risk_tier": "T1",
        }
    return {
        "action": "scale_service",
        "target": container.name,
        "status": "dry_run" if settings.dry_run else "success",
        "detail": f"Scale to {replicas} recorded (compose single-replica demo)",
        "args": {"replicas": replicas},
        "dry_run": settings.dry_run,
        "risk_tier": "T1",
    }


@tool
def clear_cache(target: str) -> dict:
    """Clear cache — demo no-op with audit. T1."""
    try:
        container = _guard(target)
    except Exception as exc:
        return {
            "action": "clear_cache",
            "target": target,
            "status": "denied",
            "detail": str(exc),
            "risk_tier": "T1",
        }
    return {
        "action": "clear_cache",
        "target": container.name,
        "status": "dry_run" if settings.dry_run else "success",
        "detail": "Cache clear simulated for demo service",
        "dry_run": settings.dry_run,
        "risk_tier": "T1",
    }


@tool
def toggle_feature_flag(target: str, flag: str, enabled: bool = False) -> dict:
    """Toggle a feature flag — demo records intent only. T1."""
    try:
        container = _guard(target)
    except Exception as exc:
        return {
            "action": "toggle_feature_flag",
            "target": target,
            "status": "denied",
            "detail": str(exc),
            "risk_tier": "T1",
        }
    return {
        "action": "toggle_feature_flag",
        "target": container.name,
        "status": "dry_run" if settings.dry_run else "success",
        "detail": f"Flag {flag} -> {enabled}",
        "args": {"flag": flag, "enabled": enabled},
        "dry_run": settings.dry_run,
        "risk_tier": "T1",
    }


docker_tools = [
    run_healthcheck,
    restart_service,
    rollback_deploy,
    scale_service,
    clear_cache,
    toggle_feature_flag,
]

ACTION_TOOL_MAP = {
    "run_healthcheck": run_healthcheck,
    "restart_service": restart_service,
    "rollback_deploy": rollback_deploy,
    "scale_service": scale_service,
    "clear_cache": clear_cache,
    "toggle_feature_flag": toggle_feature_flag,
}
