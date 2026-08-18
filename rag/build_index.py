"""Offline ingestion of knowledge/ into Postgres + pgvector."""
from __future__ import annotations

from pathlib import Path

from rag.store import get_conn, get_embeddings, index_chunks, make_splitter, upsert_document

KNOWLEDGE_ROOT = Path(__file__).resolve().parents[1] / "knowledge"
DOC_TYPE_DIRS = {
    "sops": "sop",
    "runbooks": "runbook",
    "historical_rca": "historical_rca",
}


def _infer_service(text: str, path: Path) -> str | None:
    for svc in ("payments-api", "orders-api", "postgres"):
        if svc in text.lower() or svc.replace("-", "_") in path.stem:
            return svc
    # historical naming hints
    if "payments" in path.stem or "deploy" in path.stem:
        return "payments-api"
    if "orders" in path.stem or "memory" in path.stem:
        return "orders-api"
    if "db" in path.stem or "postgres" in path.stem or "pool" in path.stem:
        return "payments-api"
    return None


def _title_from_md(content: str, fallback: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def clear_knowledge(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings")
        cur.execute("DELETE FROM knowledge_documents")
    conn.commit()


def build_index(clear: bool = True) -> dict:
    embeddings = get_embeddings()
    splitter = make_splitter()
    stats = {"docs": 0, "chunks": 0, "by_type": {}}

    with get_conn() as conn:
        if clear:
            clear_knowledge(conn)

        for folder, doc_type in DOC_TYPE_DIRS.items():
            directory = KNOWLEDGE_ROOT / folder
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                content = path.read_text(encoding="utf-8")
                title = _title_from_md(content, path.stem)
                service = _infer_service(content, path)
                doc_id = upsert_document(
                    conn,
                    doc_type=doc_type,
                    title=title,
                    content=content,
                    source_path=str(path.relative_to(KNOWLEDGE_ROOT.parent)),
                    service=service,
                    metadata={"filename": path.name},
                )
                chunks = splitter.split_text(content)
                if not chunks:
                    chunks = [content]
                n = index_chunks(
                    conn,
                    doc_id=doc_id,
                    chunks=chunks,
                    doc_type=doc_type,
                    service=service,
                    source=str(path),
                    embeddings=embeddings,
                )
                stats["docs"] += 1
                stats["chunks"] += n
                stats["by_type"][doc_type] = stats["by_type"].get(doc_type, 0) + 1
                print(f"Indexed {path.name} ({doc_type}) → {n} chunks")

    return stats


def main() -> None:
    stats = build_index(clear=True)
    print("Done:", stats)


if __name__ == "__main__":
    main()
