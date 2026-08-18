"""Bounded Sentry read tools."""
from __future__ import annotations

from typing import Any, Optional

import httpx
from langchain_core.tools import tool

from agent.config import settings
from tools.redaction import redact


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.sentry_auth_token}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    return "https://sentry.io/api/0"


@tool
def query_sentry_issues(query: str = "is:unresolved", limit: int = 5) -> dict:
    """Fetch recent Sentry issues for the configured project. Read-only."""
    if not settings.sentry_auth_token or not settings.sentry_org or not settings.sentry_project:
        return {
            "error": "Sentry not configured (need SENTRY_AUTH_TOKEN/ORG/PROJECT)",
            "fallback": "Use event.stacktrace / title from the incident payload",
            "query": query,
        }
    url = (
        f"{_base()}/projects/{settings.sentry_org}/{settings.sentry_project}/issues/"
        f"?query={httpx.QueryParams({'query': query})['query']}&limit={limit}"
    )
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                f"{_base()}/projects/{settings.sentry_org}/{settings.sentry_project}/issues/",
                headers=_headers(),
                params={"query": query, "limit": limit},
            )
            resp.raise_for_status()
            issues = resp.json()
    except Exception as exc:
        return {"error": str(exc), "query": query}

    simplified = []
    for issue in issues[:limit]:
        simplified.append(
            {
                "id": issue.get("id"),
                "title": redact(issue.get("title")),
                "culprit": redact(issue.get("culprit")),
                "count": issue.get("count"),
                "firstSeen": issue.get("firstSeen"),
                "lastSeen": issue.get("lastSeen"),
                "permalink": issue.get("permalink"),
            }
        )
    return {"issues": simplified, "count": len(simplified)}


@tool
def get_sentry_event(issue_id: str) -> dict:
    """Fetch latest event / stacktrace for a Sentry issue id. Read-only."""
    if not settings.sentry_auth_token:
        return {"error": "Sentry not configured", "issue_id": issue_id}
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                f"{_base()}/issues/{issue_id}/events/latest/",
                headers=_headers(),
            )
            resp.raise_for_status()
            event = resp.json()
    except Exception as exc:
        return {"error": str(exc), "issue_id": issue_id}

    entries = event.get("entries") or []
    stack = ""
    for entry in entries:
        if entry.get("type") == "exception":
            stack = redact(str(entry.get("data")))
            break
    return {
        "issue_id": issue_id,
        "event_id": event.get("eventID"),
        "message": redact(event.get("message") or event.get("title")),
        "stacktrace": stack[:4000],
        "tags": event.get("tags"),
    }


sentry_tools = [query_sentry_issues, get_sentry_event]
