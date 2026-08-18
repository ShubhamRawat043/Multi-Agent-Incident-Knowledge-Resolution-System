"""Redact PII / secrets before text reaches the LLM."""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([^\s'\"]+)"), r"\1=***REDACTED***"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "***EMAIL***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "***OPENAI_KEY***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "***GITHUB_TOKEN***"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "***IP***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.I), "Bearer ***REDACTED***"),
]


def redact(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_dict(data: dict) -> dict:
    def _walk(obj):
        if isinstance(obj, str):
            return redact(obj)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        return obj

    return _walk(data)
