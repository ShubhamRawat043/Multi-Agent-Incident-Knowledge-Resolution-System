"""Execution agent — risk-tiered HITL + allow-listed Docker tools."""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from agent.config import settings
from agent.state import ActionResult, IncidentState
from tools.docker_tools import ACTION_TOOL_MAP


def _needs_hitl(step: dict, severity_hint: str = "") -> bool:
    tier = step.get("risk_tier") or "T1"
    if tier in {"T2", "T3"} or step.get("requires_approval"):
        return True
    if tier == "T1" and severity_hint.upper() in {"P1", "P2"}:
        return True
    return False


def execution_node(state: IncidentState) -> dict[str, Any]:
    plan = state.get("plan") or {}
    steps = plan.get("steps") or []
    results: list[dict] = []
    audit: list[dict] = []

    for step in steps:
        action_type = step.get("action_type")
        if action_type == "gather_more_evidence":
            results.append(
                ActionResult(
                    action_type=action_type,
                    target=step.get("target") or "",
                    risk_tier=step.get("risk_tier") or "T0",
                    status="success",
                    detail="Deferred to supervisor investigate",
                    dry_run=True,
                ).model_dump()
            )
            continue

        tool = ACTION_TOOL_MAP.get(action_type)
        if tool is None:
            results.append(
                ActionResult(
                    action_type=action_type or "unknown",
                    target=step.get("target") or "",
                    status="denied",
                    detail="Action not in allow-list",
                    dry_run=settings.dry_run,
                ).model_dump()
            )
            continue

        if _needs_hitl(step):
            decision = interrupt(
                {
                    "type": "approval_request",
                    "incident_id": state.get("incident_id"),
                    "step": step,
                    "message": (
                        f"Approve {action_type} on {step.get('target')} "
                        f"(risk {step.get('risk_tier')})? yes/no/modify"
                    ),
                }
            )
            # decision may be str or dict
            if isinstance(decision, dict):
                answer = str(decision.get("decision", "no")).lower()
                approver = decision.get("approver", "operator")
                role = decision.get("role", "oncall")
            else:
                answer = str(decision).lower().strip()
                approver, role = "operator", "oncall"

            audit.append(
                {
                    "agent": "execution",
                    "event_type": "hitl_decision",
                    "payload": {
                        "decision": answer,
                        "approver": approver,
                        "role": role,
                        "step": step,
                    },
                }
            )

            if answer not in {"yes", "y", "approve", "approved"}:
                results.append(
                    ActionResult(
                        action_type=action_type,
                        target=step.get("target") or "",
                        risk_tier=step.get("risk_tier") or "T2",
                        status="cancelled",
                        detail=f"Rejected by {approver}",
                        dry_run=settings.dry_run,
                        args=step.get("args") or {},
                    ).model_dump()
                )
                continue

            if step.get("risk_tier") == "T3" and role not in settings.t3_approver_roles:
                results.append(
                    ActionResult(
                        action_type=action_type,
                        target=step.get("target") or "",
                        risk_tier="T3",
                        status="denied",
                        detail=f"Approver role '{role}' not in T3_APPROVER_ROLES",
                        dry_run=settings.dry_run,
                    ).model_dump()
                )
                continue

        # Build tool args
        args = {"target": step.get("target")}
        extra = step.get("args") or {}
        if action_type == "rollback_deploy":
            args["image_tag"] = extra.get("image_tag") or "v1.7"
        elif action_type == "scale_service":
            args["replicas"] = int(extra.get("replicas") or 1)
        elif action_type == "toggle_feature_flag":
            args["flag"] = extra.get("flag") or "default"
            args["enabled"] = bool(extra.get("enabled", False))

        try:
            raw = tool.invoke(args)
        except Exception as exc:
            raw = {
                "action": action_type,
                "target": step.get("target"),
                "status": "failed",
                "detail": str(exc),
                "dry_run": settings.dry_run,
                "risk_tier": step.get("risk_tier"),
            }

        results.append(
            ActionResult(
                action_type=action_type,
                target=str(raw.get("target") or step.get("target") or ""),
                risk_tier=str(raw.get("risk_tier") or step.get("risk_tier") or "T1"),
                status=raw.get("status") or "failed",
                detail=str(raw.get("detail") or raw),
                dry_run=bool(raw.get("dry_run", settings.dry_run)),
                args=args,
            ).model_dump()
        )

    return {
        "executed_actions": results,
        "status": "investigating",
        "audit_log": audit
        + [
            {
                "agent": "execution",
                "event_type": "executed",
                "payload": {"count": len(results)},
            }
        ],
    }
