"""Manual real-stdio MCP smoke; optional --full also uses Groq."""

import argparse
import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from jalwatch.agent.state import create_initial_state
from jalwatch.llm.groq import create_groq_model
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.graph import create_mcp_agent_graph


async def _run(full: bool, region: str) -> None:
    async with JalWatchMCPClient() as client:
        print("Discovered:", [tool.name for tool in client.tools])
        if not full:
            call_result = await client.call_tool(
                "list_active_cwc_alerts", {"region": region}
            )
            data = call_result.structured_content
            if isinstance(data, dict):
                alerts = data.get("alerts", [])
                print(
                    "MCP tools/call:",
                    json.dumps(
                        {
                            "error": call_result.is_error,
                            "alert_count": len(alerts)
                            if isinstance(alerts, list)
                            else None,
                        }
                    ),
                )
            else:
                print(
                    "MCP tools/call:",
                    json.dumps(
                        {
                            "error": call_result.is_error,
                            "content": [
                                getattr(block, "text", "")[:300]
                                for block in call_result.content
                            ],
                        }
                    ),
                )
            return

        graph = create_mcp_agent_graph(create_groq_model(), client)
        state = create_initial_state(
            f"What active CWC flood alerts are currently available for {region}?",
            selected_region=region,
        )
        result: dict[str, Any] = await graph.ainvoke(
            state, config={"recursion_limit": 12}
        )
        for message in result["messages"]:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    print("AI tool call:", call["id"], call["name"], call["args"])
            elif isinstance(message, ToolMessage):
                print(
                    "MCP result:",
                    message.tool_call_id,
                    message.status,
                    str(message.content)[:350],
                )
        latest = result["messages"][-1]
        print("Final answer:", latest.content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--region", default="Bihar")
    args = parser.parse_args()
    asyncio.run(_run(args.full, args.region))


if __name__ == "__main__":
    main()
