"""Triage & correlation: dedup + known-issue matching."""
from __future__ import annotations

from typing import Any

from ingestion.dedup import find_open_by_fingerprint, match_known_issue
from agent.state import IncidentEvent, IncidentState
from tools.redaction import redact


def triage_node(state: IncidentState) -> dict[str, Any]:
    event = IncidentEvent.model_validate(state["event"])
    event.title = redact(event.title)
    if event.stacktrace:
        event.stacktrace = redact(event.stacktrace)

    existing = find_open_by_fingerprint(event.fingerprint or "")
    known = match_known_issue(event)

    # Optional semantic boost via RAG if available
    if known is None:
        try:
            from rag.store import similarity_search

            hits = similarity_search(
                f"{event.service} {event.title}",
                k=3,
                service=event.service,
                doc_type="historical_rca",
            )
            if hits and hits[0]["score"] >= 0.75:
                known_dict = {
                    "learning_id": None,
                    "fingerprint": event.fingerprint,
                    "service": event.service,
                    "root_cause": hits[0]["content"][:300],
                    "remediation": None,
                    "similarity": hits[0]["score"],
                }
            else:
                known_dict = None
        except Exception:
            known_dict = None
    else:
        known_dict = known.model_dump()

    is_dup = existing is not None
    audit = {
        "agent": "triage",
        "event_type": "triage_complete",
        "payload": {
            "is_duplicate": is_dup,
            "existing_id": existing["id"] if existing else None,
            "known_issue": known_dict,
        },
    }

    # Fast-path for duplicates: close/attach without full investigation
    if is_dup:
        return {
            "is_duplicate": True,
            "matched_known_issue": known_dict,
            "status": "triage",
            "next_action": "close",
            "confidence": 0.9,
            "audit_log": [audit],
            "rca_report_md": (
                f"# Duplicate incident\n\nAttached to existing `{existing['id']}`.\n"
            ),
        }

    return {
        "is_duplicate": False,
        "matched_known_issue": known_dict,
        "status": "investigating",
        "next_action": "investigate",
        "confidence": known_dict["similarity"] if known_dict else 0.0,
        "supervisor_rounds": 0,
        "investigation_rounds": 0,
        "critic_rounds": 0,
        "verification_retries": 0,
        "audit_log": [audit],
    }
