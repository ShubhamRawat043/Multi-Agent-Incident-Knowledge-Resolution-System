"""Postgres checkpointer factory with in-memory fallback for local bring-up."""
from __future__ import annotations

from typing import Any

from agent.config import settings

_memory_singleton: Any = None
_pg_singleton: Any = None


def build_checkpointer() -> Any:
    """
    Prefer PostgresSaver when DB is reachable; else MemorySaver.
    Keeps a process-level singleton so HITL resume works across requests.
    """
    global _memory_singleton, _pg_singleton

    if _pg_singleton is not None:
        return _pg_singleton
    if _memory_singleton is not None and _pg_singleton is None:
        # If we already fell back, keep using memory for this process
        # unless postgres becomes available on a fresh process.
        pass

    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        conn_str = settings.database_url
        # from_conn_string returns a context manager in recent versions
        cm = PostgresSaver.from_conn_string(conn_str)
        saver = cm.__enter__()
        try:
            saver.setup()
        except Exception:
            pass
        _pg_singleton = saver
        return saver
    except Exception as exc:
        from langgraph.checkpoint.memory import MemorySaver

        if _memory_singleton is None:
            _memory_singleton = MemorySaver()
            print(f"[checkpointer] Using MemorySaver fallback ({exc})")
        return _memory_singleton
