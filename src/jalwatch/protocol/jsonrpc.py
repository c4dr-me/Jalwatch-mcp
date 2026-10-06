"""Typed JSON-RPC envelopes and pure tool-call request translation."""

from typing import Annotated, Literal

from langchain_core.messages import AIMessage, ToolCall
from pydantic import BaseModel, ConfigDict, Field, JsonValue

type Identifier = Annotated[str, Field(min_length=1)]


class ToolCallParams(BaseModel):
    """Tool name and untouched JSON arguments, not executable Python."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    name: Identifier
    arguments: dict[str, JsonValue]


class JsonRpcToolRequest(BaseModel):
    """A correlated tools/call request, ready for a future executor/transport."""

    model_config = ConfigDict(extra="forbid", strict=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: Identifier
    method: Identifier = "tools/call"
    params: ToolCallParams


class JsonRpcError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str
    data: dict[str, JsonValue] | None = None


class JsonRpcSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: Identifier
    result: JsonValue


class JsonRpcFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: Identifier
    error: JsonRpcError


type JsonRpcResponse = JsonRpcSuccess | JsonRpcFailure


def tool_call_to_request(tool_call: ToolCall) -> JsonRpcToolRequest:
    """Preserve name, ID and arguments; reject missing IDs or non-JSON values.

    This validates the envelope only, not a tool's argument contract. It never
    dispatches a function, grants approval, or transmits a request.
    """
    return JsonRpcToolRequest.model_validate(
        {
            "id": tool_call["id"],
            "params": {"name": tool_call["name"], "arguments": tool_call["args"]},
        }
    )


def tool_calls_to_requests(message: AIMessage) -> list[JsonRpcToolRequest]:
    """Translate every parsed call in order; do not silently drop malformed calls."""
    if message.invalid_tool_calls:
        raise ValueError("AIMessage contains malformed tool calls; cannot translate")
    return [tool_call_to_request(tool_call) for tool_call in message.tool_calls]
