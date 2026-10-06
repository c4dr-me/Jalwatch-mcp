"""Terminal pause/resume demo: deterministic offline default, optional live mode."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from secrets import token_bytes
from typing import Any, Literal
from uuid import uuid4

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from pydantic import BaseModel

from jalwatch.agent.reasoning import ToolBindingModel
from jalwatch.agent.state import create_initial_state
from jalwatch.agent.tool_contracts import ListActiveCwcAlerts
from jalwatch.hitl.graph import ApprovalDecision, create_hitl_graph
from jalwatch.hitl.runtime import resume_approval
from jalwatch.llm.groq import create_groq_model
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.server import create_server
from jalwatch.tools.sachet_tools import InMemoryEscalationStore, SachetTools


class OfflineService(SachetTools):
    def __init__(self, region: str) -> None:
        self.region = region

    async def list_active_cwc_alerts(
        self, args: ListActiveCwcAlerts
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "alerts": [
                {
                    "alert_ref": "OFFLINE-ALERT-1",
                    "cap_identifier": "OFFLINE-CAP-1",
                    "sender": "offline-fixture",
                    "sent": (now - timedelta(minutes=10)).isoformat(),
                    "published_at": (now - timedelta(minutes=10)).isoformat(),
                    "status": "Actual",
                    "msg_type": "Alert",
                    "source_severities": ["Moderate"],
                    "normalized_regions": [self.region],
                    "effective": [(now - timedelta(minutes=10)).isoformat()],
                    "expires": [(now + timedelta(hours=1)).isoformat()],
                    "source_url": "https://sachet.ndma.gov.in/offline-fixture",
                    "cwc_provenance": ["offline_fixture_only"],
                }
            ]
        }


class OfflineModel:
    """A scripted operator demo; its alert is explicitly synthetic."""

    def __init__(self, region: str) -> None:
        self.region = region
        self.step = 0

    def bind_tools(
        self,
        tools: Sequence[type[BaseModel] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        async def respond(value: LanguageModelInput) -> BaseMessage:
            self.step += 1
            if self.step == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "list_active_cwc_alerts",
                            "args": {"region": self.region},
                            "id": "offline-read",
                        }
                    ],
                )
            if self.step == 2:
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
                            "id": "offline-write",
                        }
                    ],
                )
            messages = convert_to_messages(value)
            latest = messages[-1]
            if isinstance(latest, ToolMessage) and latest.status == "success":
                return AIMessage(
                    content="Offline demo: internal escalation was created."
                )
            return AIMessage(content="Offline demo: escalation was not executed.")

        return RunnableLambda(respond)


async def run(*, live: bool, region: str, thread_id: str) -> None:
    model: ToolBindingModel
    if live:
        client = JalWatchMCPClient()
        model = create_groq_model()
    else:
        secret = token_bytes(32)
        service = OfflineService(region)
        server = create_server(
            service, store=InMemoryEscalationStore(), approval_secret=secret
        )
        client = JalWatchMCPClient(server, approval_secret=secret)
        model = OfflineModel(region)
        print("OFFLINE FIXTURE: no live government data or Groq call.")

    async with client:
        graph = create_hitl_graph(model, client)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        question = (
            f"Review current CWC alerts in {region}. If evidence supports it, "
            "request an internal operational escalation."
        )
        result = await graph.ainvoke(
            create_initial_state(question, selected_region=region), config=config
        )
        interrupts = result.get("__interrupt__", ())
        if interrupts:
            request = interrupts[0].value
            print("Approval request:")
            print(json.dumps(request, indent=2, ensure_ascii=False))
            while True:
                answer = input("Approve or Reject? [a/r]: ").strip().casefold()
                if answer in ("a", "approve", "r", "reject"):
                    break
                print("Enter a or r.")
            decision = ApprovalDecision(
                decision="approve" if answer in ("a", "approve") else "reject"
            )
            result = await resume_approval(
                graph, thread_id=thread_id, decision=decision
            )
        latest = result["messages"][-1]
        print("Thread:", thread_id)
        print("Final answer:", latest.content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--region", default="Bihar")
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()
    asyncio.run(
        run(
            live=args.live,
            region=args.region,
            thread_id=args.thread_id or f"jalwatch-demo-{uuid4()}",
        )
    )


if __name__ == "__main__":
    main()
