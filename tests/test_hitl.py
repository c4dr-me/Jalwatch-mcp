"""Offline Task 6 approval, MCP grant, and graph pause/resume tests."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.types import Command
from mcp.types import CallToolResult, RequestParamsMeta
from pydantic import ValidationError

from jalwatch.agent.state import JalWatchState, create_initial_state
from jalwatch.agent.tool_contracts import ListActiveCwcAlerts
from jalwatch.domain.models import ApprovalStatus
from jalwatch.hitl.grant import APPROVAL_META_KEY, issue_grant
from jalwatch.hitl.graph import ApprovalDecision, create_hitl_graph
from jalwatch.hitl.runtime import resume_approval
from jalwatch.hooks.policy import ToolContext, post_tool_hook
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.server import create_server
from jalwatch.tools.sachet_tools import InMemoryEscalationStore, SachetTools

SECRET = b"test-only-32-byte-approval-secret!"


def escalation_args(
    *,
    region: str = "Bihar",
    refs: list[str] | None = None,
    severity: str = "elevated",
) -> dict[str, Any]:
    return {
        "alert_refs": refs if refs is not None else ["real-1"],
        "region": region,
        "rationale": "Official CWC flood alert requires review.",
        "severity": severity,
    }


def summary() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "alert_ref": "real-1",
        "cap_identifier": "CAP-1",
        "sender": "state-agency",
        "status": "Actual",
        "msg_type": "Alert",
        "sent": (now - timedelta(minutes=10)).isoformat(),
        "published_at": (now - timedelta(minutes=10)).isoformat(),
        "effective": [(now - timedelta(minutes=10)).isoformat()],
        "expires": [(now + timedelta(hours=2)).isoformat()],
        "source_url": "https://sachet.ndma.gov.in/example",
        "cwc_provenance": ["rss_author:CWC"],
        "source_severities": ["Moderate"],
        "normalized_regions": ["Bihar"],
    }


def grounded_state() -> JalWatchState:
    state = create_initial_state("Review official CWC alert")
    state["messages"].extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_active_cwc_alerts",
                        "args": {"region": "Bihar"},
                        "id": "discovery",
                    }
                ],
            ),
            ToolMessage(
                name="list_active_cwc_alerts",
                tool_call_id="discovery",
                status="success",
                content=json.dumps({"alerts": [summary()]}),
            ),
            HumanMessage(content="Escalate the verified alert"),
        ]
    )
    return state


class FakeService(SachetTools):
    def __init__(self) -> None:
        self.reads = 0

    async def list_active_cwc_alerts(
        self, args: ListActiveCwcAlerts
    ) -> dict[str, object]:
        self.reads += 1
        return {"alerts": [summary()]}


class CountingClient(JalWatchMCPClient):
    def __init__(self, service: FakeService, store: InMemoryEscalationStore) -> None:
        super().__init__(
            create_server(service, store=store, approval_secret=SECRET),
            approval_secret=SECRET,
        )
        self.ordinary_calls: list[str] = []
        self.approved_calls: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.ordinary_calls.append(name)
        return await super().call_tool(name, arguments)

    async def call_approved_escalation(
        self,
        arguments: dict[str, Any],
        *,
        approval_id: str,
        thread_id: str,
        tool_call_id: str,
    ) -> CallToolResult:
        self.approved_calls.append(arguments.copy())
        return await super().call_approved_escalation(
            arguments,
            approval_id=approval_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
        )


class ScriptedModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = iter(responses)
        self.seen: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[type[Any] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        async def respond(value: LanguageModelInput) -> BaseMessage:
            self.seen.append(convert_to_messages(value))
            return next(self.responses)

        return RunnableLambda(respond)


def model_call(
    name: str = "create_escalation",
    args: dict[str, Any] | None = None,
    call_id: str = "write-1",
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args if args is not None else escalation_args(),
                "id": call_id,
            }
        ],
    )


def test_mcp_grant_and_idempotency() -> None:
    async def run() -> None:
        store = InMemoryEscalationStore()
        service = FakeService()
        server = create_server(service, store=store, approval_secret=SECRET)
        async with JalWatchMCPClient(server, approval_secret=SECRET) as client:
            schema = next(
                t.input_schema for t in client.tools if t.name == "create_escalation"
            )
            for forbidden in (
                "approved",
                "human_approved",
                "approved_by",
                "approval_token",
                "authorization",
            ):
                assert forbidden not in schema["properties"]
            normal = await client.call_tool("create_escalation", escalation_args())
            assert normal.is_error and "APPROVAL_REQUIRED" in str(normal.content)
            assert not store.approved_records
            args = escalation_args()
            grant = issue_grant(
                SECRET,
                approval_id="approval-1",
                thread_id="thread-1",
                tool_call_id="write-1",
                name="create_escalation",
                arguments=args,
            )
            meta = {APPROVAL_META_KEY: grant.model_dump(mode="json")}
            invalid = await client._client.call_tool(
                "create_escalation",
                args,
                meta=cast(
                    RequestParamsMeta,
                    {
                        APPROVAL_META_KEY: {
                            **meta[APPROVAL_META_KEY],
                            "signature": "bad",
                        }
                    },
                ),
            )
            assert invalid.is_error and "INVALID_APPROVAL_GRANT" in str(invalid.content)
            for changed in (
                escalation_args(region="Assam"),
                escalation_args(refs=["real-1", "other"]),
                escalation_args(severity="urgent"),
            ):
                tampered = await client._client.call_tool(
                    "create_escalation", changed, meta=cast(RequestParamsMeta, meta)
                )
                assert tampered.is_error
                assert "INVALID_APPROVAL_GRANT" in str(tampered.content)
            assert not store.approved_records
            first = await client.call_approved_escalation(
                args,
                approval_id="approval-1",
                thread_id="thread-1",
                tool_call_id="write-1",
            )
            assert not first.is_error
            second = await client.call_approved_escalation(
                args,
                approval_id="approval-1",
                thread_id="thread-1",
                tool_call_id="write-1",
            )
            assert first.structured_content == second.structured_content
            assert len(store.approved_records) == 1
            mismatch = await client.call_approved_escalation(
                escalation_args(region="Assam"),
                approval_id="approval-1",
                thread_id="thread-1",
                tool_call_id="write-1",
            )
            assert mismatch.is_error and "INVALID_APPROVAL_GRANT" in str(
                mismatch.content
            )
            assert len(store.approved_records) == 1

    asyncio.run(run())


def test_approval_interrupt_reject_and_approve() -> None:
    async def run(decision: str) -> None:
        store = InMemoryEscalationStore()
        async with CountingClient(FakeService(), store) as client:
            model = ScriptedModel(
                [
                    model_call(),
                    AIMessage(content=f"Final: {decision}"),
                ]
            )
            graph = create_hitl_graph(model, client)
            config: RunnableConfig = {"configurable": {"thread_id": f"hitl-{decision}"}}
            paused = await graph.ainvoke(grounded_state(), config=config)
            assert "__interrupt__" in paused
            interrupt_item = paused["__interrupt__"][0]
            assert interrupt_item.response_schema is not None
            payload = interrupt_item.value
            assert payload["approval_id"]
            assert payload["action"] == "create_escalation"
            assert payload["alert_refs"] == ["real-1"]
            assert payload["region"] == "Bihar"
            assert "no public flood warning" in payload["notice"]
            assert "secret" not in json.dumps(payload).casefold()
            assert SECRET.hex() not in json.dumps(payload)
            assert client.approved_calls == []
            assert store.approved_records == {}
            snapshot = await graph.aget_state(config)
            assert (
                snapshot.values["pending_approval"].approval_id
                == payload["approval_id"]
            )
            assert (
                snapshot.values["proposed_escalation"].approval_status
                is ApprovalStatus.PENDING
            )
            final = await graph.ainvoke(
                Command(resume={"decision": decision}), config=config
            )
            assert final["pending_approval"] is None
            assert final["proposed_escalation"].approval_status.value == (
                "approved" if decision == "approve" else "rejected"
            )
            tool_messages = [m for m in final["messages"] if isinstance(m, ToolMessage)]
            latest = tool_messages[-1]
            assert latest.tool_call_id == "write-1"
            assert len(model.seen) == 2
            assert isinstance(model.seen[-1][-1], ToolMessage)
            if decision == "approve":
                assert latest.status == "success"
                assert client.approved_calls == [escalation_args()]
                assert len(store.approved_records) == 1
            else:
                assert latest.status == "error"
                assert "HUMAN_REJECTED" in str(latest.content)
                assert client.approved_calls == []
                assert store.approved_records == {}

    asyncio.run(run("reject"))
    asyncio.run(run("approve"))


def test_approval_decision_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate({"decision": "maybe"})


def test_escalation_postcondition_matches_approved_action() -> None:
    args = escalation_args()
    record = {
        "escalation_id": "E-1",
        "approval_id": "A-1",
        **args,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "created",
    }
    context = ToolContext(
        "create_escalation",
        args,
        "write-1",
        [],
        datetime.now(UTC),
        approval_id="A-1",
    )
    assert post_tool_hook(context, record).allowed
    for changed in (
        {**record, "alert_refs": ["different"]},
        {**record, "region": "Assam"},
        {**record, "severity": "urgent"},
    ):
        rejected = post_tool_hook(context, changed)
        assert rejected.error is not None
        assert rejected.error.code == "INVALID_TOOL_RESULT"


def test_expired_grant_is_rejected() -> None:
    async def run() -> None:
        store = InMemoryEscalationStore()
        async with JalWatchMCPClient(
            create_server(FakeService(), store=store, approval_secret=SECRET),
            approval_secret=SECRET,
        ) as client:
            grant = issue_grant(
                SECRET,
                approval_id="expired",
                thread_id="thread",
                tool_call_id="call",
                name="create_escalation",
                arguments=escalation_args(),
                now=datetime.now(UTC) - timedelta(minutes=2),
            )
            expired = await client._client.call_tool(
                "create_escalation",
                escalation_args(),
                meta=cast(
                    RequestParamsMeta,
                    {APPROVAL_META_KEY: grant.model_dump(mode="json")},
                ),
            )
            assert expired.is_error
            assert "INVALID_APPROVAL_GRANT" in str(expired.content)
            assert not store.approved_records

    asyncio.run(run())


def test_graph_routes_reads_and_isolates_protected_calls() -> None:
    async def run() -> None:
        service = FakeService()
        store = InMemoryEscalationStore()
        async with CountingClient(service, store) as client:
            no_tool = create_hitl_graph(
                ScriptedModel([AIMessage(content="No tool needed")]), client
            )
            final = await no_tool.ainvoke(
                create_initial_state("Explain capability"),
                config={"configurable": {"thread_id": "no-tool"}},
            )
            assert final["messages"][-1].content == "No tool needed"
            assert not client.ordinary_calls

            read = create_hitl_graph(
                ScriptedModel(
                    [
                        model_call(
                            "list_active_cwc_alerts", {"region": "Bihar"}, "read"
                        ),
                        AIMessage(content="Read complete"),
                    ]
                ),
                client,
            )
            final = await read.ainvoke(
                create_initial_state("Check Bihar"),
                config={"configurable": {"thread_id": "read"}},
            )
            assert final["messages"][-1].content == "Read complete"
            assert client.ordinary_calls == ["list_active_cwc_alerts"]

            mixed = AIMessage(
                content="",
                tool_calls=[
                    {"name": "list_active_cwc_alerts", "args": {}, "id": "mixed-read"},
                    {
                        "name": "create_escalation",
                        "args": escalation_args(),
                        "id": "mixed-write",
                    },
                ],
            )
            graph = create_hitl_graph(
                ScriptedModel([mixed, AIMessage(content="Split needed")]), client
            )
            final = await graph.ainvoke(
                grounded_state(),
                config={"configurable": {"thread_id": "mixed"}},
            )
            mixed_errors = [m for m in final["messages"] if isinstance(m, ToolMessage)][
                -2:
            ]
            assert [m.tool_call_id for m in mixed_errors] == [
                "mixed-read",
                "mixed-write",
            ]
            assert all(
                "PROTECTED_ACTION_MUST_BE_SEPARATE" in str(m.content)
                for m in mixed_errors
            )
            assert client.ordinary_calls == ["list_active_cwc_alerts"]

            double = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_escalation",
                        "args": escalation_args(),
                        "id": "w1",
                    },
                    {
                        "name": "create_escalation",
                        "args": escalation_args(),
                        "id": "w2",
                    },
                ],
            )
            graph = create_hitl_graph(
                ScriptedModel([double, AIMessage(content="Split needed")]), client
            )
            final = await graph.ainvoke(
                grounded_state(),
                config={"configurable": {"thread_id": "double"}},
            )
            errors = [m for m in final["messages"] if isinstance(m, ToolMessage)][-2:]
            assert [m.tool_call_id for m in errors] == ["w1", "w2"]
            assert not client.approved_calls

    asyncio.run(run())


def test_ungrounded_call_does_not_interrupt_or_write() -> None:
    async def run() -> None:
        store = InMemoryEscalationStore()
        async with CountingClient(FakeService(), store) as client:
            graph = create_hitl_graph(
                ScriptedModel(
                    [
                        model_call(args=escalation_args(refs=["fabricated"])),
                        AIMessage(content="Need discovery first"),
                    ]
                ),
                client,
            )
            result = await graph.ainvoke(
                create_initial_state("Escalate fabricated alert"),
                config={"configurable": {"thread_id": "ungrounded"}},
            )
            assert "__interrupt__" not in result
            assert "UNGROUNDED_ESCALATION_EVIDENCE" in str(
                result["messages"][-2].content
            )
            assert not client.approved_calls
            assert not store.approved_records

    asyncio.run(run())


def test_different_thread_cannot_resume_pending_approval() -> None:
    async def run() -> None:
        store = InMemoryEscalationStore()
        async with CountingClient(FakeService(), store) as client:
            graph = create_hitl_graph(
                ScriptedModel(
                    [
                        model_call(),
                        AIMessage(content="Finished"),
                    ]
                ),
                client,
            )
            config: RunnableConfig = {"configurable": {"thread_id": "owner"}}
            paused = await graph.ainvoke(grounded_state(), config=config)
            assert "__interrupt__" in paused
            with pytest.raises(ValueError, match="No pending approval"):
                await resume_approval(
                    graph,
                    thread_id="other",
                    decision=ApprovalDecision(decision="approve"),
                )
            assert not store.approved_records
            completed = await resume_approval(
                graph,
                thread_id="owner",
                decision=ApprovalDecision(decision="reject"),
            )
            assert (
                completed["proposed_escalation"].approval_status
                is ApprovalStatus.REJECTED
            )

    asyncio.run(run())


def test_identical_rejected_action_does_not_reinterrupt() -> None:
    async def run() -> None:
        store = InMemoryEscalationStore()
        async with CountingClient(FakeService(), store) as client:
            graph = create_hitl_graph(
                ScriptedModel(
                    [
                        model_call(),
                        model_call(call_id="repeat"),
                        AIMessage(content="Rejected action was not repeated"),
                    ]
                ),
                client,
            )
            config: RunnableConfig = {"configurable": {"thread_id": "repeat"}}
            paused = await graph.ainvoke(grounded_state(), config=config)
            assert "__interrupt__" in paused
            final = await resume_approval(
                graph,
                thread_id="repeat",
                decision=ApprovalDecision(decision="reject"),
            )
            assert final["messages"][-1].content == "Rejected action was not repeated"
            assert "HUMAN_REJECTED" in str(final["messages"][-2].content)
            assert not client.approved_calls

    asyncio.run(run())
