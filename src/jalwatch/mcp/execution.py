"""LangGraph adapter from AI tool calls to MCP results and ToolMessages."""

import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from mcp.types import CallToolResult, TextContent, Tool

from jalwatch.agent.state import JalWatchState
from jalwatch.hooks.policy import ToolContext, post_tool_hook, pre_tool_hook
from jalwatch.telemetry.recorder import Telemetry


class MCPToolClient(Protocol):
    tools: tuple[Tool, ...]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> CallToolResult: ...


class ToolUpdate(TypedDict):
    messages: list[ToolMessage]


type MCPToolNode = Callable[[JalWatchState], Coroutine[Any, Any, ToolUpdate]]


def result_content(result: CallToolResult) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    return json.dumps(
        [block.text for block in result.content if isinstance(block, TextContent)],
        ensure_ascii=False,
    )


def create_mcp_tool_node(
    client: MCPToolClient,
    *,
    clock: Callable[[], datetime] | None = None,
    telemetry: Telemetry | None = None,
) -> MCPToolNode:
    current_time = clock or (lambda: datetime.now(UTC))
    recorder = telemetry or Telemetry()

    async def tool_node(state: JalWatchState) -> ToolUpdate:
        latest = state["messages"][-1]
        if not isinstance(latest, AIMessage):
            raise ValueError("Latest message must be an AIMessage")
        messages: list[ToolMessage] = []
        for call in latest.tool_calls:
            name, arguments, call_id = call["name"], call["args"], call["id"] or ""
            if name not in {tool.name for tool in client.tools}:
                recorder.event(
                    "pre_tool_hook",
                    tool=name,
                    allowed=False,
                    code="UNADVERTISED_TOOL",
                    mcp_skipped=True,
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "UNADVERTISED_TOOL",
                                    "message": "Tool was not discovered by MCP.",
                                },
                            }
                        ),
                        tool_call_id=call_id,
                        name=name,
                        status="error",
                    )
                )
                continue
            context = ToolContext(
                tool_name=name,
                arguments=arguments,
                tool_call_id=call_id,
                messages=[*state["messages"], *messages],
                now=current_time(),
            )
            decision = pre_tool_hook(context)
            recorder.event(
                "pre_tool_hook",
                tool=name,
                tool_call_id=call_id,
                allowed=decision.allowed,
                code=decision.error.code if decision.error else None,
                mcp_skipped=decision.error is not None,
            )
            if decision.error is not None:
                messages.append(
                    ToolMessage(
                        content=decision.error.payload(),
                        tool_call_id=call_id,
                        name=name,
                        status="error",
                    )
                )
                continue
            try:
                recorder.event(
                    "mcp_dispatch",
                    method="tools/call",
                    tool=name,
                    tool_call_id=call_id,
                    arguments=arguments,
                )
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    content, status = result_content(result), "error"
                else:
                    post = post_tool_hook(context, result.structured_content)
                    recorder.event(
                        "post_tool_hook",
                        tool=name,
                        accepted=post.allowed,
                        code=post.error.code if post.error else None,
                    )
                    if post.error is not None:
                        content, status = post.error.payload(), "error"
                    else:
                        content, status = result_content(result), "success"
            except Exception:
                content = json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "MCP_CALL_FAILED",
                            "message": "MCP tool call failed.",
                        },
                    }
                )
                status = "error"
            messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=call_id,
                    name=name,
                    status=status,
                )
            )
        return {"messages": messages}

    return tool_node
