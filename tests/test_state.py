"""Offline tests of the state contract; all monitoring evidence is fictional."""

from datetime import UTC, datetime
from typing import get_args, get_type_hints

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    MessageLikeRepresentation,
    RemoveMessage,
    ToolMessage,
)
from langgraph.constants import END, START
from langgraph.graph.message import add_messages
from langgraph.graph.state import StateGraph
from pydantic import HttpUrl, TypeAdapter, ValidationError

from jalwatch.agent.state import JalWatchState, create_initial_state
from jalwatch.domain.models import (
    ApprovalStatus,
    EscalationProposal,
    EscalationSeverity,
    StationObservation,
    ToolError,
    WaterAlert,
)

REQUEST = "Check whether any monitored stations in Assam currently require attention."


@pytest.fixture
def observation() -> StationObservation:
    return StationObservation(
        station_id="TEST-STATION-1",
        metric="water_level",
        value=12.5,
        unit="m",
        observed_at=datetime(2026, 1, 1, 8, tzinfo=UTC),
        retrieved_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
        source_url=HttpUrl("https://example.invalid/observations/1"),
    )


@pytest.fixture
def proposal() -> EscalationProposal:
    return EscalationProposal(
        proposal_id="TEST-PROPOSAL-1",
        alert_refs=("TEST-ALERT-1",),
        region="Assam",
        rationale="Review the fictional official alert.",
        severity=EscalationSeverity.ELEVATED,
    )


def test_initial_state() -> None:
    state = create_initial_state(f"  {REQUEST}\n", selected_region=" Assam ")

    assert state["user_request"] == REQUEST
    assert state["selected_region"] == "Assam"
    assert state["selected_station_ids"] == []
    assert state["proposed_escalation"] is None
    assert state["tool_errors"] == []
    assert len(state["messages"]) == 1
    assert isinstance(state["messages"][0], HumanMessage)
    assert state["messages"][0].content == REQUEST
    assert state["messages"][0].id is not None
    assert "station_observations" not in state
    assert "water_history" not in state
    assert "active_alerts" not in state


def test_region_is_not_inferred_from_request() -> None:
    assert create_initial_state(REQUEST)["selected_region"] is None


@pytest.mark.parametrize("user_request", ["", " \t\n"])
def test_blank_request_is_rejected(user_request: str) -> None:
    with pytest.raises(ValueError, match="user_request"):
        create_initial_state(user_request)


def test_blank_explicit_region_is_rejected() -> None:
    with pytest.raises(ValueError, match="selected_region"):
        create_initial_state(REQUEST, selected_region="  ")


def test_initial_states_do_not_share_mutable_data() -> None:
    first = create_initial_state(REQUEST)
    second = create_initial_state(REQUEST)

    first["selected_station_ids"].append("TEST-STATION-1")
    first["tool_errors"].append(
        ToolError(tool_call_id="call-1", tool_name="test", code="400", message="Retry")
    )
    first["messages"][0].content = "Changed in the first investigation"
    first["messages"].append(AIMessage(content="Test response"))

    assert second["selected_station_ids"] == []
    assert second["tool_errors"] == []
    assert len(second["messages"]) == 1
    assert second["messages"][0].content == REQUEST
    assert first["messages"][0].id != second["messages"][0].id


def test_message_reducer_is_declared_on_state() -> None:
    hints = get_type_hints(JalWatchState, include_extras=True)
    assert add_messages in get_args(hints["messages"])


def test_messages_append_replace_by_id_and_support_removal() -> None:
    human = HumanMessage(content=REQUEST, id="user-1")
    assistant = AIMessage(content="Original", id="assistant-1")
    messages_adapter = TypeAdapter(list[AnyMessage])

    original: list[MessageLikeRepresentation] = [human]
    appended = messages_adapter.validate_python(add_messages(original, assistant))
    assert [message.id for message in appended] == ["user-1", "assistant-1"]

    appended_input: list[MessageLikeRepresentation] = [*appended]
    replaced = messages_adapter.validate_python(
        add_messages(appended_input, AIMessage(content="Revised", id="assistant-1"))
    )
    assert len(replaced) == 2
    assert replaced[-1].content == "Revised"

    # Only reducer compatibility is tested; no compaction algorithm is implemented.
    replaced_input: list[MessageLikeRepresentation] = [*replaced]
    removed = add_messages(replaced_input, RemoveMessage(id="assistant-1"))
    assert removed == [human]


def test_langgraph_applies_message_merge_and_other_field_replacement() -> None:
    # This tiny graph is a test harness, not an application workflow or reasoning node.
    def hypothetical_update(state: JalWatchState) -> dict[str, object]:
        return {
            "messages": [AIMessage(content="Test selection", id="assistant-1")],
            "selected_station_ids": ["TEST-STATION-2"],
            "active_alerts": [],
        }

    builder = StateGraph(JalWatchState)
    builder.add_node("test_update", hypothetical_update)
    builder.add_edge(START, "test_update")
    builder.add_edge("test_update", END)
    state = create_initial_state(REQUEST)
    state["selected_station_ids"] = ["TEST-STATION-1"]

    result = TypeAdapter(JalWatchState).validate_python(builder.compile().invoke(state))

    assert len(result["messages"]) == 2
    assert result["messages"][0].content == REQUEST
    assert result["selected_station_ids"] == ["TEST-STATION-2"]
    assert result["user_request"] == REQUEST
    assert "active_alerts" in result
    assert result["active_alerts"] == []
    assert "water_history" not in result
    assert state["selected_station_ids"] == ["TEST-STATION-1"]


def test_initial_state_json_round_trip() -> None:
    adapter = TypeAdapter(JalWatchState)
    state = create_initial_state(REQUEST)
    restored = adapter.validate_json(adapter.dump_json(state))

    assert restored == state
    assert isinstance(restored["messages"][0], HumanMessage)
    assert "active_alerts" not in restored


def test_populated_state_json_round_trip(
    observation: StationObservation, proposal: EscalationProposal
) -> None:
    state = create_initial_state(REQUEST, selected_region="Assam")
    state["selected_station_ids"] = [observation.station_id]
    state["station_observations"] = [observation]
    state["water_history"] = [observation]
    state["active_alerts"] = [
        WaterAlert(
            alert_id="TEST-ALERT-1",
            region="Assam",
            severity="Fictional source label",
            message="Fictional alert for serialization tests only.",
            issued_at=observation.observed_at,
            retrieved_at=observation.retrieved_at,
            source_url=HttpUrl("https://example.invalid/alerts/1"),
        )
    ]
    state["proposed_escalation"] = proposal
    state["tool_errors"] = [
        ToolError(
            tool_call_id="call-1",
            tool_name="test_tool",
            code="unavailable",
            message="Fictional temporary failure",
        )
    ]
    state["messages"].extend(
        [
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[{"name": "test_tool", "args": {}, "id": "call-1"}],
            ),
            ToolMessage(
                content="Fictional temporary failure",
                tool_call_id="call-1",
                status="error",
                id="tool-1",
            ),
        ]
    )
    adapter = TypeAdapter(JalWatchState)

    restored = adapter.validate_json(adapter.dump_json(state))

    assert restored == state
    assert isinstance(restored["messages"][-1], ToolMessage)
    assert "station_observations" in restored
    assert isinstance(restored["station_observations"][0], StationObservation)
    assert restored["proposed_escalation"] is not None
    assert restored["proposed_escalation"].approval_status is ApprovalStatus.PENDING


@pytest.mark.parametrize("status", list(ApprovalStatus))
def test_valid_approval_status_round_trip(
    proposal: EscalationProposal, status: ApprovalStatus
) -> None:
    payload = proposal.model_dump(mode="json")
    payload["approval_status"] = status.value
    assert EscalationProposal.model_validate(payload).approval_status is status


def test_invalid_status_is_rejected(proposal: EscalationProposal) -> None:
    payload = proposal.model_dump(mode="json")
    payload["approval_status"] = "automatically_approved"
    with pytest.raises(ValidationError, match="approval_status"):
        EscalationProposal.model_validate(payload)
    with pytest.raises(ValueError):
        ApprovalStatus("automatically_approved")


def test_domain_validation_rejects_invalid_measurements(
    observation: StationObservation,
) -> None:
    payload = observation.model_dump(mode="json")
    payload["value"] = float("nan")
    with pytest.raises(ValidationError, match="value"):
        StationObservation.model_validate(payload)

    payload = observation.model_dump(mode="json")
    payload["observed_at"] = "2026-01-01T08:00:00"
    with pytest.raises(ValidationError, match="observed_at"):
        StationObservation.model_validate(payload)


def test_missing_value_is_not_zero(observation: StationObservation) -> None:
    payload = observation.model_dump(mode="json")
    payload["value"] = None
    assert StationObservation.model_validate(payload).value is None


def test_proposal_requires_alert_refs(proposal: EscalationProposal) -> None:
    for field in ("alert_refs",):
        payload = proposal.model_dump(mode="json")
        payload[field] = []
        with pytest.raises(ValidationError, match=field):
            EscalationProposal.model_validate(payload)
