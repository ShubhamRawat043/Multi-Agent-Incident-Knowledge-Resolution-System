"""Bounded GitHub read tools for change correlation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.tools import tool

from agent.config import settings


def _repo():
    if not settings.github_token:
        return None
    try:
        from github import Github

        return Github(settings.github_token).get_repo(settings.github_repo)
    except Exception:
        return None


@tool
def list_recent_commits(hours: int = 24, limit: int = 10) -> dict:
    """List recent commits on the default branch. Read-only."""
    repo = _repo()
    if repo is None:
        return {
            "error": "GitHub not configured or unreachable",
            "hint": "Set GITHUB_TOKEN and GITHUB_REPO, or rely on event.deploy_tag",
            "commits": [],
        }
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    commits = []
    try:
        for c in repo.get_commits(since=since)[:limit]:
            commits.append(
                {
                    "sha": c.sha[:8],
                    "message": (c.commit.message or "").split("\n")[0][:200],
                    "author": c.commit.author.name if c.commit.author else None,
                    "date": c.commit.author.date.isoformat() if c.commit.author else None,
                }
            )
    except Exception as exc:
        return {"error": str(exc), "commits": []}
    return {"repo": settings.github_repo, "commits": commits}


@tool
def list_recent_deploys(limit: int = 5) -> dict:
    """List recent GitHub deployments. Read-only."""
    repo = _repo()
    if repo is None:
        return {
            "error": "GitHub not configured",
            "deployments": [],
            "fallback": "Check incident event.deploy_tag / IMAGE_TAG on services",
        }
    out = []
    try:
        for d in repo.get_deployments()[:limit]:
            out.append(
                {
                    "id": d.id,
                    "sha": (d.sha or "")[:8],
                    "environment": d.environment,
                    "description": d.description,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
            )
    except Exception as exc:
        return {"error": str(exc), "deployments": []}
    return {"repo": settings.github_repo, "deployments": out}


@tool
def get_service_image_tag(service_url: str) -> dict:
    """Read /info from a demo service to get current image_tag. Read-only."""
    import httpx

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{service_url.rstrip('/')}/info")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": str(exc), "service_url": service_url}


github_tools = [list_recent_commits, list_recent_deploys, get_service_image_tag]
