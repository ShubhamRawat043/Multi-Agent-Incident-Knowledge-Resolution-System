"""Shared Sentry + Prometheus instrumentation for demo services."""
from __future__ import annotations

import os
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["service", "method", "endpoint", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ERROR_RATE = Gauge(
    "http_error_rate",
    "Approximate recent error rate (0-1)",
    ["service"],
)
DB_CONNECTIONS = Gauge(
    "db_connections_in_use",
    "Simulated DB connections in use",
    ["service"],
)
MEMORY_BYTES = Gauge(
    "process_memory_bytes_sim",
    "Simulated memory usage",
    ["service"],
)


def init_sentry(service_name: str) -> None:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.2,
            environment="demo",
            release=os.getenv("IMAGE_TAG", "dev"),
            integrations=[FastApiIntegration()],
            server_name=service_name,
        )
    except Exception:
        # Demo must still run without Sentry configured
        pass


def capture_exception(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def instrument_app(app: FastAPI, service_name: str) -> None:
    init_sentry(service_name)
    ERROR_RATE.labels(service=service_name).set(0.0)
    DB_CONNECTIONS.labels(service=service_name).set(0)
    MEMORY_BYTES.labels(service=service_name).set(50_000_000)

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next: Callable):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception as exc:
            capture_exception(exc)
            raise
        finally:
            elapsed = time.perf_counter() - start
            endpoint = request.url.path
            REQUEST_COUNT.labels(
                service=service_name,
                method=request.method,
                endpoint=endpoint,
                status=str(status),
            ).inc()
            REQUEST_LATENCY.labels(service=service_name, endpoint=endpoint).observe(
                elapsed
            )

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": service_name,
            "image_tag": os.getenv("IMAGE_TAG", "dev"),
            "fault_mode": os.getenv("FAULT_MODE", "none"),
        }
