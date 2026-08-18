"""Synthesis / RCA — reconcile competing hypotheses with confidence."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.llm import get_strong_llm
from agent.state import IncidentState, RootCause


class _Root(BaseModel):
    summary: str
    category: str = "unknown"
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_hypotheses: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    notes: str = ""


def synthesis_node(state: IncidentState) -> dict[str, Any]:
    hypos = state.get("hypotheses") or []
    event = state.get("event") or {}

    # Heuristic blend if LLM unavailable
    def heuristic() -> RootCause:
        if not hypos:
            return RootCause(
                summary="Unknown — no hypotheses",
                category="unknown",
                confidence=0.2,
            )
        ranked = sorted(hypos, key=lambda h: float(h.get("confidence") or 0), reverse=True)
        top = ranked[0]
        # Boost when ChangeCorrelator + LogAnalyst agree on deploy
        texts = " ".join(
            f"{h.get('statement','')} {' '.join(h.get('evidence') or [])}" for h in hypos
        ).lower()
        conf = float(top.get("confidence") or 0.5)
        category = "unknown"
        if "deploy" in texts or "v1.8" in texts or "rollback" in texts:
            category = "bad_deployment"
            conf = min(0.95, conf + 0.1)
        elif "pool" in texts or "connection" in texts:
            category = "db_pool_exhaustion"
        elif "dependency" in texts or "unreachable" in texts:
            category = "dependency_failure"
        elif "memory" in texts:
            category = "memory_leak"
        elif "latency" in texts:
            category = "high_latency"
        elif "config" in texts:
            category = "config_error"
        return RootCause(
            summary=top.get("statement") or "See hypotheses",
            category=category,
            confidence=conf,
            supporting_hypotheses=[h.get("source_agent", "") for h in ranked[:3]],
            evidence=[e for h in ranked[:3] for e in (h.get("evidence") or [])][:8],
        )

    root: RootCause
    try:
        llm = get_strong_llm().with_structured_output(_Root)
        result = llm.invoke(
            f"""Reconcile these competing incident hypotheses into ONE root cause.
Prefer evidence convergence. If MetricsAnalyst says CPU but ChangeCorrelator+LogAnalyst
point at a bad deploy, treat CPU as a symptom.
Service={event.get('service')} Title={event.get('title')}
Hypotheses={hypos}
Return category one of: bad_deployment, db_pool_exhaustion, dependency_failure,
memory_leak, high_latency, config_error, application_exception, unknown.
"""
        )
        root = RootCause(
            summary=result.summary,
            category=result.category,
            confidence=result.confidence,
            supporting_hypotheses=result.supporting_hypotheses,
            evidence=result.evidence,
        )
    except Exception:
        root = heuristic()

    return {
        "root_cause": root.model_dump(),
        "confidence": root.confidence,
        "audit_log": [
            {
                "agent": "synthesis",
                "event_type": "root_cause",
                "payload": root.model_dump(),
            }
        ],
    }
