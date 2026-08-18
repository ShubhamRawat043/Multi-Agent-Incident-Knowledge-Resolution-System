"""Incident dedup / correlation helpers (Postgres-backed)."""
from __future__ import annotations

from typing import Any, Optional

import psycopg

from agent.config import settings
from agent.state import IncidentEvent, KnownIssue


def get_conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def find_open_by_fingerprint(fingerprint: str) -> Optional[dict[str, Any]]:
    if not fingerprint:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, status, title, service
                    FROM incidents
                    WHERE fingerprint = %s
                      AND status NOT IN ('resolved', 'escalated')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (fingerprint,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "status": row[1],
                    "title": row[2],
                    "service": row[3],
                }
    except Exception:
        return None


def match_known_issue(event: IncidentEvent) -> Optional[KnownIssue]:
    """Keyword / learning-table match; semantic RAG match is done in triage agent."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, fingerprint, service, root_cause, remediation
                    FROM learnings
                    WHERE is_active = TRUE
                      AND (service = %s OR service IS NULL)
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (event.service,),
                )
                rows = cur.fetchall()
    except Exception:
        return None

    hay = f"{event.title} {event.stacktrace or ''}".lower()
    best: Optional[KnownIssue] = None
    best_score = 0.0
    for row in rows:
        text = f"{row[3] or ''} {row[4] or ''}".lower()
        tokens = [t for t in text.replace("/", " ").split() if len(t) > 4]
        hits = sum(1 for t in tokens if t in hay)
        score = hits / max(len(tokens), 1)
        if score > best_score and score >= 0.15:
            best_score = score
            best = KnownIssue(
                learning_id=row[0],
                fingerprint=row[1],
                service=row[2],
                root_cause=row[3],
                remediation=row[4],
                similarity=min(score, 0.95),
            )
    return best


def upsert_incident(incident_id: str, event: IncidentEvent, status: str = "triage") -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO incidents (id, fingerprint, source, service, kind, title, status, event)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        updated_at = NOW()
                    """,
                    (
                        incident_id,
                        event.fingerprint,
                        event.source,
                        event.service,
                        event.kind,
                        event.title,
                        status,
                        psycopg.types.json.Json(event.model_dump()),
                    ),
                )
            conn.commit()
    except Exception:
        # Allow running without DB during early bring-up
        pass


def append_audit(incident_id: str, agent: str, event_type: str, payload: dict) -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs (incident_id, agent, event_type, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    (
                        incident_id,
                        agent,
                        event_type,
                        psycopg.types.json.Json(payload),
                    ),
                )
            conn.commit()
    except Exception:
        pass
