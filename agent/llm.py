"""LLM helpers with model tiering."""
from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from agent.config import settings


@lru_cache(maxsize=2)
def get_fast_llm() -> ChatOpenAI:
    return ChatOpenAI(model=settings.fast_model, temperature=0)


@lru_cache(maxsize=2)
def get_strong_llm() -> ChatOpenAI:
    return ChatOpenAI(model=settings.strong_model, temperature=0)
