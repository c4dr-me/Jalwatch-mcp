"""Shared working memory only: no graph, nodes, tools, or persistence."""

from typing import NotRequired
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.graph.message import MessagesState

from jalwatch.domain.models import (
    EscalationProposal,
    PendingEscalationApproval,
    StationObservation,
    ToolError,
    WaterAlert,
)


class JalWatchState(MessagesState):
    """Typed state; only messages have a reducer (inherited add_messages).

    Other updates replace the whole field. Evidence keys are absent until a
    retrieval succeeds; an empty list means the retrieval returned no records.
    TypedDict provides static checking, not validation of arbitrary assignment.
    Domain records validate when constructed; see TypeAdapter in the tests for
    validating and serializing a complete state without a persistence service.
    """

    user_request: str
    selected_region: str | None
    selected_station_ids: list[str]
    station_observations: NotRequired[list[StationObservation]]
    water_history: NotRequired[list[StationObservation]]
    active_alerts: NotRequired[list[WaterAlert]]
    proposed_escalation: EscalationProposal | None
    pending_approval: NotRequired[PendingEscalationApproval | None]
    conversation_summary: str | None
    tool_errors: list[ToolError]


def create_initial_state(
    user_request: str, *, selected_region: str | None = None
) -> JalWatchState:
    """Start one investigation with a normalized request and fresh containers.

    Region selection is explicit; this helper does not infer geography or make
    external calls. Whitespace is normalized without changing case or meaning.
    """
    normalized_request = " ".join(user_request.split())
    if not normalized_request:
        raise ValueError("user_request must not be blank")

    normalized_region = None
    if selected_region is not None:
        normalized_region = " ".join(selected_region.split())
        if not normalized_region:
            raise ValueError("selected_region must not be blank when provided")

    return {
        "messages": [HumanMessage(content=normalized_request, id=str(uuid4()))],
        "user_request": normalized_request,
        "selected_region": normalized_region,
        "selected_station_ids": [],
        "proposed_escalation": None,
        "conversation_summary": None,
        "tool_errors": [],
    }
