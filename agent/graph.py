"""
Main LangGraph: triage → supervisor loop → specialists/RAG/plan/critic/exec/verify → report.
"""
from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from agent.checkpointer import build_checkpointer
from agent.critic import critic_node
from agent.execution import execution_node
from agent.investigate import investigate_node
from agent.planner import planner_node
from agent.reporting import escalate_node, reporting_node
from agent.retrieval import retrieval_node
from agent.state import IncidentState
from agent.supervisor import route_supervisor, supervisor_node
from agent.synthesis import synthesis_node
from agent.triage import triage_node
from agent.verification import verification_node


def _after_triage(state: IncidentState) -> Literal["supervisor", "report"]:
    if state.get("is_duplicate"):
        return "report"
    return "supervisor"


def _after_plan_path(state: IncidentState) -> Literal["critic"]:
    return "critic"


def build_graph(checkpointer: Any | None = None):
    g = StateGraph(IncidentState)

    g.add_node("triage", triage_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("investigate", investigate_node)
    g.add_node("synthesis", synthesis_node)
    g.add_node("retrieve", retrieval_node)
    g.add_node("plan", planner_node)
    g.add_node("critic", critic_node)
    g.add_node("execute", execution_node)
    g.add_node("verify", verification_node)
    g.add_node("escalate", escalate_node)
    g.add_node("report", reporting_node)

    g.add_edge(START, "triage")
    g.add_conditional_edges(
        "triage",
        _after_triage,
        {"supervisor": "supervisor", "report": "report"},
    )

    g.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {
            "investigate": "investigate",
            "retrieve": "retrieve",
            "plan": "plan",
            "execute": "execute",
            "escalate": "escalate",
            "close": "report",
        },
    )

    g.add_edge("investigate", "synthesis")
    g.add_edge("synthesis", "supervisor")
    g.add_edge("retrieve", "supervisor")
    g.add_edge("plan", "critic")

    def _after_critic(state: IncidentState) -> Literal["plan", "supervisor"]:
        # critic clears plan and sets next_action=plan on reject
        if state.get("plan") is None and state.get("next_action") == "plan":
            return "plan"
        return "supervisor"

    g.add_conditional_edges(
        "critic",
        _after_critic,
        {"plan": "plan", "supervisor": "supervisor"},
    )

    g.add_edge("execute", "verify")
    g.add_edge("verify", "supervisor")
    g.add_edge("escalate", "report")
    g.add_edge("report", END)

    cp = checkpointer if checkpointer is not None else build_checkpointer()
    return g.compile(checkpointer=cp)


# Singleton used by ingestion + UI
incident_graph = build_graph()


def initial_state(incident_id: str, event: dict) -> IncidentState:
    return {
        "incident_id": incident_id,
        "event": event,
        "is_duplicate": False,
        "matched_known_issue": None,
        "hypotheses": [],
        "root_cause": None,
        "investigation_rounds": 0,
        "retrieved": [],
        "plan": None,
        "critic_rounds": 0,
        "critic_feedback": "",
        "executed_actions": [],
        "audit_log": [],
        "next_action": "investigate",
        "confidence": 0.0,
        "supervisor_rounds": 0,
        "verification_retries": 0,
        "status": "triage",
        "verification": None,
        "rca_report_md": "",
        "approval_decision": None,
        "escalation_notes": "",
    }
