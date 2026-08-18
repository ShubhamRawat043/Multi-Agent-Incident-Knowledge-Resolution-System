"""pgvector-backed RAG store and retriever."""
from __future__ import annotations

from typing import Any, Optional

import psycopg
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from agent.config import settings


EMBED_DIM = 1536


def get_conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(model="text-embedding-3-small")


def upsert_document(
    conn: psycopg.Connection,
    *,
    doc_type: str,
    title: str,
    content: str,
    source_path: str,
    service: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO knowledge_documents (doc_type, service, title, source_path, content, metadata)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                doc_type,
                service,
                title,
                source_path,
                content,
                psycopg.types.json.Json(metadata or {}),
            ),
        )
        doc_id = cur.fetchone()[0]
    conn.commit()
    return doc_id


def index_chunks(
    conn: psycopg.Connection,
    *,
    doc_id: int,
    chunks: list[str],
    doc_type: str,
    service: Optional[str],
    source: str,
    embeddings: OpenAIEmbeddings,
) -> int:
    vectors = embeddings.embed_documents(chunks)
    with conn.cursor() as cur:
        for i, (text, vec) in enumerate(zip(chunks, vectors)):
            cur.execute(
                """
                INSERT INTO embeddings
                    (doc_id, chunk_index, chunk_text, doc_type, service, source, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                """,
                (doc_id, i, text, doc_type, service, source, vec),
            )
    conn.commit()
    return len(chunks)


def similarity_search(
    query: str,
    *,
    k: int = 5,
    service: Optional[str] = None,
    doc_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Semantic retrieval with optional metadata filters."""
    embeddings = get_embeddings()
    qvec = embeddings.embed_query(query)

    params: list[Any] = [qvec]
    where_clauses: list[str] = []
    if service:
        where_clauses.append("service = %s")
        params.append(service)
    if doc_type:
        where_clauses.append("doc_type = %s")
        params.append(doc_type)
    where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
    params.extend([qvec, k])

    sql = f"""
        SELECT chunk_text, doc_type, service, source, doc_id,
               1 - (embedding <=> %s::vector) AS score
        FROM embeddings
        WHERE embedding IS NOT NULL{where_sql}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [
        {
            "content": r[0],
            "doc_type": r[1],
            "service": r[2],
            "source": r[3],
            "doc_id": r[4],
            "score": float(r[5]) if r[5] is not None else 0.0,
        }
        for r in rows
    ]


def make_splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        separators=["\n## ", "\n### ", "\n\n", "\n", " ", ""],
    )
