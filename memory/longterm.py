"""Long-term memory: distilled learnings + optional RCA re-index into pgvector."""
from __future__ import annotations

from typing import Optional

import psycopg

from agent.config import settings


def get_conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def write_learning(
    *,
    fingerprint: Optional[str],
    service: str,
    symptom: str,
    root_cause: str,
    remediation: str,
    rca_markdown: str = "",
    mttr_seconds: Optional[int] = None,
) -> None:
    """Dedup with simple is_new style check on symptom+root_cause+service."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM learnings
                    WHERE service = %s
                      AND root_cause = %s
                      AND symptom = %s
                      AND is_active = TRUE
                    LIMIT 1
                    """,
                    (service, root_cause, symptom),
                )
                existing = cur.fetchone()
                if existing:
                    return  # is_new = false
                cur.execute(
                    """
                    INSERT INTO learnings
                        (fingerprint, service, symptom, root_cause, remediation, mttr_seconds)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        fingerprint,
                        service,
                        symptom,
                        root_cause,
                        remediation,
                        mttr_seconds,
                    ),
                )
                learning_id = cur.fetchone()[0]
            conn.commit()
    except Exception:
        return

    # Re-index RCA into knowledge + embeddings when possible
    if rca_markdown.strip():
        try:
            from rag.store import (
                get_embeddings,
                index_chunks,
                make_splitter,
                upsert_document,
            )

            with get_conn() as conn:
                doc_id = upsert_document(
                    conn,
                    doc_type="historical_rca",
                    title=f"Auto RCA {fingerprint or learning_id}",
                    content=rca_markdown,
                    source_path=f"auto/{fingerprint or learning_id}.md",
                    service=service,
                    metadata={"auto": True, "learning_id": learning_id},
                )
                chunks = make_splitter().split_text(rca_markdown) or [rca_markdown]
                index_chunks(
                    conn,
                    doc_id=doc_id,
                    chunks=chunks,
                    doc_type="historical_rca",
                    service=service,
                    source=f"auto/{fingerprint or learning_id}.md",
                    embeddings=get_embeddings(),
                )
        except Exception:
            pass
