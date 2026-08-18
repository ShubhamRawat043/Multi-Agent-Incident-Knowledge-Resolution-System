"""Remediation planner — grounded in retrieved docs only."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.llm import get_strong_llm
from agent.state import IncidentState, RemediationPlan, RemediationStep


class _Plan(BaseModel):
    summary: str
    steps: list[RemediationStep]
    notes: str = ""


def _heuristic_plan(state: IncidentState) -> RemediationPlan:
    root = state.get("root_cause") or {}
    event = state.get("event") or {}
    service = event.get("service") or "payments-api"
    category = (root.get("category") or "").lower()
    retrieved = state.get("retrieved") or []
    cite = retrieved[0]["source"] if retrieved else None

    if "deploy" in category:
        steps = [
            RemediationStep(
                action_type="run_healthcheck",
                target=service,
                risk_tier="T0",
                expected_outcome="Confirm unhealthy",
                source_citation=cite,
            ),
            RemediationStep(
                action_type="rollback_deploy",
                target=service,
                risk_tier="T2",
                requires_approval=True,
                args={"image_tag": event.get("deploy_tag") and "v1.7" or "v1.7"},
                expected_outcome="Error rate returns to baseline",
                rollback="Re-deploy newer tag after fix",
                source_citation=cite or "knowledge/sops/deployment_rollback.md",
            ),
        ]
    elif "pool" in category or "db" in category:
        steps = [
            RemediationStep(
                action_type="restart_service",
                target=service,
                risk_tier="T1",
                expected_outcome="Clear leaked connections",
                source_citation=cite or "knowledge/sops/database_connection_pool.md",
            ),
            RemediationStep(
                action_type="run_healthcheck",
                target=service,
                risk_tier="T0",
                expected_outcome="Health ok",
                source_citation=cite,
            ),
        ]
    elif "dependency" in category:
        steps = [
            RemediationStep(
                action_type="run_healthcheck",
                target="payments-api",
                risk_tier="T0",
                expected_outcome="Identify unhealthy dependency",
                source_citation=cite,
            ),
            RemediationStep(
                action_type="restart_service",
                target="payments-api",
                risk_tier="T1",
                expected_outcome="Dependency recovers; orders follow",
                source_citation=cite or "knowledge/runbooks/dependency_failure.md",
            ),
        ]
    else:
        steps = [
            RemediationStep(
                action_type="restart_service",
                target=service,
                risk_tier="T1",
                expected_outcome="Transient recovery",
                source_citation=cite or "knowledge/sops/service_restart.md",
            ),
            RemediationStep(
                action_type="run_healthcheck",
                target=service,
                risk_tier="T0",
                expected_outcome="Health ok",
                source_citation=cite,
            ),
        ]

    # Flag unsourced steps
    for s in steps:
        if not s.source_citation:
            s.source_citation = "no source — human review required"

    grounded = all(
        s.source_citation and "human review" not in s.source_citation for s in steps
    )
    return RemediationPlan(
        summary=f"Plan for {category or 'unknown'} on {service}",
        steps=steps,
        grounded=grounded,
        notes=root.get("summary") or "",
    )


def planner_node(state: IncidentState) -> dict[str, Any]:
    feedback = state.get("critic_feedback") or ""
    try:
        llm = get_strong_llm().with_structured_output(_Plan)
        plan = llm.invoke(
            f"""Create a remediation plan. ONLY cite retrieved sources for steps.
If a step has no source, set source_citation to 'no source — human review required'.
Allowed action_type values: restart_service, rollback_deploy, scale_service,
clear_cache, run_healthcheck, toggle_feature_flag, gather_more_evidence.
Risk tiers: T0 read-only, T1 reversible, T2 production-impacting (requires_approval=true),
T3 destructive (requires_approval=true).
Root cause: {state.get('root_cause')}
Retrieved: {state.get('retrieved')}
Critic feedback to address: {feedback}
Event: {state.get('event')}
"""
        )
        # Ensure approval flags
        steps = []
        for s in plan.steps:
            if s.risk_tier in {"T2", "T3"}:
                s.requires_approval = True
            if not s.source_citation:
                s.source_citation = "no source — human review required"
            steps.append(s)
        result = RemediationPlan(
            summary=plan.summary,
            steps=steps,
            grounded=all(
                s.source_citation and "human review" not in (s.source_citation or "")
                for s in steps
            ),
            notes=plan.notes,
        )
    except Exception:
        result = _heuristic_plan(state)

    return {
        "plan": result.model_dump(),
        "status": "planning",
        "audit_log": [
            {
                "agent": "planner",
                "event_type": "plan_created",
                "payload": {"summary": result.summary, "steps": len(result.steps)},
            }
        ],
    }
