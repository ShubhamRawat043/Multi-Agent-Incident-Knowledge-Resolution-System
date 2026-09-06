"""LLM helpers with model tiering."""
from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from agent.config import settings

# gpt-5-family models default to a non-"none" reasoning effort, and OpenAI's
# /v1/chat/completions endpoint (what langchain-openai calls by default)
# rejects function tools whenever reasoning is enabled:
#   "Function tools with reasoning_effort are not supported for <model> in
#   /v1/chat/completions. ... or set reasoning_effort to 'none'."
# The four specialist agents (agent/specialists/*.py) all bind tools via
# build_specialist_graph(), so without this every specialist call fails and
# investigation silently falls back to "root cause unknown" on every incident.
_REASONING_EFFORT_NONE = "none"


@lru_cache(maxsize=2)
def get_fast_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.fast_model, temperature=0, reasoning_effort=_REASONING_EFFORT_NONE
    )


@lru_cache(maxsize=2)
def get_strong_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.strong_model, temperature=0, reasoning_effort=_REASONING_EFFORT_NONE
    )
