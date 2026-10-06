"""Normal Task 4 graph, bound to tools discovered from a live MCP session."""

from collections.abc import Callable
from datetime import datetime

from langchain_core.runnables import RunnableLambda
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from jalwatch.agent.graph import route_after_reasoning
from jalwatch.agent.reasoning import ToolBindingModel, create_reasoning_node
from jalwatch.agent.state import JalWatchState
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.execution import create_mcp_tool_node


def create_mcp_agent_graph(
    model: ToolBindingModel,
    client: JalWatchMCPClient,
    *,
    clock: Callable[[], datetime] | None = None,
) -> CompiledStateGraph[JalWatchState, None, JalWatchState, JalWatchState]:
    """Build only after client startup/tools/list; reuse the connection for calls."""
    builder = StateGraph(JalWatchState)
    builder.add_node(
        "reasoning",
        RunnableLambda(create_reasoning_node(model, client.schemas_for_model())),
    )
    builder.add_node("tools", RunnableLambda(create_mcp_tool_node(client, clock=clock)))
    builder.add_edge(START, "reasoning")
    builder.add_conditional_edges("reasoning", route_after_reasoning)
    builder.add_edge("tools", "reasoning")
    return builder.compile()
