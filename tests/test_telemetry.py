"""Offline recording backend tests; no Langfuse exporter or model calls."""

import asyncio
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel

from jalwatch.agent.reasoning import create_reasoning_node
from jalwatch.agent.state import create_initial_state
from jalwatch.hitl.demo import OfflineService
from jalwatch.hitl.graph import ApprovalDecision, create_hitl_graph
from jalwatch.hitl.runtime import resume_approval
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.execution import create_mcp_tool_node
from jalwatch.mcp.server import create_server
from jalwatch.persistence.compaction import SummaryResult, create_compaction_node
from jalwatch.telemetry.langfuse import configured_telemetry, observed_run
from jalwatch.telemetry.recorder import Telemetry, redact, session_id


class RecordingBackend:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.spans: list[tuple[str, dict[str, Any]]] = []

    def event(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, data))

    @contextmanager
    def span(self, name: str, data: dict[str, Any]) -> Iterator[None]:
        self.spans.append((name, data))
        yield

    def callbacks(self) -> list[BaseCallbackHandler]:
        return []

    def flush(self) -> None:
        return None


class FailingBackend(RecordingBackend):
    def event(self, name: str, data: dict[str, Any]) -> None:
        raise RuntimeError("exporter unavailable")

    @contextmanager
    def span(self, name: str, data: dict[str, Any]) -> Iterator[None]:
        raise RuntimeError("exporter unavailable")
        yield


class AnswerModel:
    def bind_tools(
        self,
        tools: Sequence[type[BaseModel] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        return RunnableLambda(
            lambda value: AIMessage(
                content="Answer",
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "total_tokens": 17,
                },
            )
        )


class NoUsageModel(AnswerModel):
    def bind_tools(
        self,
        tools: Sequence[type[BaseModel] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        return RunnableLambda(lambda value: AIMessage(content="Answer"))


class FakeSummary:
    async def summarize(
        self, previous_summary: str | None, messages: Sequence[AnyMessage]
    ) -> SummaryResult:
        return {
            "text": "One concise paragraph.",
            "input_tokens": None,
            "output_tokens": None,
        }


def test_disabled_redaction_and_failure_isolation() -> None:
    keys = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
    old = {key: os.environ.pop(key, None) for key in keys}
    try:
        disabled = configured_telemetry()
        disabled.event("safe")
        with disabled.span("safe"):
            pass
    finally:
        for key, value in old.items():
            if value is not None:
                os.environ[key] = value
    assert redact(
        {
            "signature": "abc",
            "Authorization": "Bearer X",
            "groq_api_key": "k",
            "JALWATCH_API_KEY": "private",
        }
    ) == {
        "signature": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "groq_api_key": "[REDACTED]",
        "JALWATCH_API_KEY": "[REDACTED]",
    }
    failing = Telemetry(FailingBackend())
    failing.event("cannot_export")
    with failing.span("cannot_export"):
        assert 1 + 1 == 2
    answer = asyncio.run(
        create_reasoning_node(AnswerModel(), telemetry=failing)(
            create_initial_state("Still answer")
        )
    )
    assert answer["messages"][0].content == "Answer"


def test_root_reasoning_usage_and_summary_events() -> None:
    async def run() -> None:
        backend = RecordingBackend()
        telemetry = Telemetry(backend)
        with observed_run(
            telemetry,
            thread_id="same",
            operation="invoke",
            resumed_from_checkpoint=False,
        ):
            await create_reasoning_node(AnswerModel(), telemetry=telemetry)(
                create_initial_state("Question")
            )
            await create_reasoning_node(NoUsageModel(), telemetry=telemetry)(
                create_initial_state("Question")
            )
            state = create_initial_state("Question")
            state["messages"] = [
                HumanMessage(content=str(i), id=str(i)) for i in range(11)
            ]
            await create_compaction_node(FakeSummary(), telemetry=telemetry)(state)
        assert backend.spans[0][0] == "jalwatch_run"
        assert backend.spans[0][1]["session_id"] == session_id("same")
        reasoning = [
            data for name, data in backend.events if name == "reasoning_result"
        ]
        assert reasoning[0]["total_tokens"] == 17
        assert reasoning[1]["total_tokens"] is None
        assert any(name == "context_summary" for name, _ in backend.events)
        assert any(name == "summarize_context" for name, _ in backend.spans)
        with observed_run(
            telemetry,
            thread_id="same",
            operation="resume",
            resumed_from_checkpoint=True,
        ):
            pass
        assert backend.spans[-1][1]["session_id"] == backend.spans[0][1]["session_id"]

    asyncio.run(run())


def test_mcp_discovery_call_and_hook_events() -> None:
    async def run() -> None:
        backend = RecordingBackend()
        telemetry = Telemetry(backend)
        async with JalWatchMCPClient(
            create_server(OfflineService("Bihar")), telemetry=telemetry
        ) as client:
            assert any(name == "mcp_tools_discovered" for name, _ in backend.events)
            await client.call_tool("list_active_cwc_alerts", {"region": "Bihar"})
            assert any(name == "mcp_tool_call" for name, _ in backend.spans)
            state = create_initial_state("Get unknown details")
            state["messages"].append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_alert_details",
                            "args": {"alert_ref": "unknown"},
                            "id": "bad",
                        }
                    ],
                )
            )
            await create_mcp_tool_node(client, telemetry=telemetry)(state)
            assert any(
                name == "pre_tool_hook" and data["code"] == "UNKNOWN_ALERT_REF"
                for name, data in backend.events
            )
            assert not any(
                name == "mcp_tool_result" and data["tool"] == "get_alert_details"
                for name, data in backend.events
            )

        class BadClient:
            tools: tuple[Tool, ...] = (
                Tool(name="list_active_cwc_alerts", input_schema={"type": "object"}),
            )

            async def call_tool(
                self, name: str, arguments: dict[str, Any]
            ) -> CallToolResult:
                return CallToolResult(
                    content=[TextContent(type="text", text="bad")],
                    structured_content={"alerts": [{"alert_ref": "incomplete"}]},
                    is_error=False,
                )

        state = create_initial_state("Check active alerts")
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "list_active_cwc_alerts", "args": {}, "id": "bad-output"}
                ],
            )
        )
        await create_mcp_tool_node(BadClient(), telemetry=telemetry)(state)
        assert any(
            name == "post_tool_hook" and data["code"] == "INVALID_TOOL_RESULT"
            for name, data in backend.events
        )
        assert any(
            name == "mcp_dispatch" and data["tool_call_id"] == "bad-output"
            for name, data in backend.events
        )

    asyncio.run(run())


def test_approval_and_checkpoint_events() -> None:
    from jalwatch.persistence.demo import RestartModel

    async def run() -> None:
        backend = RecordingBackend()
        telemetry = Telemetry(backend)
        secret = b"z" * 32
        server = create_server(OfflineService("Bihar"), approval_secret=secret)
        async with JalWatchMCPClient(
            server, approval_secret=secret, telemetry=telemetry
        ) as client:
            graph = create_hitl_graph(
                RestartModel("Bihar"), client, telemetry=telemetry
            )
            config: RunnableConfig = {
                "configurable": {"thread_id": "telemetry-approval"}
            }
            paused = await graph.ainvoke(
                create_initial_state("Review Bihar"), config=config
            )
            assert paused.get("__interrupt__")
            assert any(name == "approval_requested" for name, _ in backend.events)
            await resume_approval(
                graph,
                thread_id="telemetry-approval",
                decision=ApprovalDecision(decision="reject"),
                telemetry=telemetry,
            )
            names = [name for name, _ in backend.events]
            assert "approval_decision" in names
            assert "checkpoint_resume" in names
            assert "signature" not in str(backend.events)

    asyncio.run(run())
