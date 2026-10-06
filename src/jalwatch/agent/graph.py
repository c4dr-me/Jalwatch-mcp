"""The smallest Task 3 reasoning/tool loop; no hooks, HITL, or checkpointing."""

from typing import Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from jalwatch.agent.execution import create_tool_node
from jalwatch.agent.reasoning import ToolBindingModel, create_reasoning_node
from jalwatch.agent.state import JalWatchState
from jalwatch.tools.registry import JsonRpcDispatcher


def route_after_reasoning(state: JalWatchState) -> Literal["tools", "__end__"]:
    latest = state["messages"][-1]
    return "tools" if isinstance(latest, AIMessage) and latest.tool_calls else "__end__"


def create_agent_graph(
    model: ToolBindingModel, dispatcher: JsonRpcDispatcher
) -> CompiledStateGraph[JalWatchState, None, JalWatchState, JalWatchState]:
    builder = StateGraph(JalWatchState)
    builder.add_node("reasoning", RunnableLambda(create_reasoning_node(model)))
    builder.add_node("tools", RunnableLambda(create_tool_node(dispatcher)))
    builder.add_edge(START, "reasoning")
    builder.add_conditional_edges("reasoning", route_after_reasoning)
    builder.add_edge("tools", "reasoning")
    return builder.compile()
