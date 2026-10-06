"""Task 6 pause/review/resume graph; no disk persistence or HTTP interface."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, ValidationError

from jalwatch.agent.reasoning import ToolBindingModel, create_reasoning_node
from jalwatch.agent.state import JalWatchState
from jalwatch.agent.tool_contracts import CreateEscalation
from jalwatch.domain.models import (
    ApprovalStatus,
    EscalationProposal,
    EscalationSeverity,
    PendingEscalationApproval,
    StationObservation,
    ToolError,
    WaterAlert,
)
from jalwatch.hitl.grant import action_digest
from jalwatch.hooks.policy import (
    ToolContext,
    observed_alerts,
    post_tool_hook,
    pre_tool_hook,
)
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.mcp.execution import create_mcp_tool_node, result_content
from jalwatch.persistence.compaction import (
    SummaryModel,
    create_compaction_node,
    should_compact,
)
from jalwatch.telemetry.recorder import Telemetry


class ApprovalDecision(BaseModel):
    """The human may approve or reject; tool arguments are immutable here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approve", "reject"]
    comment: str | None = None


def proposal_arguments(proposal: EscalationProposal) -> dict[str, Any]:
    return {
        "alert_refs": list(proposal.alert_refs),
        "region": proposal.region,
        "rationale": proposal.rationale,
        "severity": proposal.severity.value,
    }


def _error(call_id: str, code: str, message: str, *, status: int) -> ToolMessage:
    return ToolMessage(
        name="create_escalation",
        tool_call_id=call_id,
        status="error",
        content=json.dumps(
            {
                "ok": False,
                "error": {
                    "status": status,
                    "code": code,
                    "message": message,
                },
            }
        ),
    )


def route_after_reasoning(
    state: JalWatchState,
) -> Literal["tools", "prepare_approval", "separate_protected", "__end__"]:
    latest = state["messages"][-1]
    if not isinstance(latest, AIMessage) or not latest.tool_calls:
        return "__end__"
    protected = [
        call for call in latest.tool_calls if call["name"] == "create_escalation"
    ]
    if not protected:
        return "tools"
    if len(protected) != 1 or len(latest.tool_calls) != 1:
        return "separate_protected"
    return "prepare_approval"


def separate_protected(state: JalWatchState) -> dict[str, object]:
    latest = state["messages"][-1]
    assert isinstance(latest, AIMessage)
    return {
        "messages": [
            ToolMessage(
                name=call["name"],
                tool_call_id=call["id"] or "",
                status="error",
                content=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "status": 400,
                            "code": "PROTECTED_ACTION_MUST_BE_SEPARATE",
                            "message": "Request create_escalation alone after reads.",
                        },
                    }
                ),
            )
            for call in latest.tool_calls
        ]
    }


def create_hitl_graph(
    model: ToolBindingModel,
    client: JalWatchMCPClient,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    clock: Callable[[], datetime] | None = None,
    summarizer: SummaryModel | None = None,
    telemetry: Telemetry | None = None,
) -> CompiledStateGraph[JalWatchState, None, JalWatchState, JalWatchState]:
    """Build after MCP discovery; keep checkpointer and client alive across resume."""
    now = clock or (lambda: datetime.now(UTC))
    recorder = telemetry or Telemetry()

    def prepare_approval(state: JalWatchState) -> dict[str, object]:
        latest = state["messages"][-1]
        assert isinstance(latest, AIMessage) and len(latest.tool_calls) == 1
        call = latest.tool_calls[0]
        call_id = call["id"] or ""
        context = ToolContext(
            "create_escalation", call["args"], call_id, state["messages"], now()
        )
        decision = pre_tool_hook(context)
        if decision.error is not None:
            return {
                "messages": [
                    ToolMessage(
                        name="create_escalation",
                        tool_call_id=call_id,
                        status="error",
                        content=decision.error.payload(),
                    )
                ],
                "pending_approval": None,
            }
        try:
            args = CreateEscalation.model_validate(call["args"])
        except ValidationError:
            return {
                "messages": [
                    _error(
                        call_id,
                        "INVALID_TOOL_ARGUMENTS",
                        "Escalation arguments are invalid.",
                        status=400,
                    )
                ],
                "pending_approval": None,
            }
        previous = state["proposed_escalation"]
        if (
            previous is not None
            and previous.approval_status is ApprovalStatus.REJECTED
            and proposal_arguments(previous) == args.model_dump(mode="json")
        ):
            return {
                "messages": [
                    _error(
                        call_id,
                        "HUMAN_REJECTED",
                        "This rejected proposal needs new circumstances.",
                        status=403,
                    )
                ],
                "pending_approval": None,
            }
        proposal = EscalationProposal(
            proposal_id=str(uuid4()),
            alert_refs=args.alert_refs,
            region=args.region,
            rationale=args.rationale,
            severity=args.severity,
        )
        pending = PendingEscalationApproval(
            approval_id=str(uuid4()),
            tool_call_id=call_id,
            action_digest=action_digest(proposal_arguments(proposal)),
        )
        recorder.event(
            "approval_requested",
            approval_id=pending.approval_id,
            action="create_escalation",
            region=proposal.region,
            local_priority=proposal.severity.value,
            alert_ref_count=len(proposal.alert_refs),
        )
        return {"proposed_escalation": proposal, "pending_approval": pending}

    def after_prepare(state: JalWatchState) -> Literal["approval", "reasoning"]:
        return "approval" if state.get("pending_approval") is not None else "reasoning"

    def approval(state: JalWatchState) -> dict[str, object]:
        proposal = state["proposed_escalation"]
        pending = state.get("pending_approval")
        if proposal is None or pending is None:
            raise ValueError("Approval requires a checkpointed proposal")
        if action_digest(proposal_arguments(proposal)) != pending.action_digest:
            return {
                "messages": [
                    _error(
                        pending.tool_call_id,
                        "APPROVAL_ACTION_CHANGED",
                        "The reviewed action changed; approval was not applied.",
                        status=400,
                    )
                ],
                "pending_approval": None,
            }
        evidence = observed_alerts(state["messages"])
        payload = {
            "approval_id": pending.approval_id,
            "action": "create_escalation",
            **proposal_arguments(proposal),
            "evidence_regions": {
                ref: sorted(evidence.get(ref, set())) for ref in proposal.alert_refs
            },
            "notice": "Internal operational escalation only; no public flood warning.",
        }
        response = interrupt(payload, response_schema=ApprovalDecision)
        # LangGraph re-enters this pure node on resume. Validate that the same
        # checkpointed action is still the one presented to the reviewer.
        if action_digest(proposal_arguments(proposal)) != pending.action_digest:
            return {
                "messages": [
                    _error(
                        pending.tool_call_id,
                        "APPROVAL_ACTION_CHANGED",
                        "The reviewed action changed; approval was not applied.",
                        status=400,
                    )
                ],
                "pending_approval": None,
            }
        decision = ApprovalDecision.model_validate(response)
        recorder.event(
            "approval_decision",
            approval_id=pending.approval_id,
            decision=decision.decision,
        )
        new_status = (
            ApprovalStatus.APPROVED
            if decision.decision == "approve"
            else ApprovalStatus.REJECTED
        )
        return {
            "proposed_escalation": proposal.model_copy(
                update={
                    "approval_status": new_status,
                }
            )
        }

    def after_approval(
        state: JalWatchState,
    ) -> Literal["execute_approved", "reject", "reasoning"]:
        if state.get("pending_approval") is None:
            return "reasoning"
        proposal = state["proposed_escalation"]
        return (
            "execute_approved"
            if proposal and proposal.approval_status is ApprovalStatus.APPROVED
            else "reject"
        )

    def reject(state: JalWatchState) -> dict[str, object]:
        pending = state.get("pending_approval")
        assert pending is not None
        return {
            "pending_approval": None,
            "messages": [
                _error(
                    pending.tool_call_id,
                    "HUMAN_REJECTED",
                    "The proposed escalation was rejected by the human reviewer.",
                    status=403,
                )
            ],
        }

    async def execute_approved(
        state: JalWatchState, config: RunnableConfig
    ) -> dict[str, object]:
        proposal = state["proposed_escalation"]
        pending = state.get("pending_approval")
        assert proposal is not None and pending is not None
        args = proposal_arguments(proposal)
        if action_digest(args) != pending.action_digest:
            return {
                "pending_approval": None,
                "messages": [
                    _error(
                        pending.tool_call_id,
                        "APPROVAL_ACTION_CHANGED",
                        "The approved action changed before execution.",
                        status=400,
                    )
                ],
            }
        context = ToolContext(
            "create_escalation",
            args,
            pending.tool_call_id,
            state["messages"],
            now(),
            approval_id=pending.approval_id,
        )
        pre = pre_tool_hook(context)
        recorder.event(
            "pre_tool_hook",
            tool="create_escalation",
            allowed=pre.allowed,
            code=pre.error.code if pre.error else None,
            mcp_skipped=pre.error is not None,
        )
        if pre.error is not None:
            message = ToolMessage(
                name="create_escalation",
                tool_call_id=pending.tool_call_id,
                content=pre.error.payload(),
                status="error",
            )
        else:
            configurable = config.get("configurable") or {}
            thread_id = configurable.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("A stable configurable.thread_id is required")
            try:
                result = await client.call_approved_escalation(
                    args,
                    approval_id=pending.approval_id,
                    thread_id=thread_id,
                    tool_call_id=pending.tool_call_id,
                )
                if result.is_error:
                    content, status = result_content(result), "error"
                else:
                    post = post_tool_hook(context, result.structured_content)
                    recorder.event(
                        "post_tool_hook",
                        tool="create_escalation",
                        accepted=post.allowed,
                        code=post.error.code if post.error else None,
                    )
                    if post.error is not None:
                        content, status = post.error.payload(), "error"
                    else:
                        content, status = result_content(result), "success"
            except Exception:
                content = json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "MCP_CALL_FAILED",
                            "message": "Approved MCP call failed.",
                        },
                    }
                )
                status = "error"
            message = ToolMessage(
                name="create_escalation",
                tool_call_id=pending.tool_call_id,
                content=content,
                status=status,
            )
        return {"pending_approval": None, "messages": [message]}

    builder = StateGraph(JalWatchState)
    builder.add_node(
        "reasoning",
        RunnableLambda(
            create_reasoning_node(model, client.schemas_for_model(), telemetry=recorder)
        ),
    )
    builder.add_node(
        "tools",
        RunnableLambda(create_mcp_tool_node(client, clock=now, telemetry=recorder)),
    )
    builder.add_node("separate_protected", separate_protected)
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("approval", approval)
    builder.add_node("reject", reject)
    builder.add_node("execute_approved", RunnableLambda(execute_approved))
    builder.add_edge(START, "reasoning")

    def route_with_compaction(state: JalWatchState) -> str:
        route = route_after_reasoning(state)
        if route == "__end__" and summarizer is not None and should_compact(state):
            return "compact_final"
        return route

    builder.add_conditional_edges("reasoning", route_with_compaction)
    if summarizer is not None:
        builder.add_node(
            "compact_cycle",
            RunnableLambda(create_compaction_node(summarizer, telemetry=recorder)),
        )
        builder.add_node(
            "compact_final",
            RunnableLambda(create_compaction_node(summarizer, telemetry=recorder)),
        )
        builder.add_edge("compact_cycle", "reasoning")
        builder.add_edge("compact_final", "__end__")
        builder.add_conditional_edges(
            "tools",
            lambda state: "compact_cycle" if should_compact(state) else "reasoning",
        )
    else:
        builder.add_edge("tools", "reasoning")
    builder.add_edge("separate_protected", "reasoning")
    builder.add_conditional_edges("prepare_approval", after_prepare)
    builder.add_conditional_edges("approval", after_approval)
    builder.add_edge("reject", "reasoning")
    builder.add_edge("execute_approved", "reasoning")
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            (cls.__module__, cls.__name__)
            for cls in (
                ApprovalStatus,
                EscalationSeverity,
                EscalationProposal,
                PendingEscalationApproval,
                StationObservation,
                WaterAlert,
                ToolError,
            )
        ]
    )
    return builder.compile(checkpointer=checkpointer or InMemorySaver(serde=serde))
