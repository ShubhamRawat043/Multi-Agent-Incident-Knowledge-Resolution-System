"""Shared typed state and Pydantic contracts for the incident graph."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


# ---------- Pydantic contracts (inter-agent API) ----------


class IncidentEvent(BaseModel):
    source: Literal["sentry", "alertmanager", "github", "manual", "chaos"] = "manual"
    service: str
    kind: Literal["error", "metric", "deploy", "manual"] = "error"
    title: str
    stacktrace: Optional[str] = None
    metric_series: Optional[dict[str, Any]] = None
    deploy_sha: Optional[str] = None
    deploy_tag: Optional[str] = None
    first_seen: Optional[str] = None
    fingerprint: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class KnownIssue(BaseModel):
    learning_id: Optional[int] = None
    fingerprint: Optional[str] = None
    service: Optional[str] = None
    root_cause: Optional[str] = None
    remediation: Optional[str] = None
    similarity: float = 0.0


class Hypothesis(BaseModel):
    statement: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    source_agent: str


class RootCause(BaseModel):
    summary: str
    category: str = "unknown"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    supporting_hypotheses: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class RetrievedDoc(BaseModel):
    content: str
    source: str
    doc_type: str
    service: Optional[str] = None
    score: float = 0.0


class RemediationStep(BaseModel):
    action_type: Literal[
        "restart_service",
        "rollback_deploy",
        "scale_service",
        "clear_cache",
        "run_healthcheck",
        "toggle_feature_flag",
        "gather_more_evidence",
    ]
    target: str
    risk_tier: Literal["T0", "T1", "T2", "T3"] = "T1"
    requires_approval: bool = False
    preconditions: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    rollback: str = ""
    source_citation: Optional[str] = None
    args: dict[str, Any] = Field(default_factory=dict)


class RemediationPlan(BaseModel):
    summary: str
    steps: list[RemediationStep]
    grounded: bool = True
    notes: str = ""


class ActionResult(BaseModel):
    action_type: str
    target: str
    risk_tier: str = "T1"
    status: Literal["success", "failed", "denied", "cancelled", "dry_run", "awaiting_approval"]
    detail: str = ""
    dry_run: bool = True
    args: dict[str, Any] = Field(default_factory=dict)


class AuditEntry(BaseModel):
    agent: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    recovered: bool
    error_rate: Optional[float] = None
    notes: str = ""
    evidence: list[str] = Field(default_factory=list)


class CriticVerdict(BaseModel):
    approved: bool
    reason: str
    required_changes: list[str] = Field(default_factory=list)


class NextActionDecision(BaseModel):
    next_action: Literal[
        "investigate", "retrieve", "plan", "execute", "escalate", "close"
    ]
    reason: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


# ---------- LangGraph state ----------


def _add_hypotheses(
    left: list[Hypothesis] | list[dict] | None,
    right: list[Hypothesis] | list[dict] | None,
) -> list[dict]:
    out: list[dict] = []
    for item in list(left or []) + list(right or []):
        if isinstance(item, Hypothesis):
            out.append(item.model_dump())
        else:
            out.append(item)
    return out


def _add_dicts(
    left: list[dict] | None,
    right: list[dict] | None,
) -> list[dict]:
    return list(left or []) + list(right or [])


class IncidentState(TypedDict, total=False):
    incident_id: str
    event: dict  # IncidentEvent dump

    # triage
    is_duplicate: bool
    matched_known_issue: Optional[dict]

    # investigation blackboard
    hypotheses: Annotated[list[dict], _add_hypotheses]
    root_cause: Optional[dict]
    investigation_rounds: int

    # knowledge + plan
    retrieved: list[dict]
    plan: Optional[dict]
    critic_rounds: int
    critic_feedback: str

    # execution
    executed_actions: Annotated[list[dict], _add_dicts]
    audit_log: Annotated[list[dict], _add_dicts]

    # control
    next_action: Literal[
        "investigate", "retrieve", "plan", "execute", "escalate", "close"
    ]
    confidence: float
    supervisor_rounds: int
    verification_retries: int
    status: Literal[
        "triage",
        "investigating",
        "planning",
        "awaiting_approval",
        "resolved",
        "escalated",
    ]

    # output
    verification: Optional[dict]
    rca_report_md: str
    approval_decision: Optional[str]
    escalation_notes: str
