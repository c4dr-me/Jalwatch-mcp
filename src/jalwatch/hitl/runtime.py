"""Checked resume helper for one in-process HITL graph runtime."""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from jalwatch.agent.state import JalWatchState
from jalwatch.hitl.graph import ApprovalDecision
from jalwatch.telemetry.recorder import Telemetry


async def resume_approval(
    graph: CompiledStateGraph[JalWatchState, None, JalWatchState, JalWatchState],
    *,
    thread_id: str,
    decision: ApprovalDecision,
    telemetry: Telemetry | None = None,
) -> dict[str, Any]:
    """Resume only the checkpointed approval belonging to this thread."""
    if not thread_id.strip():
        raise ValueError("thread_id must not be blank")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values or snapshot.values.get("pending_approval") is None:
        raise ValueError("No pending approval for this thread_id")
    if not any(task.interrupts for task in snapshot.tasks):
        raise ValueError("This thread is not paused for approval")
    (telemetry or Telemetry()).event(
        "checkpoint_resume",
        thread_id=thread_id,
        resumed_from_checkpoint=True,
        pending_approval=True,
    )
    return await graph.ainvoke(
        Command(resume=decision.model_dump(mode="json")), config=config
    )
