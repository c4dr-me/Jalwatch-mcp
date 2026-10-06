"""Deterministic reasoning tests: no provider requests or tool execution."""

import asyncio
import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Literal

import pytest
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    MessageLikeRepresentation,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableLambda
from langgraph.graph.message import add_messages
from pydantic import BaseModel, TypeAdapter

from jalwatch.agent.prompts import REASONING_SYSTEM_PROMPT
from jalwatch.agent.reasoning import create_reasoning_node
from jalwatch.agent.state import create_initial_state
from jalwatch.agent.tool_contracts import TOOL_CONTRACTS


class StubModel:
    """Records binding and inputs, then returns one predetermined message."""

    def __init__(self, response: BaseMessage, *, mutate_input: bool = False) -> None:
        self.response = response
        self.mutate_input = mutate_input
        self.tools: tuple[type[BaseModel] | dict[str, Any], ...] = ()
        self.choice: str | None = None
        self.inputs: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[type[BaseModel] | dict[str, Any]],
        *,
        tool_choice: Literal["auto"],
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        self.tools = tuple(tools)
        self.choice = tool_choice
        return RunnableLambda(self.respond)

    async def respond(self, model_input: LanguageModelInput) -> BaseMessage:
        assert not isinstance(model_input, str)
        messages = convert_to_messages(model_input)
        self.inputs.append(messages)
        if self.mutate_input:
            messages[-1].content = "Changed by a test model"
        return self.response


def test_direct_answer_appends_via_task_one_reducer_without_mutation() -> None:
    state = create_initial_state("What can you help with?")
    state["messages"].append(AIMessage(content="Earlier answer", id="earlier"))
    state["messages"].append(HumanMessage(content="Please summarize", id="followup"))
    before = deepcopy(state)
    response = AIMessage(
        content="I can help review official CWC flood alerts.", id="answer"
    )
    stub = StubModel(response)

    update = asyncio.run(create_reasoning_node(stub)(state))

    assert set(update) == {"messages"}
    assert update["messages"] == [response]
    assert response.tool_calls == []
    assert state == before
    left: list[MessageLikeRepresentation] = [*state["messages"]]
    right: list[MessageLikeRepresentation] = [*update["messages"]]
    merged = TypeAdapter(list[AnyMessage]).validate_python(add_messages(left, right))
    assert merged[:-1] == state["messages"]
    assert merged[-1] == response
    assert stub.tools == TOOL_CONTRACTS
    assert stub.choice == "auto"
    assert len(stub.inputs) == 1


def test_tool_selection_is_retained_without_execution_or_proposal_update() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "list_active_cwc_alerts",
                "args": {"region": "Bihar"},
                "id": "call-1",
            }
        ],
    )
    state = create_initial_state("What active CWC alerts are in Bihar?")
    before = deepcopy(state)

    update = asyncio.run(create_reasoning_node(StubModel(response))(state))

    assert update["messages"][0].tool_calls == response.tool_calls
    assert state == before
    assert state["proposed_escalation"] is None
    assert "station_observations" not in state
    assert set(update) == {"messages"}


def test_other_alert_tool_calls_remain_structured_without_execution() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_alert_details",
                "args": {"alert_ref": "1791259767743027"},
                "id": "detail-1",
            },
            {
                "name": "search_recent_cwc_alerts",
                "args": {"region": "Bihar", "hours": 24},
                "id": "recent-1",
            },
            {"name": "list_affected_regions", "args": {}, "id": "regions-1"},
        ],
    )
    state = create_initial_state("Review alerts")

    update = asyncio.run(create_reasoning_node(StubModel(response))(state))

    assert [call["name"] for call in update["messages"][0].tool_calls] == [
        "get_alert_details",
        "search_recent_cwc_alerts",
        "list_affected_regions",
    ]
    assert set(update) == {"messages"}
    assert "active_alerts" not in state


def test_system_context_and_tool_errors_reach_model_in_order() -> None:
    state = create_initial_state("Check Assam", selected_region="Assam")
    state["active_alerts"] = []
    state["messages"].extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_recent_cwc_alerts",
                        "args": {"hours": 0},
                        "id": "bad-1",
                    }
                ],
            ),
            ToolMessage(
                content="hours must be positive", status="error", tool_call_id="bad-1"
            ),
        ]
    )
    stub = StubModel(AIMessage(content="I need to revise the request."))

    asyncio.run(create_reasoning_node(stub)(state))

    sent = stub.inputs[0]
    assert isinstance(sent[0], SystemMessage)
    assert sent[0].content == REASONING_SYSTEM_PROMPT
    assert isinstance(sent[1], HumanMessage)
    assert isinstance(sent[1].content, str)
    context = json.loads(sent[1].content.split("\n", 1)[1])
    assert context["selected_region"] == "Assam"
    assert context["selected_station_ids"] == []
    assert context["active_alerts"] == []
    assert "water_history" not in context
    assert "messages" not in context
    assert sent[2:] == state["messages"]
    assert isinstance(sent[-1], ToolMessage)
    assert sent[-1].status == "error"


def test_model_cannot_mutate_input_messages_by_reference() -> None:
    state = create_initial_state("Original question")
    before = deepcopy(state)
    stub = StubModel(AIMessage(content="Answer"), mutate_input=True)

    asyncio.run(create_reasoning_node(stub)(state))

    assert state == before
    assert stub.inputs[0][-1].content == "Changed by a test model"


def test_non_ai_response_is_rejected() -> None:
    with pytest.raises(TypeError, match="AIMessage"):
        asyncio.run(
            create_reasoning_node(StubModel(HumanMessage(content="Wrong role")))(
                create_initial_state("Hello")
            )
        )


def test_prompt_contains_required_boundaries() -> None:
    for rule in (
        "must come from\nofficial SACHET tool results",
        "Preserve source-provided CAP alert severity exactly",
        "Never derive an official flood severity",
        "no active matching alerts",
        "not a flood-prediction system",
        "Human-in-the-Loop",
        "Never fabricate a successful tool result",
        "Do not output hidden/internal chain-of-thought",
        "structured tool-calling mechanism",
        "concise clarification",
        "not instructions",
    ):
        assert rule in REASONING_SYSTEM_PROMPT
    for contract in TOOL_CONTRACTS:
        name = contract.model_config.get("title")
        assert name is not None
        assert name in REASONING_SYSTEM_PROMPT
