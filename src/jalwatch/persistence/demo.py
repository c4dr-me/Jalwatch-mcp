"""Two-process offline HITL restart demonstration using a real stdio MCP server."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from secrets import token_bytes
from typing import Any, Literal

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from mcp import StdioServerParameters
from pydantic import BaseModel

from jalwatch.agent.state import create_initial_state
from jalwatch.hitl.grant import APPROVAL_SECRET_ENV
from jalwatch.hitl.graph import ApprovalDecision, create_hitl_graph
from jalwatch.hitl.runtime import resume_approval
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.persistence.runtime import open_checkpoint_saver
from jalwatch.telemetry.langfuse import configured_telemetry, observed_run


class RestartModel:
    def __init__(self, region: str) -> None:
        self.region = region

    def bind_tools(
        self,
        tools: Sequence[type[BaseModel] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        async def respond(value: LanguageModelInput) -> BaseMessage:
            messages = convert_to_messages(value)
            results = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if any(result.name == "create_escalation" for result in results):
                outcome = results[-1]
                return AIMessage(
                    content=(
                        "Offline restart demo: internal escalation created."
                        if outcome.status == "success"
                        else "Offline restart demo: escalation was not executed."
                    )
                )
            if any(result.name == "list_active_cwc_alerts" for result in results):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "create_escalation",
                            "args": {
                                "alert_refs": ["OFFLINE-ALERT-1"],
                                "region": self.region,
                                "rationale": "Review synthetic training alert.",
                                "severity": "elevated",
                            },
                            "id": "restart-write",
                        }
                    ],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_active_cwc_alerts",
                        "args": {"region": self.region},
                        "id": "restart-read",
                    }
                ],
            )

        return RunnableLambda(respond)


async def run(command: str, thread_id: str, region: str, decision: str | None) -> None:
    telemetry = configured_telemetry()
    secret = token_bytes(32)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jalwatch.persistence.demo_server"],
        env={
            APPROVAL_SECRET_ENV: secret.hex(),
            "JALWATCH_DEMO_REGION": region,
        },
    )
    async with (
        open_checkpoint_saver() as saver,
        JalWatchMCPClient(
            server, approval_secret=secret, telemetry=telemetry
        ) as client,
    ):
        graph = create_hitl_graph(
            RestartModel(region), client, checkpointer=saver, telemetry=telemetry
        )
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        if command == "start":
            existing = await graph.aget_state(config)
            if existing.values:
                raise ValueError("Thread already exists; choose a fresh thread_id")
            with observed_run(
                telemetry,
                thread_id=thread_id,
                operation="invoke",
                resumed_from_checkpoint=False,
            ):
                result = await graph.ainvoke(
                    create_initial_state(
                        f"Review synthetic CWC alert in {region} "
                        "for internal escalation.",
                        selected_region=region,
                    ),
                    config=config,
                )
            if not result.get("__interrupt__"):
                raise RuntimeError("Demo did not reach an approval interrupt")
            snapshot = await graph.aget_state(config)
            print("Paused approval:", json.dumps(result["__interrupt__"][0].value))
            print("Checkpointed messages:", len(snapshot.values["messages"]))
            print(
                "Close this process; resume in a NEW process with the same thread_id."
            )
        else:
            snapshot = await graph.aget_state(config)
            if not snapshot.values or not snapshot.values.get("pending_approval"):
                raise ValueError("No pending approval for this thread_id")
            pending = snapshot.values["pending_approval"]
            print("Recovered approval_id:", pending.approval_id)
            print("Recovered messages:", len(snapshot.values["messages"]))
            with observed_run(
                telemetry,
                thread_id=thread_id,
                operation="resume",
                resumed_from_checkpoint=True,
            ):
                result = await resume_approval(
                    graph,
                    thread_id=thread_id,
                    decision=ApprovalDecision(
                        decision="approve" if decision == "approve" else "reject"
                    ),
                    telemetry=telemetry,
                )
            print("Final answer:", result["messages"][-1].content)
    telemetry.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "resume"))
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--region", default="Bihar")
    parser.add_argument("--decision", choices=("approve", "reject"))
    args = parser.parse_args()
    if args.command == "resume" and args.decision is None:
        parser.error("resume requires --decision approve|reject")
    asyncio.run(run(args.command, args.thread_id, args.region, args.decision))


if __name__ == "__main__":
    main()
