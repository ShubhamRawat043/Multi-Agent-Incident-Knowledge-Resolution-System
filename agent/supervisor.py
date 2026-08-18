"""Supervisor / orchestrator — dynamic control loop with hard round caps."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.config import settings
from agent.llm import get_fast_llm
from agent.state import IncidentState, NextActionDecision


class _Decision(BaseModel):
    next_action: Literal[
        "investigate", "retrieve", "plan", "execute", "escalate", "close"
    ]
    reason: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


def supervisor_node(state: IncidentState) -> dict[str, Any]:
    rounds = int(state.get("supervisor_rounds") or 0) + 1
    max_rounds = settings.supervisor_max_rounds
    confidence = float(state.get("confidence") or 0.0)
    root = state.get("root_cause")
    plan = state.get("plan")
    verification = state.get("verification")
    inv_rounds = int(state.get("investigation_rounds") or 0)
    ver_retries = int(state.get("verification_retries") or 0)

    # Hard caps — never burn tokens forever
    if rounds > max_rounds:
        return {
            "supervisor_rounds": rounds,
            "next_action": "escalate",
            "status": "escalated",
            "escalation_notes": f"Supervisor hit max_rounds={max_rounds}",
            "audit_log": [
                {
                    "agent": "supervisor",
                    "event_type": "max_rounds",
                    "payload": {"rounds": rounds},
                }
            ],
        }

    if verification and verification.get("recovered"):
        return {
            "supervisor_rounds": rounds,
            "next_action": "close",
            "status": "resolved",
            "audit_log": [
                {
                    "agent": "supervisor",
                    "event_type": "resolved",
                    "payload": {"rounds": rounds},
                }
            ],
        }

    if (
        verification
        and not verification.get("recovered")
        and ver_retries >= settings.verification_max_retries
    ):
        return {
            "supervisor_rounds": rounds,
            "next_action": "escalate",
            "status": "escalated",
            "escalation_notes": "Verification failed after max retries",
            "audit_log": [
                {
                    "agent": "supervisor",
                    "event_type": "verification_exhausted",
                    "payload": {"retries": ver_retries},
                }
            ],
        }

    # Deterministic policy first (saves tokens); LLM only if ambiguous
    decision: _Decision | None = None

    if root is None:
        if inv_rounds >= settings.investigation_max_rounds:
            decision = _Decision(
                next_action="escalate",
                reason="Investigation rounds exhausted without root cause",
                confidence=confidence,
            )
        else:
            decision = _Decision(
                next_action="investigate",
                reason="No root cause yet — investigate",
                confidence=confidence,
            )
    elif confidence < settings.confidence_investigate:
        if inv_rounds < settings.investigation_max_rounds:
            decision = _Decision(
                next_action="investigate",
                reason="Confidence below investigate threshold",
                confidence=confidence,
            )
        else:
            decision = _Decision(
                next_action="escalate",
                reason="Low confidence and investigation capped",
                confidence=confidence,
            )
    elif not state.get("retrieved"):
        decision = _Decision(
            next_action="retrieve",
            reason="Root cause present — retrieve SOPs/runbooks",
            confidence=confidence,
        )
    elif plan is None:
        decision = _Decision(
            next_action="plan",
            reason="Knowledge retrieved — plan remediation",
            confidence=confidence,
        )
    elif verification and not verification.get("recovered"):
        # Failed remediations: re-investigate if budget remains
        if inv_rounds < settings.investigation_max_rounds:
            decision = _Decision(
                next_action="investigate",
                reason="Verification failed — re-investigate",
                confidence=confidence,
            )
        else:
            decision = _Decision(
                next_action="escalate",
                reason="Verification failed and no investigation budget left",
                confidence=confidence,
            )
    else:
        decision = _Decision(
            next_action="execute",
            reason="Plan ready — execute",
            confidence=confidence,
        )

    # Optional LLM refinement when confidence is mid-band
    if (
        settings.confidence_investigate <= confidence < settings.confidence_proceed
        and root is not None
        and decision.next_action in {"investigate", "retrieve", "plan"}
    ):
        try:
            llm = get_fast_llm().with_structured_output(_Decision)
            prompt = (
                "You are the incident supervisor. Choose the next_action.\n"
                f"Confidence={confidence}, inv_rounds={inv_rounds}, "
                f"has_root={bool(root)}, has_plan={bool(plan)}, "
                f"retrieved={bool(state.get('retrieved'))}.\n"
                f"Root cause: {root}\n"
                "Prefer retrieve→plan when confidence is medium and knowledge missing.\n"
                "Escalate only if stuck."
            )
            decision = llm.invoke(prompt)
        except Exception:
            pass

    status_map = {
        "investigate": "investigating",
        "retrieve": "investigating",
        "plan": "planning",
        "execute": "planning",
        "escalate": "escalated",
        "close": "resolved" if (verification or {}).get("recovered") else state.get("status", "triage"),
    }

    return {
        "supervisor_rounds": rounds,
        "next_action": decision.next_action,
        "confidence": decision.confidence or confidence,
        "status": status_map.get(decision.next_action, state.get("status", "investigating")),
        "audit_log": [
            {
                "agent": "supervisor",
                "event_type": "decision",
                "payload": decision.model_dump(),
            }
        ],
    }


def route_supervisor(
    state: IncidentState,
) -> Literal["investigate", "retrieve", "plan", "execute", "escalate", "close"]:
    return state.get("next_action") or "investigate"
