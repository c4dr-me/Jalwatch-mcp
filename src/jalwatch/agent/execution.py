"""Translate AI tool calls, dispatch locally, and append correlated ToolMessages."""

from collections.abc import Callable, Coroutine
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from jalwatch.agent.state import JalWatchState
from jalwatch.protocol.jsonrpc import (
    JsonRpcError,
    JsonRpcFailure,
    tool_call_to_request,
)
from jalwatch.tools.registry import JsonRpcDispatcher


class ToolUpdate(TypedDict):
    messages: list[ToolMessage]


type ToolNode = Callable[[JalWatchState], Coroutine[Any, Any, ToolUpdate]]


def create_tool_node(dispatcher: JsonRpcDispatcher) -> ToolNode:
    async def tool_node(state: JalWatchState) -> ToolUpdate:
        latest = state["messages"][-1]
        if not isinstance(latest, AIMessage):
            raise ValueError("Tool node requires a latest AIMessage")
        results: list[ToolMessage] = []
        for index, call in enumerate(latest.tool_calls):
            call_id = call.get("id") or f"invalid-call-{index}"
            name = call["name"]
            try:
                request = tool_call_to_request(call)
                response = await dispatcher.dispatch(request)
            except (ValidationError, ValueError):
                response = JsonRpcFailure(
                    id=call_id,
                    error=JsonRpcError(
                        code=-32600,
                        message="Malformed tool request",
                        data={"code": "INVALID_REQUEST"},
                    ),
                )
            results.append(
                ToolMessage(
                    content=response.model_dump_json(),
                    tool_call_id=call_id,
                    name=name,
                    status="error"
                    if isinstance(response, JsonRpcFailure)
                    else "success",
                )
            )
        return {"messages": results}

    return tool_node
