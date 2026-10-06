"""Manual live SACHET checks. Use --mode direct or --mode full."""

import argparse
import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from jalwatch.agent.graph import create_agent_graph
from jalwatch.agent.state import create_initial_state
from jalwatch.llm.groq import create_groq_model
from jalwatch.protocol.jsonrpc import (
    JsonRpcToolRequest,
    ToolCallParams,
    tool_calls_to_requests,
)
from jalwatch.sources.sachet import SachetClient
from jalwatch.tools.registry import JsonRpcDispatcher, create_registry
from jalwatch.tools.sachet_tools import InMemoryEscalationStore, SachetTools


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("direct", "full"), required=True)
    parser.add_argument("--region", default="Bihar")
    args = parser.parse_args()
    client = SachetClient()
    try:
        dispatcher = JsonRpcDispatcher(
            create_registry(SachetTools(client), InMemoryEscalationStore())
        )
        if args.mode == "direct":
            request = JsonRpcToolRequest(
                id="manual-direct-1",
                params=ToolCallParams(
                    name="list_active_cwc_alerts", arguments={"region": args.region}
                ),
            )
            print("Selected tool: list_active_cwc_alerts")
            print("JSON-RPC request:", request.model_dump_json())
            response = await dispatcher.dispatch(request)
            print(
                "JSON-RPC response (bounded preview):",
                response.model_dump_json()[:2500],
            )
            return
        question = f"What active CWC alerts are currently available for {args.region}?"
        graph = create_agent_graph(create_groq_model(), dispatcher)
        result = await graph.ainvoke(
            create_initial_state(question, selected_region=args.region),
            config={"recursion_limit": 12},
        )
        for message in result["messages"]:
            if isinstance(message, AIMessage):
                for request in tool_calls_to_requests(message):
                    print("Selected tool:", request.params.name)
                    print("JSON-RPC request:", request.model_dump_json())
                if not message.tool_calls:
                    print("Final answer:", message.text)
            elif isinstance(message, ToolMessage):
                print("tool_call_id:", message.tool_call_id)
                print(
                    "JSON-RPC response (bounded preview):", str(message.content)[:2500]
                )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
