"""Deterministic Task 5 policy and self-correction tests; no external calls."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableLambda
from mcp.types import CallToolResult, TextContent, Tool

from jalwatch.agent.state import create_initial_state
from jalwatch.agent.tool_contracts import ListActiveCwcAlerts
from jalwatch.hooks.policy import (
    ToolContext,
    observed_alerts,
    post_tool_hook,
    pre_tool_hook,
)
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.execution import create_mcp_tool_node
from jalwatch.mcp.graph import create_mcp_agent_graph
from jalwatch.mcp.server import create_server
from jalwatch.tools.sachet_tools import SachetTools

NOW = datetime(2026, 10, 6, 10, tzinfo=UTC)


def summary(ref: str = "real-1", region: str = "Bihar") -> dict[str, object]:
    return {
        "alert_ref": ref,
        "cap_identifier": "CAP-123",
        "source_url": "https://sachet.ndma.gov.in/example",
        "cwc_provenance": ["rss_author:CWC"],
        "sender": "state-agency",
        "sent": (NOW - timedelta(hours=2)).isoformat(),
        "published_at": (NOW - timedelta(hours=2)).isoformat(),
        "status": "Actual",
        "msg_type": "Alert",
        "source_severities": ["Moderate"],
        "normalized_regions": [region],
        "effective": [(NOW - timedelta(hours=2)).isoformat()],
        "expires": [(NOW + timedelta(hours=2)).isoformat()],
    }


def detail(ref: str = "real-1") -> dict[str, object]:
    record = summary(ref)
    record["info"] = [
        {
            "source_severity": "Moderate",
            "raw_area_descriptions": ["Bihar: Patna District"],
            "normalized_regions": ["Bihar"],
            "effective": (NOW - timedelta(hours=2)).isoformat(),
            "onset": None,
            "expires": (NOW + timedelta(hours=2)).isoformat(),
        }
    ]
    return record


def result(body: dict[str, object], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structured_content=body if not error else None,
        is_error=error,
    )


def observed_message(ref: str = "real-1") -> ToolMessage:
    return ToolMessage(
        name="list_active_cwc_alerts",
        status="success",
        tool_call_id="discovery",
        content=json.dumps({"alerts": [summary(ref)]}),
    )


def context(
    name: str, args: dict[str, object], messages: Sequence[BaseMessage] = ()
) -> ToolContext:
    return ToolContext(name, args, "call-1", messages, NOW)


def test_pre_hook_grounding_and_region_policy() -> None:
    history = [observed_message()]
    assert pre_tool_hook(context("list_active_cwc_alerts", {})).allowed
    unknown = pre_tool_hook(context("get_alert_details", {"alert_ref": "fake"}))
    assert unknown.error is not None
    assert json.loads(unknown.error.payload())["error"]["status"] == 400
    assert unknown.error.code == "UNKNOWN_ALERT_REF"
    assert pre_tool_hook(
        context("get_alert_details", {"alert_ref": "real-1"}, history)
    ).allowed
    bad = pre_tool_hook(
        context(
            "create_escalation",
            {
                "alert_refs": ["real-1", "fake"],
                "region": "Bihar",
            },
            history,
        )
    )
    assert bad.error is not None
    assert bad.error.code == "UNGROUNDED_ESCALATION_EVIDENCE"
    assert bad.error.details["unknown_alert_refs"] == ["fake"]
    mismatch = pre_tool_hook(
        context(
            "create_escalation",
            {
                "alert_refs": ["real-1"],
                "region": "Assam",
            },
            history,
        )
    )
    assert mismatch.error is not None
    assert mismatch.error.code == "REGION_EVIDENCE_MISMATCH"
    assert pre_tool_hook(
        context(
            "create_escalation",
            {
                "alert_refs": ["real-1"],
                "region": "Bihar",
            },
            history,
        )
    ).allowed
    assert (
        observed_alerts(
            [
                ToolMessage(
                    name="list_active_cwc_alerts",
                    status="error",
                    tool_call_id="bad",
                    content=json.dumps({"alerts": [summary("fake")]}),
                )
            ]
        )
        == {}
    )


def test_post_hook_validates_successful_results_only() -> None:
    active = context("list_active_cwc_alerts", {})
    assert post_tool_hook(active, {"alerts": [summary()]}).allowed
    malformed = post_tool_hook(active, {"alerts": [{"alert_ref": "real-1"}]})
    assert malformed.error is not None
    assert malformed.error.status == 502
    assert malformed.error.code == "INVALID_TOOL_RESULT"
    expired = summary()
    expired["expires"] = [(NOW - timedelta(minutes=1)).isoformat()]
    assert not post_tool_hook(active, {"alerts": [expired]}).allowed
    missing_expiry = summary()
    missing_expiry["expires"] = [None]
    assert not post_tool_hook(active, {"alerts": [missing_expiry]}).allowed

    details = context("get_alert_details", {"alert_ref": "real-1"})
    assert post_tool_hook(details, {"alert": detail()}).allowed
    assert not post_tool_hook(details, {"alert": detail("other")}).allowed
    no_provenance = detail()
    no_provenance["cwc_provenance"] = []
    assert not post_tool_hook(details, {"alert": no_provenance}).allowed

    recent = context("search_recent_cwc_alerts", {"hours": 3})
    assert post_tool_hook(recent, {"alerts": [summary()]}).allowed
    old = summary()
    old["published_at"] = (NOW - timedelta(hours=4)).isoformat()
    assert not post_tool_hook(recent, {"alerts": [old]}).allowed

    regions = context("list_affected_regions", {})
    assert post_tool_hook(regions, {"regions": []}).allowed
    assert post_tool_hook(regions, {"regions": ["Assam", "Bihar"]}).allowed
    assert not post_tool_hook(regions, {"regions": ["Bihar", "Assam"]}).allowed
    assert not post_tool_hook(regions, {"regions": ["Assam", "Assam"]}).allowed


class CountingMCP:
    def __init__(self, outputs: dict[str, CallToolResult]) -> None:
        self.tools = tuple(
            Tool(name=name, input_schema={"type": "object"}) for name in outputs
        )
        self.outputs = outputs
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.calls.append(name)
        return self.outputs[name]


def test_pre_rejection_skips_mcp_and_other_calls_continue() -> None:
    async def run() -> None:
        client = CountingMCP(
            {
                "get_alert_details": result({"alert": detail()}),
                "list_active_cwc_alerts": result({"alerts": [summary()]}),
            }
        )
        state = create_initial_state("Check")
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_alert_details",
                        "args": {"alert_ref": "fake"},
                        "id": "bad",
                    },
                    {"name": "list_active_cwc_alerts", "args": {}, "id": "good"},
                ],
            )
        )
        update = await create_mcp_tool_node(client, clock=lambda: NOW)(state)
        assert client.calls == ["list_active_cwc_alerts"]
        assert [m.tool_call_id for m in update["messages"]] == ["bad", "good"]
        assert [m.status for m in update["messages"]] == ["error", "success"]
        content = update["messages"][0].content
        assert isinstance(content, str)
        assert json.loads(content)["error"]["status"] == 400

        state["messages"].insert(-1, observed_message())
        latest = state["messages"][-1]
        assert isinstance(latest, AIMessage)
        latest.tool_calls[0]["args"]["alert_ref"] = "real-1"
        update = await create_mcp_tool_node(client, clock=lambda: NOW)(state)
        assert client.calls.count("get_alert_details") == 1
        assert update["messages"][0].status == "success"

    asyncio.run(run())


def test_mcp_error_bypasses_post_and_post_rejection_hides_bad_result() -> None:
    async def run() -> None:
        client = CountingMCP(
            {
                "list_active_cwc_alerts": result({"alerts": [summary()]}, error=True),
            }
        )
        state = create_initial_state("Check")
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "list_active_cwc_alerts", "args": {}, "id": "one"},
                ],
            )
        )
        node = create_mcp_tool_node(client, clock=lambda: NOW)
        error = (await node(state))["messages"][0]
        assert error.status == "error"
        assert "INVALID_TOOL_RESULT" not in str(error.content)
        client.outputs["list_active_cwc_alerts"] = result(
            {"alerts": [{"alert_ref": "bad"}]}
        )
        rejected = (await node(state))["messages"][0]
        assert isinstance(rejected.content, str)
        payload = json.loads(rejected.content)
        assert rejected.status == "error"
        assert payload["error"]["status"] == 502
        assert "alerts" not in payload

    asyncio.run(run())


class FakeService(SachetTools):
    def __init__(self) -> None:
        self.calls = 0

    async def list_active_cwc_alerts(
        self, args: ListActiveCwcAlerts
    ) -> dict[str, object]:
        self.calls += 1
        return {"alerts": [summary()]}


class CountingClient(JalWatchMCPClient):
    def __init__(self, service: FakeService) -> None:
        super().__init__(create_server(service))
        self.called: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.called.append(name)
        return await super().call_tool(name, arguments)


class ScriptedModel:
    def __init__(self) -> None:
        self.step = 0
        self.seen: list[LanguageModelInput] = []

    def bind_tools(
        self,
        tools: Sequence[type[Any] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        async def answer(messages: LanguageModelInput) -> BaseMessage:
            self.seen.append(messages)
            self.step += 1
            if self.step == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "create_escalation",
                            "args": {
                                "alert_refs": ["fake-123"],
                                "region": "Bihar",
                                "rationale": "Check",
                                "severity": "elevated",
                            },
                            "id": "first",
                        }
                    ],
                )
            if self.step == 2:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "list_active_cwc_alerts",
                            "args": {"region": "Bihar"},
                            "id": "second",
                        }
                    ],
                )
            if self.step == 3:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "create_escalation",
                            "args": {
                                "alert_refs": ["real-1"],
                                "region": "Bihar",
                                "rationale": "Official alert",
                                "severity": "elevated",
                            },
                            "id": "third",
                        }
                    ],
                )
            return AIMessage(content="Human approval is required before escalation.")

        return RunnableLambda(answer)


def test_graph_self_correction_grounding_then_server_approval_block() -> None:
    async def run() -> None:
        service = FakeService()
        async with CountingClient(service) as client:
            model = ScriptedModel()
            graph = create_mcp_agent_graph(model, client, clock=lambda: NOW)
            final = await graph.ainvoke(create_initial_state("Escalate fake-123"))
            tool_messages = [m for m in final["messages"] if isinstance(m, ToolMessage)]
            assert [m.tool_call_id for m in tool_messages] == [
                "first",
                "second",
                "third",
            ]
            assert [m.status for m in tool_messages] == ["error", "success", "error"]
            first_content = tool_messages[0].content
            assert isinstance(first_content, str)
            assert json.loads(first_content)["error"]["status"] == 400
            assert "APPROVAL_REQUIRED" in str(tool_messages[2].content)
            assert client.called == ["list_active_cwc_alerts", "create_escalation"]
            assert service.calls == 1
            assert (
                final["messages"][-1].content
                == "Human approval is required before escalation."
            )
            assert len(model.seen) == 4
            second_reasoning_input = convert_to_messages(model.seen[1])
            assert isinstance(second_reasoning_input[-1], ToolMessage)
            assert second_reasoning_input[-1].status == "error"

    asyncio.run(run())
