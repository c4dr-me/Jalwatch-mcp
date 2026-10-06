"""Offline JSON-RPC dispatch, protected action, ToolMessage, and graph loop."""

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from jalwatch.agent.graph import create_agent_graph
from jalwatch.agent.state import create_initial_state
from jalwatch.agent.tool_contracts import CreateEscalation
from jalwatch.domain.models import ApprovalStatus, EscalationSeverity
from jalwatch.protocol.jsonrpc import (
    JsonRpcFailure,
    JsonRpcSuccess,
    JsonRpcToolRequest,
    ToolCallParams,
)
from jalwatch.sources.sachet import SachetClient
from jalwatch.tools.registry import JsonRpcDispatcher, create_registry
from jalwatch.tools.sachet_tools import InMemoryEscalationStore, SachetTools
from tests.test_sachet import NOW, make_client


def request(
    name: str, args: dict[str, object], call_id: str = "call-1"
) -> JsonRpcToolRequest:
    return JsonRpcToolRequest(
        id=call_id,
        params=ToolCallParams.model_validate({"name": name, "arguments": args}),
    )


def test_dispatch_success_unknown_invalid_method_and_protected_write() -> None:
    async def check() -> None:
        client, http, _ = make_client()
        store = InMemoryEscalationStore()
        dispatcher = JsonRpcDispatcher(
            create_registry(SachetTools(client, lambda: NOW), store)
        )
        try:
            success = await dispatcher.dispatch(request("list_affected_regions", {}))
            assert isinstance(success, JsonRpcSuccess)
            assert success.result == {"regions": ["Bihar", "Jharkhand"]}
            missing = await dispatcher.dispatch(request("no_such_tool", {}))
            assert isinstance(missing, JsonRpcFailure)
            assert missing.error.code == -32601
            bad = await dispatcher.dispatch(
                request("search_recent_cwc_alerts", {"hours": 0})
            )
            assert isinstance(bad, JsonRpcFailure)
            assert bad.error.code == -32602
            wrong_method = request("list_affected_regions", {}).model_copy(
                update={"method": "other"}
            )
            rejected = await dispatcher.dispatch(wrong_method)
            assert isinstance(rejected, JsonRpcFailure)
            assert rejected.error.code == -32601
            protected = await dispatcher.dispatch(
                request(
                    "create_escalation",
                    {
                        "alert_refs": ["1001"],
                        "region": "Jharkhand",
                        "rationale": "Official alert needs review",
                        "severity": "urgent",
                    },
                )
            )
            assert isinstance(protected, JsonRpcFailure)
            assert protected.error.data == {"code": "APPROVAL_REQUIRED"}
            assert store.records == {}
            invalid_approval = await dispatcher.dispatch(
                request(
                    "create_escalation",
                    {
                        "alert_refs": ["1001"],
                        "region": "Jharkhand",
                        "rationale": "Review",
                        "severity": "urgent",
                        "human_approved": True,
                    },
                )
            )
            assert isinstance(invalid_approval, JsonRpcFailure)
            assert invalid_approval.error.code == -32602
        finally:
            await http.aclose()

    asyncio.run(check())


def test_upstream_failure_is_structured_and_not_a_traceback() -> None:
    async def check() -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(503))
        )
        dispatcher = JsonRpcDispatcher(
            create_registry(
                SachetTools(SachetClient(http), lambda: NOW), InMemoryEscalationStore()
            )
        )
        try:
            response = await dispatcher.dispatch(request("list_active_cwc_alerts", {}))
            assert isinstance(response, JsonRpcFailure)
            assert response.error.data == {"code": "UPSTREAM_UNAVAILABLE"}
            assert "Traceback" not in response.model_dump_json()
        finally:
            await http.aclose()

    asyncio.run(check())


def test_direct_local_escalation_handler_creates_pending_record() -> None:
    store = InMemoryEscalationStore()
    args = CreateEscalation(
        alert_refs=("1001",),
        region="Jharkhand",
        rationale="Official alert needs operational review",
        severity=EscalationSeverity.URGENT,
    )
    created = store.create(args)
    assert created.approval_status is ApprovalStatus.PENDING
    assert created.alert_refs == ("1001",)
    assert store.records[created.proposal_id] == created


class FakeModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = responses
        self.inputs: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[type[BaseModel] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        assert len(tools) == 5
        assert tool_choice == "auto"
        return RunnableLambda(self.respond)

    async def respond(self, model_input: LanguageModelInput) -> BaseMessage:
        assert not isinstance(model_input, str)
        self.inputs.append(convert_to_messages(model_input))
        return self.responses.pop(0)


def run_graph(
    calls: list[dict[str, object]], final: str = "Grounded final"
) -> tuple[list[BaseMessage], FakeModel]:
    async def check() -> tuple[list[BaseMessage], FakeModel]:
        client, http, _ = make_client()
        model = FakeModel(
            [
                AIMessage(content="", tool_calls=calls),
                AIMessage(content=final),
            ]
        )
        dispatcher = JsonRpcDispatcher(
            create_registry(SachetTools(client, lambda: NOW), InMemoryEscalationStore())
        )
        try:
            result = await create_agent_graph(model, dispatcher).ainvoke(
                create_initial_state("Check CWC alerts"), config={"recursion_limit": 12}
            )
            return result["messages"], model
        finally:
            await http.aclose()

    return asyncio.run(check())


def test_graph_read_tool_then_toolmessage_then_final_answer() -> None:
    messages, model = run_graph(
        [
            {
                "name": "list_active_cwc_alerts",
                "args": {"region": "Bihar"},
                "id": "call-1",
            }
        ]
    )
    tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert tool.tool_call_id == "call-1"
    assert tool.status == "success"
    payload = json.loads(str(tool.content))
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "call-1"
    assert len(payload["result"]["alerts"]) == 1
    assert isinstance(model.inputs[1][-1], ToolMessage)
    assert messages[-1].content == "Grounded final"


def test_graph_multiple_calls_order_and_ids() -> None:
    messages, _ = run_graph(
        [
            {"name": "list_affected_regions", "args": {}, "id": "call-a"},
            {
                "name": "get_alert_details",
                "args": {"alert_ref": "1001"},
                "id": "call-b",
            },
        ]
    )
    tools = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tools] == ["call-a", "call-b"]
    assert all(message.status == "success" for message in tools)


def test_graph_one_bad_call_does_not_block_the_next_call() -> None:
    messages, model = run_graph(
        [
            {"name": "search_recent_cwc_alerts", "args": {"hours": 0}, "id": "bad"},
            {"name": "list_affected_regions", "args": {}, "id": "good"},
        ]
    )
    tools = [message for message in messages if isinstance(message, ToolMessage)]
    assert [(message.tool_call_id, message.status) for message in tools] == [
        ("bad", "error"),
        ("good", "success"),
    ]
    assert len(model.inputs[1]) >= 2


def test_graph_error_reaches_model_for_recovery() -> None:
    messages, model = run_graph(
        [
            {
                "name": "get_alert_details",
                "args": {"alert_ref": "unknown"},
                "id": "call-bad",
            }
        ],
        final="I could not find that alert reference.",
    )
    tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert tool.status == "error"
    assert json.loads(str(tool.content))["error"]["data"]["code"] == "ALERT_NOT_FOUND"
    assert isinstance(model.inputs[1][-1], ToolMessage)


def test_graph_protected_write_gets_approval_required_message() -> None:
    messages, _ = run_graph(
        [
            {
                "name": "create_escalation",
                "args": {
                    "alert_refs": ["1001"],
                    "region": "Jharkhand",
                    "rationale": "Review",
                    "severity": "urgent",
                },
                "id": "call-write",
            }
        ],
        final="This needs human approval.",
    )
    tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert tool.status == "error"
    assert json.loads(str(tool.content))["error"]["data"]["code"] == "APPROVAL_REQUIRED"


def test_graph_no_tool_answer_ends_immediately() -> None:
    async def check() -> None:
        client, http, requests = make_client()
        model = FakeModel([AIMessage(content="I can review official alerts.")])
        dispatcher = JsonRpcDispatcher(
            create_registry(SachetTools(client, lambda: NOW), InMemoryEscalationStore())
        )
        try:
            result = await create_agent_graph(model, dispatcher).ainvoke(
                create_initial_state("What can you do?"), config={"recursion_limit": 12}
            )
            assert result["messages"][-1].content == "I can review official alerts."
            assert not requests
            assert len(model.inputs) == 1
        finally:
            await http.aclose()

    asyncio.run(check())
