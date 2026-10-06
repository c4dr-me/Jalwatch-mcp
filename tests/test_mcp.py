"""Offline MCP v2 discovery, protected writes, and graph integration."""

import asyncio
from collections.abc import Sequence
from typing import Any, Literal

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableLambda
from mcp.types import ToolAnnotations

from jalwatch.agent.state import create_initial_state
from jalwatch.agent.tool_contracts import (
    GetAlertDetails,
    ListActiveCwcAlerts,
    SearchRecentCwcAlerts,
)
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.execution import create_mcp_tool_node
from jalwatch.mcp.graph import create_mcp_agent_graph
from jalwatch.mcp.server import create_server
from jalwatch.tools.sachet_tools import SachetTools


class FakeTools(SachetTools):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_active_cwc_alerts(
        self, args: ListActiveCwcAlerts
    ) -> dict[str, object]:
        self.calls.append("active")
        return {"alerts": [], "region": args.region}

    async def get_alert_details(self, args: GetAlertDetails) -> dict[str, object]:
        self.calls.append("details")
        return {"alert": {"alert_ref": args.alert_ref}}

    async def search_recent_cwc_alerts(
        self, args: SearchRecentCwcAlerts
    ) -> dict[str, object]:
        self.calls.append("recent")
        return {"alerts": [], "hours": args.hours}

    async def list_affected_regions(self) -> dict[str, object]:
        self.calls.append("regions")
        return {"regions": []}


class FakeModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = iter(responses)
        self.bound: list[str] = []
        self.seen: list[LanguageModelInput] = []

    def bind_tools(
        self,
        tools: Sequence[type[Any] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        self.bound = [
            tool["function"]["name"] for tool in tools if isinstance(tool, dict)
        ]

        async def respond(messages: LanguageModelInput) -> BaseMessage:
            self.seen.append(messages)
            return next(self.responses)

        return RunnableLambda(respond)


def test_server_discovery_and_protected_write() -> None:
    async def run() -> None:
        service = FakeTools()
        async with JalWatchMCPClient(create_server(service)) as client:
            assert {tool.name for tool in client.tools} == {
                "list_active_cwc_alerts",
                "get_alert_details",
                "search_recent_cwc_alerts",
                "list_affected_regions",
                "create_escalation",
            }
            schemas = {tool.name: tool.input_schema for tool in client.tools}
            assert "alert_ref" in schemas["get_alert_details"]["required"]
            assert "hours" in schemas["search_recent_cwc_alerts"]["required"]
            assert "alert_refs" in schemas["create_escalation"]["required"]
            assert "approved_by" not in schemas["create_escalation"]["properties"]
            annotations = {tool.name: tool.annotations for tool in client.tools}
            assert annotations["get_alert_details"] is not None
            assert annotations["create_escalation"] is not None
            assert annotations["get_alert_details"].read_only_hint is True
            assert annotations["create_escalation"].read_only_hint is False
            result = await client.call_tool(
                "list_active_cwc_alerts", {"region": "Bihar"}
            )
            assert result.structured_content == {"alerts": [], "region": "Bihar"}
            blocked = await client.call_tool(
                "create_escalation",
                {
                    "alert_refs": ["ref-1"],
                    "region": "Bihar",
                    "rationale": "Evidence",
                    "severity": "elevated",
                },
            )
            assert blocked.is_error
            assert "APPROVAL_REQUIRED" in str(blocked.content)
            assert service.calls == ["active"]
        assert client.tools == ()

    asyncio.run(run())


def test_dynamic_discovery_and_execution_correlation() -> None:
    async def run() -> None:
        service = FakeTools()
        server = create_server(service)
        async with JalWatchMCPClient(server) as client:
            model = FakeModel([])
            create_mcp_agent_graph(model, client)
            baseline = set(model.bound)
            assert len(baseline) == 5
            state = create_initial_state("Check")
            state["messages"].append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "list_active_cwc_alerts", "args": {}, "id": "call-a"},
                        {
                            "name": "get_alert_details",
                            "args": {"alert_ref": "ref-1"},
                            "id": "call-b",
                        },
                        {"name": "unknown", "args": {}, "id": "call-c"},
                    ],
                )
            )
            update = await create_mcp_tool_node(client)(state)
            assert [m.tool_call_id for m in update["messages"]] == [
                "call-a",
                "call-b",
                "call-c",
            ]
            assert [m.status for m in update["messages"]] == [
                "success",
                "error",
                "error",
            ]
            assert service.calls == ["active"]

        @server.tool(annotations=ToolAnnotations(read_only_hint=True))
        def tool_c() -> str:
            """A test-only capability discovered without changing reasoning code."""
            return "ok"

        async with JalWatchMCPClient(server) as client:
            model = FakeModel([])
            create_mcp_agent_graph(model, client)
            assert set(model.bound) == baseline | {"tool_c"}

    asyncio.run(run())


def test_graph_read_then_final_and_protected_error() -> None:
    async def run() -> None:
        service = FakeTools()
        async with JalWatchMCPClient(create_server(service)) as client:
            model = FakeModel(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "list_active_cwc_alerts",
                                "args": {"region": "Bihar"},
                                "id": "one",
                            }
                        ],
                    ),
                    AIMessage(content="No active matching alerts were returned."),
                ]
            )
            graph = create_mcp_agent_graph(model, client)
            result = await graph.ainvoke(create_initial_state("Check Bihar"))
            assert isinstance(result["messages"][-2], ToolMessage)
            assert result["messages"][-2].tool_call_id == "one"
            assert (
                result["messages"][-1].content
                == "No active matching alerts were returned."
            )

            model = FakeModel(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "create_escalation",
                                "args": {
                                    "alert_refs": ["ref-1"],
                                    "region": "Bihar",
                                    "rationale": "Evidence",
                                    "severity": "elevated",
                                },
                                "id": "write",
                            }
                        ],
                    ),
                    AIMessage(content="Human approval is required."),
                ]
            )
            result = await create_mcp_agent_graph(model, client).ainvoke(
                create_initial_state("Escalate")
            )
            assert result["messages"][-2].status == "error"
            assert service.calls == ["active"]

            model = FakeModel([AIMessage(content="I can explain alerts.")])
            result = await create_mcp_agent_graph(model, client).ainvoke(
                create_initial_state("Hello")
            )
            assert len(result["messages"]) == 2

    asyncio.run(run())
