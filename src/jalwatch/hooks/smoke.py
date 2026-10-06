"""Manual read-only hook smoke over a real stdio MCP subprocess."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import Any, TypedDict

from langchain_core.messages import AIMessage
from mcp.types import CallToolResult

from jalwatch.agent.state import create_initial_state
from jalwatch.hooks.policy import ToolContext, pre_tool_hook
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.execution import create_mcp_tool_node


class CountingClient(JalWatchMCPClient):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.call_count += 1
        return await super().call_tool(name, arguments)


class SmokeCall(TypedDict):
    name: str
    args: dict[str, Any]
    id: str


async def run(region: str) -> None:
    async with CountingClient() as client:
        state = create_initial_state(f"Check CWC alerts for {region}")
        calls: list[SmokeCall] = [
            {
                "name": "get_alert_details",
                "args": {"alert_ref": "unknown-ref"},
                "id": "probe-reject",
            },
            {
                "name": "list_active_cwc_alerts",
                "args": {"region": region},
                "id": "probe-read",
            },
        ]
        state["messages"].append(AIMessage(content="", tool_calls=calls))
        for call in calls:
            decision = pre_tool_hook(
                ToolContext(
                    call["name"],
                    call["args"],
                    call["id"],
                    state["messages"],
                    datetime.now(UTC),
                )
            )
            error = decision.error
            print(
                "Request:",
                call["name"],
                "pre-hook:",
                "ALLOW" if error is None else error.code,
            )
        update = await create_mcp_tool_node(client)(state)
        print("MCP calls made:", client.call_count)
        for message in update["messages"]:
            outcome = "ACCEPT" if message.status == "success" else "ERROR"
            if message.status == "error" and isinstance(message.content, str):
                try:
                    payload = json.loads(message.content)
                    if payload.get("error", {}).get("status") == 400:
                        outcome = "SKIPPED (pre-hook rejected)"
                    elif payload.get("error", {}).get("status") == 502:
                        outcome = "REJECT (502-style post-hook)"
                except (ValueError, AttributeError):
                    pass
            print(
                "ToolMessage:",
                message.tool_call_id,
                message.status,
                "post-hook:",
                outcome,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="Bihar")
    args = parser.parse_args()
    asyncio.run(run(args.region))


if __name__ == "__main__":
    main()
