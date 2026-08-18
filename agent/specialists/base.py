"""Base helpers for capped ReAct specialist subgraphs."""
from __future__ import annotations

from typing import Annotated, Any, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from agent.config import settings
from agent.llm import get_fast_llm
from agent.state import Hypothesis


class SpecialistState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    hypothesis: dict
    incident_context: str


class HypothesisSchema(BaseModel):
    statement: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


def build_specialist_graph(
    name: str,
    system_prompt: str,
    tools: Sequence[BaseTool],
) -> Any:
    """Compile a ReAct subgraph that stops at SPECIALIST_MAX_STEPS."""

    llm = get_fast_llm().bind_tools(list(tools))
    structured = get_fast_llm().with_structured_output(HypothesisSchema)
    tool_node = ToolNode(list(tools))
    max_steps = settings.specialist_max_steps

    def agent_node(state: SpecialistState) -> dict:
        steps = int(state.get("steps") or 0)
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_prompt), *messages]
        # Force wrap-up when step budget is about to expire
        if steps >= max_steps - 1:
            messages = messages + [
                HumanMessage(
                    content=(
                        "Step budget nearly exhausted. Do NOT call more tools. "
                        "Produce your best hypothesis now from what you have."
                    )
                )
            ]
            hypo = structured.invoke(messages)
            return {
                "steps": steps + 1,
                "messages": [AIMessage(content=hypo.statement)],
                "hypothesis": Hypothesis(
                    statement=hypo.statement,
                    evidence=hypo.evidence,
                    confidence=hypo.confidence,
                    source_agent=name,
                ).model_dump(),
            }

        response = llm.invoke(messages)
        return {"messages": [response], "steps": steps + 1}

    def finalize_node(state: SpecialistState) -> dict:
        if state.get("hypothesis"):
            return {}
        messages = list(state.get("messages") or [])
        hypo = structured.invoke(
            [
                SystemMessage(content=system_prompt),
                *messages,
                HumanMessage(
                    content="Emit your final hypothesis as structured output now."
                ),
            ]
        )
        return {
            "hypothesis": Hypothesis(
                statement=hypo.statement,
                evidence=hypo.evidence,
                confidence=hypo.confidence,
                source_agent=name,
            ).model_dump()
        }

    def should_continue(state: SpecialistState) -> str:
        steps = int(state.get("steps") or 0)
        if steps >= max_steps:
            return "finalize"
        last = (state.get("messages") or [None])[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "finalize"

    g = StateGraph(SpecialistState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "finalize": "finalize"},
    )
    g.add_edge("tools", "agent")
    g.add_edge("finalize", END)
    return g.compile()
