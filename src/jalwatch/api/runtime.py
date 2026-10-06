"""Process-local graph runtime; no transport objects enter checkpointed state."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import token_bytes
from typing import Any, Protocol

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from jalwatch.agent.state import create_initial_state
from jalwatch.hitl.graph import ApprovalDecision, create_hitl_graph
from jalwatch.hitl.runtime import resume_approval
from jalwatch.llm.groq import create_groq_model
from jalwatch.mcp.client import JalWatchMCPClient, stdio_server_parameters
from jalwatch.persistence.groq import GroqSummaryModel
from jalwatch.persistence.ollama import OllamaSummaryModel
from jalwatch.persistence.runtime import open_checkpoint_saver
from jalwatch.telemetry.langfuse import configured_telemetry, observed_run
from jalwatch.telemetry.recorder import Telemetry


class AgentService(Protocol):
    async def invoke(self, thread_id: str, message: str) -> dict[str, Any]: ...
    async def resume(
        self, thread_id: str, decision: ApprovalDecision
    ) -> dict[str, Any]: ...
    async def status(self, thread_id: str) -> dict[str, Any] | None: ...


def public_result(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Expose only the final answer or the explicit safe approval payload."""
    interrupts = result.get("__interrupt__") or ()
    if interrupts:
        return {
            "thread_id": thread_id,
            "status": "approval_required",
            "approval": interrupts[0].value,
        }
    messages = result.get("messages") or ()
    answer = next(
        (
            m.content
            for m in reversed(messages)
            if isinstance(m, AIMessage) and not m.tool_calls
        ),
        "No final answer was produced.",
    )
    return {"thread_id": thread_id, "status": "completed", "response": answer}


class GraphService:
    def __init__(
        self,
        *,
        mcp_url: str,
        api_key: str,
        approval_secret: bytes,
        mcp_transport: str = "http",
    ) -> None:
        if mcp_transport not in {"http", "stdio"}:
            raise ValueError("JALWATCH_MCP_TRANSPORT must be http or stdio")
        self.mcp_url = mcp_url
        self.api_key = api_key
        self.approval_secret = approval_secret
        self.mcp_transport = mcp_transport
        self._lock = asyncio.Lock()
        self._client: JalWatchMCPClient | None = None
        self._saver_context: Any = None
        self._graph: Any = None
        self.telemetry: Telemetry = configured_telemetry()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
        if self._saver_context is not None:
            await self._saver_context.__aexit__(None, None, None)
            self._saver_context = None
        self.telemetry.flush()

    async def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        client = (
            JalWatchMCPClient.http(
                self.mcp_url,
                bearer_token=self.api_key,
                approval_secret=self.approval_secret,
                telemetry=self.telemetry,
            )
            if self.mcp_transport == "http"
            else JalWatchMCPClient(
                stdio_server_parameters(self.approval_secret),
                approval_secret=self.approval_secret,
                telemetry=self.telemetry,
            )
        )
        await client.__aenter__()
        self._client = client
        context = open_checkpoint_saver()
        try:
            saver = await context.__aenter__()
            self._saver_context = context
            model = create_groq_model()
            summary_backend = os.environ.get("JALWATCH_SUMMARY_BACKEND", "groq")
            summarizer = (
                OllamaSummaryModel()
                if summary_backend == "ollama"
                else GroqSummaryModel(model)
            )
            self._graph = create_hitl_graph(
                model,
                client,
                checkpointer=saver,
                summarizer=summarizer,
                telemetry=self.telemetry,
            )
            return self._graph
        except BaseException:
            await self.aclose()
            raise

    async def invoke(self, thread_id: str, message: str) -> dict[str, Any]:
        async with self._lock:
            graph = await self._ensure_graph()
            config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
            snapshot = await graph.aget_state(config)
            if snapshot.values:
                raise ValueError(
                    "Thread already exists; use a new thread_id or resume it"
                )
            with observed_run(
                self.telemetry,
                thread_id=thread_id,
                operation="invoke",
                resumed_from_checkpoint=False,
            ):
                result = await graph.ainvoke(
                    create_initial_state(message), config=config
                )
            return public_result(thread_id, result)

    async def resume(
        self, thread_id: str, decision: ApprovalDecision
    ) -> dict[str, Any]:
        async with self._lock:
            graph = await self._ensure_graph()
            with observed_run(
                self.telemetry,
                thread_id=thread_id,
                operation="resume",
                resumed_from_checkpoint=True,
            ):
                result = await resume_approval(
                    graph,
                    thread_id=thread_id,
                    decision=decision,
                    telemetry=self.telemetry,
                )
            return public_result(thread_id, result)

    async def status(self, thread_id: str) -> dict[str, Any] | None:
        async with self._lock:
            graph = await self._ensure_graph()
            config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
            snapshot = await graph.aget_state(config)
            if not snapshot.values:
                return None
            pending = snapshot.values.get("pending_approval")
            proposal = snapshot.values.get("proposed_escalation")
            if pending is not None and proposal is not None:
                return {
                    "thread_id": thread_id,
                    "status": "approval_required",
                    "approval": {
                        "approval_id": pending.approval_id,
                        "action": "create_escalation",
                        "alert_refs": list(proposal.alert_refs),
                        "region": proposal.region,
                        "rationale": proposal.rationale,
                        "severity": proposal.severity.value,
                    },
                }
            return {"thread_id": thread_id, "status": "completed"}


def checkpoint_storage_mode() -> str:
    return (
        "persistent_disk"
        if Path(os.environ.get("JALWATCH_CHECKPOINT_DB", "")).is_absolute()
        and os.environ.get("JALWATCH_PERSISTENT_DISK") == "1"
        else "ephemeral"
    )


@asynccontextmanager
async def graph_service(
    *, mcp_url: str, api_key: str, approval_secret: bytes | None = None
) -> AsyncIterator[GraphService]:
    service = GraphService(
        mcp_url=mcp_url,
        api_key=api_key,
        approval_secret=approval_secret or token_bytes(32),
    )
    try:
        yield service
    finally:
        await service.aclose()
