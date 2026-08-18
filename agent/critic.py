"""Safety Critic — adversarial review of remediation plans (capped revisions)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.config import settings
from agent.llm import get_strong_llm
from agent.state import CriticVerdict, IncidentState


class _Verdict(BaseModel):
    approved: bool
    reason: str
    required_changes: list[str] = Field(default_factory=list)


def critic_node(state: IncidentState) -> dict[str, Any]:
    rounds = int(state.get("critic_rounds") or 0) + 1
    plan = state.get("plan") or {}
    root = state.get("root_cause") or {}
    confidence = float(state.get("confidence") or 0)

    # Hard cap: force HITL with current plan
    if rounds > settings.critic_max_revisions:
        return {
            "critic_rounds": rounds,
            "critic_feedback": "",
            "next_action": "execute",
            "audit_log": [
                {
                    "agent": "critic",
                    "event_type": "max_revisions",
                    "payload": {"rounds": rounds, "forced": True},
                }
            ],
        }

    # Deterministic safety checks
    issues: list[str] = []
    for step in plan.get("steps") or []:
        if step.get("action_type") == "rollback_deploy" and confidence < 0.7:
            issues.append("Rollback not justified at current confidence")
        if not step.get("source_citation") or "human review" in (
            step.get("source_citation") or ""
        ):
            if step.get("risk_tier") in {"T2", "T3"}:
                issues.append(f"High-risk step lacks solid citation: {step.get('action_type')}")
        if step.get("risk_tier") == "T3":
            issues.append("T3 destructive action — ensure RBAC approver")

    try:
        llm = get_strong_llm().with_structured_output(_Verdict)
        verdict = llm.invoke(
            f"""You are a Safety Critic. Adversarially review this remediation plan.
Reject if blast radius unjustified, evidence weak, or safer step should come first.
Root={root} Confidence={confidence} Plan={plan}
Known issues from static checks: {issues}
"""
        )
    except Exception:
        verdict = _Verdict(
            approved=len(issues) == 0,
            reason="; ".join(issues) if issues else "Static checks passed",
            required_changes=issues,
        )

    if issues and verdict.approved:
        # Prefer caution
        verdict = _Verdict(
            approved=False,
            reason=verdict.reason + " | " + "; ".join(issues),
            required_changes=list(set(verdict.required_changes + issues)),
        )

    if verdict.approved:
        return {
            "critic_rounds": rounds,
            "critic_feedback": "",
            "next_action": "execute",
            "audit_log": [
                {
                    "agent": "critic",
                    "event_type": "approved",
                    "payload": verdict.model_dump(),
                }
            ],
        }

    return {
        "critic_rounds": rounds,
        "critic_feedback": verdict.reason,
        "plan": None,  # force replanner
        "next_action": "plan",
        "audit_log": [
            {
                "agent": "critic",
                "event_type": "rejected",
                "payload": verdict.model_dump(),
            }
        ],
    }


def route_after_critic(state: IncidentState) -> str:
    if state.get("next_action") == "plan" and state.get("plan") is None:
        return "plan"
    return "supervisor"
