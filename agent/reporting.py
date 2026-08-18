"""RCA report generation + learning write-back / re-index."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.state import IncidentState
from memory.longterm import write_learning
from ingestion.dedup import upsert_incident
from agent.state import IncidentEvent


def _render_rca(state: IncidentState) -> str:
    event = state.get("event") or {}
    root = state.get("root_cause") or {}
    plan = state.get("plan") or {}
    verification = state.get("verification") or {}
    hypos = state.get("hypotheses") or []
    actions = state.get("executed_actions") or []
    retrieved = state.get("retrieved") or []

    lines = [
        f"# RCA — {state.get('incident_id')}",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        f"**Service:** {event.get('service')}",
        f"**Title:** {event.get('title')}",
        f"**Status:** {state.get('status')}",
        f"**Confidence:** {state.get('confidence')}",
        "",
        "## Root cause",
        f"- Summary: {root.get('summary')}",
        f"- Category: {root.get('category')}",
        f"- Evidence: {root.get('evidence')}",
        "",
        "## Hypotheses (blackboard)",
    ]
    for h in hypos:
        lines.append(
            f"- **{h.get('source_agent')}** ({h.get('confidence')}): {h.get('statement')}"
        )

    lines += ["", "## Retrieved knowledge"]
    for d in retrieved[:5]:
        lines.append(
            f"- [{d.get('doc_type')}] {d.get('source')} (score={d.get('score'):.2f})"
            if isinstance(d.get("score"), (int, float))
            else f"- [{d.get('doc_type')}] {d.get('source')}"
        )

    lines += ["", "## Remediation plan", f"{plan.get('summary')}", ""]
    for i, s in enumerate(plan.get("steps") or [], 1):
        lines.append(
            f"{i}. `{s.get('action_type')}` on `{s.get('target')}` "
            f"(risk {s.get('risk_tier')}) — cite: {s.get('source_citation')}"
        )

    lines += ["", "## Executed actions"]
    for a in actions:
        lines.append(
            f"- {a.get('action_type')} → {a.get('status')} "
            f"(dry_run={a.get('dry_run')}): {a.get('detail')}"
        )

    lines += [
        "",
        "## Verification",
        f"- Recovered: {verification.get('recovered')}",
        f"- Notes: {verification.get('notes')}",
        "",
        "## Escalation",
        state.get("escalation_notes") or "_n/a_",
    ]
    return "\n".join(lines)


def reporting_node(state: IncidentState) -> dict[str, Any]:
    rca = _render_rca(state)
    incident_id = state.get("incident_id") or "unknown"
    event = IncidentEvent.model_validate(state.get("event") or {"service": "unknown", "title": "x"})
    status = state.get("status") or "resolved"
    if state.get("next_action") == "escalate":
        status = "escalated"
    elif (state.get("verification") or {}).get("recovered"):
        status = "resolved"

    try:
        upsert_incident(incident_id, event, status=status)
    except Exception:
        pass

    root = state.get("root_cause") or {}
    plan = state.get("plan") or {}
    try:
        write_learning(
            fingerprint=event.fingerprint,
            service=event.service,
            symptom=event.title,
            root_cause=root.get("summary") or "",
            remediation=plan.get("summary") or "",
            rca_markdown=rca,
        )
    except Exception:
        pass

    return {
        "rca_report_md": rca,
        "status": status,
        "audit_log": [
            {
                "agent": "reporting",
                "event_type": "rca_written",
                "payload": {"chars": len(rca), "status": status},
            }
        ],
    }


def escalate_node(state: IncidentState) -> dict[str, Any]:
    """HITL escalation gate before final report."""
    from langgraph.types import interrupt

    decision = interrupt(
        {
            "type": "escalation",
            "incident_id": state.get("incident_id"),
            "message": (
                "Incident requires human escalation. "
                "Acknowledge with 'ack' or provide notes."
            ),
            "notes": state.get("escalation_notes"),
            "root_cause": state.get("root_cause"),
        }
    )
    notes = decision if isinstance(decision, str) else str(
        (decision or {}).get("notes") or decision
    )
    return {
        "status": "escalated",
        "escalation_notes": notes,
        "next_action": "close",
        "audit_log": [
            {
                "agent": "escalate",
                "event_type": "escalated",
                "payload": {"notes": notes},
            }
        ],
    }
