"""orders-api — depends on payments-api."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.instrumentation import ERROR_RATE, instrument_app  # noqa: E402
from faults.injectors import (  # noqa: E402
    apply_fault,
    clear_fault,
    get_fault,
    set_fault,
)

SERVICE = os.getenv("SERVICE_NAME", "orders-api")
PAYMENTS_URL = os.getenv("PAYMENTS_API_URL", "http://localhost:8002").rstrip("/")
app = FastAPI(title=SERVICE)
instrument_app(app, SERVICE)


class OrderRequest(BaseModel):
    item: str
    amount: float
    currency: str = "USD"


class FaultRequest(BaseModel):
    mode: str


@app.post("/orders")
def create_order(body: OrderRequest) -> dict:
    fault = apply_fault(SERVICE)
    if fault is not None:
        ERROR_RATE.labels(service=SERVICE).set(0.7)
        raise HTTPException(status_code=500, detail=str(fault))

    order_id = str(uuid4())
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{PAYMENTS_URL}/pay",
                json={
                    "order_id": order_id,
                    "amount": body.amount,
                    "currency": body.currency,
                },
            )
        if resp.status_code >= 500:
            ERROR_RATE.labels(service=SERVICE).set(0.6)
            raise HTTPException(
                status_code=502,
                detail=f"payments-api failure: {resp.text}",
            )
        payment = resp.json()
    except httpx.RequestError as exc:
        ERROR_RATE.labels(service=SERVICE).set(0.9)
        raise HTTPException(
            status_code=503,
            detail=f"payments-api unreachable: {exc}",
        ) from exc

    ERROR_RATE.labels(service=SERVICE).set(0.01)
    return {
        "status": "created",
        "order_id": order_id,
        "item": body.item,
        "payment": payment,
        "image_tag": os.getenv("IMAGE_TAG", "v1.0"),
        "fault_mode": get_fault(),
    }


@app.post("/fault")
def inject_fault(body: FaultRequest) -> dict:
    return {"service": SERVICE, "fault_mode": set_fault(body.mode)}


@app.delete("/fault")
def reset_fault() -> dict:
    ERROR_RATE.labels(service=SERVICE).set(0.0)
    return {"service": SERVICE, "fault_mode": clear_fault()}


@app.get("/info")
def info() -> dict:
    return {
        "service": SERVICE,
        "image_tag": os.getenv("IMAGE_TAG", "v1.0"),
        "fault_mode": get_fault(),
        "payments_url": PAYMENTS_URL,
    }
