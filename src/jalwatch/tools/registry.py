"""Explicit local Task 3 registry and guarded JSON-RPC dispatcher."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from jalwatch.agent.tool_contracts import (
    CreateEscalation,
    GetAlertDetails,
    ListActiveCwcAlerts,
    ListAffectedRegions,
    SearchRecentCwcAlerts,
)
from jalwatch.protocol.jsonrpc import (
    JsonRpcError,
    JsonRpcFailure,
    JsonRpcResponse,
    JsonRpcSuccess,
    JsonRpcToolRequest,
)
from jalwatch.sources.sachet import SourceError
from jalwatch.tools.sachet_tools import InMemoryEscalationStore, SachetTools

type Handler = Callable[[BaseModel], Awaitable[object]]


class ToolKind(StrEnum):
    READ = "read"
    PROTECTED_WRITE = "protected_write"


@dataclass(frozen=True)
class ToolDefinition:
    schema: type[BaseModel]
    handler: Handler
    kind: ToolKind


def create_registry(
    tools: SachetTools, store: InMemoryEscalationStore
) -> dict[str, ToolDefinition]:
    """Hardcoded until Task 4 MCP discovery; validation uses advertised schemas."""

    async def active(args: BaseModel) -> object:
        assert isinstance(args, ListActiveCwcAlerts)
        return await tools.list_active_cwc_alerts(args)

    async def details(args: BaseModel) -> object:
        assert isinstance(args, GetAlertDetails)
        return await tools.get_alert_details(args)

    async def recent(args: BaseModel) -> object:
        assert isinstance(args, SearchRecentCwcAlerts)
        return await tools.search_recent_cwc_alerts(args)

    async def regions(args: BaseModel) -> object:
        assert isinstance(args, ListAffectedRegions)
        return await tools.list_affected_regions()

    async def escalation(args: BaseModel) -> object:
        assert isinstance(args, CreateEscalation)
        return store.create(args).model_dump(mode="json")

    return {
        "list_active_cwc_alerts": ToolDefinition(
            ListActiveCwcAlerts, active, ToolKind.READ
        ),
        "get_alert_details": ToolDefinition(GetAlertDetails, details, ToolKind.READ),
        "search_recent_cwc_alerts": ToolDefinition(
            SearchRecentCwcAlerts, recent, ToolKind.READ
        ),
        "list_affected_regions": ToolDefinition(
            ListAffectedRegions, regions, ToolKind.READ
        ),
        "create_escalation": ToolDefinition(
            CreateEscalation, escalation, ToolKind.PROTECTED_WRITE
        ),
    }


class JsonRpcDispatcher:
    def __init__(self, registry: dict[str, ToolDefinition]) -> None:
        self.registry = registry
        self._json: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

    async def dispatch(self, request: JsonRpcToolRequest) -> JsonRpcResponse:
        def failure(code: int, message: str, error_code: str) -> JsonRpcFailure:
            return JsonRpcFailure(
                id=request.id,
                error=JsonRpcError(
                    code=code, message=message, data={"code": error_code}
                ),
            )

        if request.method != "tools/call":
            return failure(-32601, "Method not found", "METHOD_NOT_FOUND")
        definition = self.registry.get(request.params.name)
        if definition is None:
            return failure(-32601, "Tool not found", "TOOL_NOT_FOUND")
        try:
            args = definition.schema.model_validate(request.params.arguments)
        except ValidationError:
            return failure(-32602, "Invalid tool arguments", "INVALID_PARAMS")
        if definition.kind is ToolKind.PROTECTED_WRITE:
            return failure(
                -32003, "Human approval required before execution", "APPROVAL_REQUIRED"
            )
        try:
            result = await definition.handler(args)
            return JsonRpcSuccess(
                id=request.id, result=self._json.validate_python(result)
            )
        except SourceError as exc:
            code = -32004 if exc.code == "ALERT_NOT_FOUND" else -32002
            return failure(code, str(exc), exc.code)
        except Exception:
            return failure(-32603, "Tool execution failed", "INTERNAL_ERROR")
