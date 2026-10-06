"""The protocol adapter preserves requests and never invokes tools."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolCall
from pydantic import ValidationError

from jalwatch.protocol.jsonrpc import (
    JsonRpcToolRequest,
    tool_call_to_request,
    tool_calls_to_requests,
)


def test_exact_jsonrpc_envelope_and_round_trip() -> None:
    call = ToolCall(
        name="get_alert_details", args={"alert_ref": "ALERT-123"}, id="call_abc"
    )
    request = tool_call_to_request(call)

    assert json.loads(request.model_dump_json()) == {
        "jsonrpc": "2.0",
        "id": "call_abc",
        "method": "tools/call",
        "params": {
            "name": "get_alert_details",
            "arguments": {"alert_ref": "ALERT-123"},
        },
    }
    assert JsonRpcToolRequest.model_validate_json(request.model_dump_json()) == request


def test_multiple_calls_preserve_order_arguments_and_input_independence() -> None:
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "list_active_cwc_alerts",
                "args": {"region": "Bihar"},
                "id": "call-1",
            },
            {
                "name": "search_recent_cwc_alerts",
                "args": {"region": "Bihar", "hours": 24},
                "id": "call-2",
            },
        ],
    )
    before = deepcopy(message)

    requests = tool_calls_to_requests(message)

    assert [request.id for request in requests] == ["call-1", "call-2"]
    assert [request.params.arguments for request in requests] == [
        call["args"] for call in message.tool_calls
    ]
    requests[0].params.arguments["region"] = "Changed only in output"
    assert message == before


def test_nested_json_arguments_remain_json_and_do_not_alias_input() -> None:
    call = ToolCall(
        name="future_tool",
        args={"nested": {"values": [1, 1.5, True, None, "text"]}},
        id="nested",
    )
    request = tool_call_to_request(call)
    assert json.loads(request.model_dump_json())["params"]["arguments"] == call["args"]
    assert request.params.arguments["nested"] is not call["args"]["nested"]


def test_text_response_produces_no_requests() -> None:
    assert tool_calls_to_requests(AIMessage(content="A direct answer")) == []


@pytest.mark.parametrize("call_id", [None, ""])
def test_missing_call_id_is_rejected(call_id: str | None) -> None:
    with pytest.raises(ValidationError, match="id"):
        tool_call_to_request(ToolCall(name="get_alert_details", args={}, id=call_id))


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), datetime(2026, 1, 1, tzinfo=UTC), object()]
)
def test_non_json_values_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        tool_call_to_request(ToolCall(name="test", args={"value": value}, id="call-1"))


def test_malformed_calls_are_not_silently_ignored() -> None:
    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "test", "args": "{bad", "id": "bad-1", "error": "Invalid JSON"}
        ],
    )
    with pytest.raises(ValueError, match="malformed"):
        tool_calls_to_requests(message)
