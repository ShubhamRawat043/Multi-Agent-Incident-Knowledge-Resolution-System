"""LangSmith / run config helpers — group traces per incident."""
from __future__ import annotations

from typing import Any


def incident_run_config(incident_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": incident_id},
        "metadata": {
            "incident_id": incident_id,
            "system": "incident-resolution",
        },
        "tags": ["incident-system", f"incident:{incident_id}"],
        "run_name": f"incident-{incident_id}",
    }
