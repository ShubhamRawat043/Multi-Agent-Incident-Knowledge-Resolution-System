"""payments-api — downstream dependency of orders-api."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Allow importing common/faults when run from container or host
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.instrumentation import (  # noqa: E402
    DB_CONNECTIONS,
    ERROR_RATE,
    MEMORY_BYTES,
    instrument_app,
)
from faults.injectors import (  # noqa: E402
    apply_fault,
    clear_fault,
    get_fault,
    leaked_connection_count,
    memory_ballast_bytes,
    set_fault,
)

SERVICE = os.getenv("SERVICE_NAME", "payments-api")
app = FastAPI(title=SERVICE)
instrument_app(app, SERVICE)


class PayRequest(BaseModel):
    order_id: str
    amount: float
    currency: str = "USD"


class FaultRequest(BaseModel):
    mode: str


@app.on_event("startup")
def _sync_gauges():
    DB_CONNECTIONS.labels(service=SERVICE).set(leaked_connection_count())
    MEMORY_BYTES.labels(service=SERVICE).set(50_000_000 + memory_ballast_bytes())


@app.post("/pay")
def pay(body: PayRequest) -> dict:
    fault = apply_fault(SERVICE)
    DB_CONNECTIONS.labels(service=SERVICE).set(leaked_connection_count())
    MEMORY_BYTES.labels(service=SERVICE).set(50_000_000 + memory_ballast_bytes())

    if fault is not None:
        ERROR_RATE.labels(service=SERVICE).set(0.85)
        raise HTTPException(status_code=500, detail=str(fault))

    ERROR_RATE.labels(service=SERVICE).set(0.01)
    return {
        "status": "paid",
        "order_id": body.order_id,
        "amount": body.amount,
        "currency": body.currency,
        "image_tag": os.getenv("IMAGE_TAG", "v1.7"),
        "fault_mode": get_fault(),
    }


@app.post("/fault")
def inject_fault(body: FaultRequest) -> dict:
    mode = set_fault(body.mode)
    return {"service": SERVICE, "fault_mode": mode}


@app.delete("/fault")
def reset_fault() -> dict:
    mode = clear_fault()
    ERROR_RATE.labels(service=SERVICE).set(0.0)
    DB_CONNECTIONS.labels(service=SERVICE).set(0)
    MEMORY_BYTES.labels(service=SERVICE).set(50_000_000)
    return {"service": SERVICE, "fault_mode": mode}


@app.get("/info")
def info() -> dict:
    return {
        "service": SERVICE,
        "image_tag": os.getenv("IMAGE_TAG", "v1.7"),
        "fault_mode": get_fault(),
        "leaked_connections": leaked_connection_count(),
        "memory_ballast_bytes": memory_ballast_bytes(),
    }
