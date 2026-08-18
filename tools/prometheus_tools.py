"""Bounded Prometheus query tools."""
from __future__ import annotations

from typing import Any, Optional

import httpx
from langchain_core.tools import tool

from agent.config import settings


@tool
def query_prometheus(promql: str) -> dict:
    """Run an instant PromQL query against Prometheus. Read-only."""
    url = f"{settings.prometheus_url.rstrip('/')}/api/v1/query"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params={"query": promql})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"error": str(exc), "promql": promql, "fallback": "metrics unavailable"}
    return {"promql": promql, "data": data.get("data")}


@tool
def get_service_error_rate(service: str) -> dict:
    """Fetch http_error_rate gauge for a demo service. Read-only."""
    promql = f'http_error_rate{{service="{service}"}}'
    return query_prometheus.invoke({"promql": promql})


@tool
def get_service_latency(service: str) -> dict:
    """Fetch average request latency for a demo service. Read-only."""
    promql = (
        f'rate(http_request_duration_seconds_sum{{service="{service}"}}[5m])'
        f' / rate(http_request_duration_seconds_count{{service="{service}"}}[5m])'
    )
    return query_prometheus.invoke({"promql": promql})


@tool
def get_db_connections(service: str) -> dict:
    """Fetch simulated DB connections in use for a service. Read-only."""
    promql = f'db_connections_in_use{{service="{service}"}}'
    return query_prometheus.invoke({"promql": promql})


prometheus_tools = [
    query_prometheus,
    get_service_error_rate,
    get_service_latency,
    get_db_connections,
]
