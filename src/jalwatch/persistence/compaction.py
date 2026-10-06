"""Compact completed conversation prefixes without breaking tool-call protocol."""

import json
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, ToolMessage

from jalwatch.agent.state import JalWatchState
from jalwatch.telemetry.recorder import Telemetry, redact

COMPACTION_THRESHOLD = 10
BASE_PREFIX = 8


class SummaryResult(TypedDict):
    text: str
    input_tokens: int | None
    output_tokens: int | None


class SummaryModel(Protocol):
    async def summarize(
        self, previous_summary: str | None, messages: Sequence[AnyMessage]
    ) -> SummaryResult: ...


def compactable_prefix(messages: Sequence[AnyMessage]) -> int:
    """Return shortest prefix >=8 closed under AI tool-call/result relationships."""
    if len(messages) <= COMPACTION_THRESHOLD:
        return 0
    end = BASE_PREFIX
    index = 0
    while index < end:
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                call_id = call.get("id")
                if not call_id:
                    return 0
                match = None
                for position in range(index + 1, len(messages)):
                    candidate = messages[position]
                    if (
                        isinstance(candidate, ToolMessage)
                        and candidate.tool_call_id == call_id
                    ):
                        match = position
                        break
                if match is None:
                    return 0
                end = max(end, match + 1)
        index += 1
    return end if end < len(messages) else 0


def should_compact(state: JalWatchState) -> bool:
    if state.get("pending_approval") is not None:
        return False
    if (
        state["proposed_escalation"] is not None
        and state["proposed_escalation"].approval_status.value == "pending"
    ):
        return False
    return compactable_prefix(state["messages"]) > 0


def summary_input(messages: Sequence[AnyMessage]) -> str:
    """Render selected source messages as data, omitting private metadata."""
    return json.dumps(
        [
            {
                "role": message.type,
                "content": redact(message.content),
                "tool_name": message.name if isinstance(message, ToolMessage) else None,
                "tool_status": message.status
                if isinstance(message, ToolMessage)
                else None,
                "tool_calls": [
                    {"name": call["name"], "args": redact(call["args"])}
                    for call in message.tool_calls
                ]
                if isinstance(message, AIMessage)
                else None,
            }
            for message in messages
        ],
        ensure_ascii=False,
        default=str,
    )


def create_compaction_node(
    model: SummaryModel,
    *,
    telemetry: Telemetry | None = None,
) -> Callable[[JalWatchState], Coroutine[Any, Any, dict[str, object]]]:
    recorder = telemetry or Telemetry()

    async def compact(state: JalWatchState) -> dict[str, object]:
        if not should_compact(state):
            return {}
        end = compactable_prefix(state["messages"])
        selected = state["messages"][:end]
        if any(not message.id for message in selected):
            raise ValueError("Compaction requires stable IDs on every deleted message")
        with recorder.span(
            "summarize_context",
            message_count_before=len(state["messages"]),
            messages_compacted=end,
            previous_summary_present=bool(state.get("conversation_summary")),
            summarizer_model=getattr(model, "model", "test-or-local"),
        ):
            result = await model.summarize(state.get("conversation_summary"), selected)
        paragraph = " ".join(result["text"].split())
        if not paragraph:
            raise ValueError("Local summarizer returned an empty summary")
        recorder.event(
            "context_summary",
            message_count_before=len(state["messages"]),
            messages_compacted=end,
            message_count_after=len(state["messages"]) - end,
            previous_summary_present=bool(state.get("conversation_summary")),
            summarizer_model=getattr(model, "model", "test-or-local"),
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
        )
        ids = [message.id for message in selected]
        if any(identifier is None for identifier in ids):
            raise ValueError("Compaction requires stable IDs")
        return {
            "conversation_summary": paragraph,
            "messages": [
                RemoveMessage(id=identifier)
                for identifier in ids
                if identifier is not None
            ],
        }

    return compact
