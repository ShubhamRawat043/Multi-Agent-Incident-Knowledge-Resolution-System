"""Investigation fan-out: run 4 specialists in parallel onto the blackboard."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from agent.specialists.change_correlator import run_change_correlator
from agent.specialists.dependency_analyst import run_dependency_analyst
from agent.specialists.log_analyst import run_log_analyst
from agent.specialists.metrics_analyst import run_metrics_analyst
from agent.state import IncidentState
from tools.redaction import redact


def _context(state: IncidentState) -> str:
    event = state.get("event") or {}
    return redact(
        f"""Incident id: {state.get('incident_id')}
Service: {event.get('service')}
Title: {event.get('title')}
Kind: {event.get('kind')}
Deploy tag: {event.get('deploy_tag')}
Deploy sha: {event.get('deploy_sha')}
Stacktrace:
{event.get('stacktrace') or '(none)'}
Known issue hint: {state.get('matched_known_issue')}
Prior hypotheses: {state.get('hypotheses')}
"""
    )


def investigate_node(state: IncidentState) -> dict[str, Any]:
    ctx = _context(state)
    runners = {
        "LogAnalyst": run_log_analyst,
        "MetricsAnalyst": run_metrics_analyst,
        "ChangeCorrelator": run_change_correlator,
        "DependencyAnalyst": run_dependency_analyst,
    }
    hypotheses: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, ctx): name for name, fn in runners.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                hypotheses.append(fut.result())
            except Exception as exc:
                hypotheses.append(
                    {
                        "statement": f"{name} failed: {exc}",
                        "evidence": [],
                        "confidence": 0.1,
                        "source_agent": name,
                    }
                )

    return {
        "hypotheses": hypotheses,
        "investigation_rounds": int(state.get("investigation_rounds") or 0) + 1,
        "status": "investigating",
        "audit_log": [
            {
                "agent": "investigate",
                "event_type": "blackboard_update",
                "payload": {"count": len(hypotheses)},
            }
        ],
    }
