"""Normalize inbound webhooks into IncidentEvent."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from agent.state import IncidentEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(*parts: str) -> str:
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_sentry(payload: dict[str, Any]) -> IncidentEvent:
    data = payload.get("data") or payload
    event = data.get("event") or data
    issue = data.get("issue") or {}
    title = (
        event.get("title")
        or issue.get("title")
        or payload.get("message")
        or "Sentry alert"
    )
    culprit = event.get("culprit") or issue.get("culprit") or ""
    service = (
        (event.get("tags") or {}).get("server_name")
        or (event.get("server_name"))
        or _guess_service(title + " " + culprit)
    )
    stack = ""
    entries = event.get("entries") or []
    for entry in entries:
        if entry.get("type") == "exception":
            stack = json.dumps(entry.get("data"))[:4000]
            break
    if not stack:
        stack = str(event.get("exception") or event.get("message") or "")[:4000]

    fp = issue.get("fingerprint") or event.get("fingerprint")
    if isinstance(fp, list):
        fp = _fingerprint(*[str(x) for x in fp])
    if not fp:
        fp = _fingerprint("sentry", service, title)

    return IncidentEvent(
        source="sentry",
        service=service,
        kind="error",
        title=str(title)[:300],
        stacktrace=stack or None,
        first_seen=_now(),
        fingerprint=fp,
        raw=payload,
    )


def normalize_alertmanager(payload: dict[str, Any]) -> IncidentEvent:
    alerts = payload.get("alerts") or [payload]
    alert = alerts[0]
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    service = labels.get("service") or labels.get("job") or "unknown"
    title = annotations.get("summary") or labels.get("alertname") or "Prometheus alert"
    fp = _fingerprint("am", service, labels.get("alertname", title))
    return IncidentEvent(
        source="alertmanager",
        service=service,
        kind="metric",
        title=str(title)[:300],
        metric_series={"labels": labels, "annotations": annotations},
        first_seen=alert.get("startsAt") or _now(),
        fingerprint=fp,
        raw=payload,
    )


def normalize_github(payload: dict[str, Any]) -> IncidentEvent:
    deploy = payload.get("deployment") or payload
    sha = deploy.get("sha") or payload.get("after") or ""
    env = deploy.get("environment") or "production"
    service = _guess_service(json.dumps(payload)) or "payments-api"
    title = f"Deploy {sha[:8] or 'unknown'} to {env}"
    fp = _fingerprint("gh", service, sha or title)
    return IncidentEvent(
        source="github",
        service=service,
        kind="deploy",
        title=title,
        deploy_sha=sha or None,
        deploy_tag=(deploy.get("payload") or {}).get("image_tag"),
        first_seen=_now(),
        fingerprint=fp,
        raw=payload,
    )


def normalize_manual(payload: dict[str, Any]) -> IncidentEvent:
    service = payload.get("service") or _guess_service(str(payload)) or "payments-api"
    title = payload.get("title") or "Manual incident"
    fp = payload.get("fingerprint") or _fingerprint("manual", service, title, _now())
    return IncidentEvent(
        source=payload.get("source") or "manual",
        service=service,
        kind=payload.get("kind") or "manual",
        title=title,
        stacktrace=payload.get("stacktrace"),
        metric_series=payload.get("metric_series"),
        deploy_sha=payload.get("deploy_sha"),
        deploy_tag=payload.get("deploy_tag"),
        first_seen=payload.get("first_seen") or _now(),
        fingerprint=fp,
        raw=payload,
    )


def _guess_service(text: str) -> str:
    t = text.lower()
    if "orders" in t:
        return "orders-api"
    if "payment" in t:
        return "payments-api"
    return "payments-api"


def normalize(source: str, payload: dict[str, Any]) -> IncidentEvent:
    source = (source or "manual").lower()
    if source == "sentry":
        return normalize_sentry(payload)
    if source in {"alertmanager", "prometheus"}:
        return normalize_alertmanager(payload)
    if source == "github":
        return normalize_github(payload)
    return normalize_manual(payload)
