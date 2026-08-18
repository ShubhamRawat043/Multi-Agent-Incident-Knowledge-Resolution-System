"""FastAPI ingestion: webhooks + HITL resume API."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent.graph import incident_graph, initial_state
from agent.state import IncidentEvent
from ingestion.dedup import append_audit, upsert_incident
from ingestion.normalize import normalize
from observability.langsmith import incident_run_config

app = FastAPI(title="Incident Ingestion API", version="1.0.0")


class ManualIncident(BaseModel):
    service: str = "payments-api"
    title: str
    kind: str = "manual"
    stacktrace: Optional[str] = None
    deploy_tag: Optional[str] = None
    deploy_sha: Optional[str] = None
    fingerprint: Optional[str] = None
    source: str = "manual"


class ApproveBody(BaseModel):
    incident_id: str
    decision: str = Field(description="yes / no / ack / notes")
    approver: str = "operator"
    role: str = "oncall"
    notes: Optional[str] = None


def _invoke_new(event: IncidentEvent) -> dict[str, Any]:
    incident_id = f"INC-{uuid.uuid4().hex[:10]}"
    upsert_incident(incident_id, event, status="triage")
    append_audit(
        incident_id,
        "ingestion",
        "received",
        {"source": event.source, "title": event.title},
    )
    state = initial_state(incident_id, event.model_dump())
    config = incident_run_config(incident_id)
    result = incident_graph.invoke(state, config=config)
    return _public_result(incident_id, result, config)


def _public_result(incident_id: str, result: dict, config: dict) -> dict[str, Any]:
    interrupts = result.get("__interrupt__") or []
    pending = None
    if interrupts:
        pending = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
    return {
        "incident_id": incident_id,
        "status": result.get("status"),
        "confidence": result.get("confidence"),
        "next_action": result.get("next_action"),
        "root_cause": result.get("root_cause"),
        "plan": result.get("plan"),
        "hypotheses": result.get("hypotheses"),
        "verification": result.get("verification"),
        "rca_report_md": result.get("rca_report_md"),
        "pending_interrupt": pending,
        "thread_id": config["configurable"]["thread_id"],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/{source}")
async def webhook(source: str, payload: dict[str, Any]) -> dict:
    event = normalize(source, payload)
    return _invoke_new(event)


@app.post("/incidents")
async def create_incident(body: ManualIncident) -> dict:
    event = normalize("manual", body.model_dump())
    return _invoke_new(event)


@app.post("/approve")
async def approve(body: ApproveBody) -> dict:
    config = incident_run_config(body.incident_id)
    resume_payload = {
        "decision": body.decision,
        "approver": body.approver,
        "role": body.role,
        "notes": body.notes,
    }
    # For escalation ack, string is fine; for approvals prefer dict
    resume_value: Any
    if body.decision.lower() in {"ack"}:
        resume_value = body.notes or "ack"
    else:
        resume_value = resume_payload

    append_audit(
        body.incident_id,
        "hitl",
        "resume",
        resume_payload,
    )
    result = incident_graph.invoke(Command(resume=resume_value), config=config)
    return _public_result(body.incident_id, result, config)


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str) -> dict:
    config = incident_run_config(incident_id)
    snap = incident_graph.get_state(config)
    values = snap.values or {}
    if not values:
        raise HTTPException(status_code=404, detail="Incident not found in checkpointer")
    pending = None
    if snap.tasks:
        for t in snap.tasks:
            if getattr(t, "interrupts", None):
                pending = t.interrupts[0].value if t.interrupts else None
    return {
        "incident_id": incident_id,
        "status": values.get("status"),
        "confidence": values.get("confidence"),
        "root_cause": values.get("root_cause"),
        "plan": values.get("plan"),
        "hypotheses": values.get("hypotheses"),
        "executed_actions": values.get("executed_actions"),
        "verification": values.get("verification"),
        "rca_report_md": values.get("rca_report_md"),
        "audit_log": values.get("audit_log"),
        "pending_interrupt": pending,
        "next": list(snap.next) if snap.next else [],
    }


@app.get("/incidents")
async def list_incidents() -> dict:
    """Best-effort list from Postgres incidents table."""
    try:
        import psycopg
        from agent.config import settings

        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, service, title, status, confidence, created_at
                    FROM incidents
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
                rows = cur.fetchall()
        return {
            "incidents": [
                {
                    "id": r[0],
                    "service": r[1],
                    "title": r[2],
                    "status": r[3],
                    "confidence": r[4],
                    "created_at": r[5].isoformat() if r[5] else None,
                }
                for r in rows
            ]
        }
    except Exception as exc:
        return {"incidents": [], "error": str(exc)}
