"""Retrieval agent — RAG over pgvector with service/doc_type filters."""
from __future__ import annotations

from typing import Any

from agent.state import IncidentState, RetrievedDoc
from tools.redaction import redact


def retrieval_node(state: IncidentState) -> dict[str, Any]:
    root = state.get("root_cause") or {}
    event = state.get("event") or {}
    service = event.get("service")
    query = f"{root.get('summary','')} {root.get('category','')} {service}"
    query = redact(query)

    docs: list[RetrievedDoc] = []
    try:
        from rag.store import similarity_search

        # Prefer runbooks/sops first, then historical RCA
        for doc_type in ("runbook", "sop", "historical_rca"):
            hits = similarity_search(query, k=3, service=service, doc_type=doc_type)
            for h in hits:
                docs.append(
                    RetrievedDoc(
                        content=redact(h["content"]),
                        source=h.get("source") or "",
                        doc_type=h.get("doc_type") or doc_type,
                        service=h.get("service"),
                        score=float(h.get("score") or 0),
                    )
                )
        if not docs:
            hits = similarity_search(query, k=5)
            for h in hits:
                docs.append(
                    RetrievedDoc(
                        content=redact(h["content"]),
                        source=h.get("source") or "",
                        doc_type=h.get("doc_type") or "unknown",
                        service=h.get("service"),
                        score=float(h.get("score") or 0),
                    )
                )
    except Exception as exc:
        docs.append(
            RetrievedDoc(
                content=f"RAG unavailable ({exc}). Use built-in SOP heuristics.",
                source="fallback",
                doc_type="sop",
                score=0.0,
            )
        )

    # Dedup by source+snippet prefix
    seen = set()
    unique = []
    for d in docs:
        key = (d.source, d.content[:80])
        if key in seen:
            continue
        seen.add(key)
        unique.append(d.model_dump())

    return {
        "retrieved": unique[:8],
        "audit_log": [
            {
                "agent": "retrieval",
                "event_type": "retrieved",
                "payload": {"count": len(unique), "query": query[:200]},
            }
        ],
    }
