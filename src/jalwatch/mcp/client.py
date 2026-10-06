"""One MCP connection and its discovered tool definitions per application run."""

import sys
from collections.abc import Sequence
from secrets import token_bytes
from typing import Any, Self, cast

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client import Transport
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.types import CallToolResult, RequestParamsMeta, TextContent, Tool

from jalwatch.hitl.grant import (
    APPROVAL_META_KEY,
    APPROVAL_SECRET_ENV,
    issue_grant,
)
from jalwatch.telemetry.recorder import Telemetry


def stdio_server_parameters(
    approval_secret: bytes | None = None,
) -> StdioServerParameters:
    """Launch the installed JalWatch package using the active Python environment."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "jalwatch.mcp.server"],
        env={APPROVAL_SECRET_ENV: approval_secret.hex()} if approval_secret else None,
    )


def model_tool_schemas(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    """Convert MCP discovery into LangChain's OpenAI-compatible tool shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or tool.title or tool.name,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _logical_error_code(result: CallToolResult) -> str | None:
    if not result.is_error:
        return None
    structured = result.structured_content
    if isinstance(structured, dict):
        error = structured.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str):
                return code
    for item in result.content:
        if isinstance(item, TextContent):
            prefix = item.text.partition(":")[0].strip()
            if prefix.isupper() and prefix.replace("_", "").isalnum():
                return prefix
    return "MCP_TOOL_ERROR"


class JalWatchMCPClient:
    """Lifecycle owner; discovered metadata is retained until the session closes."""

    def __init__(
        self,
        target: StdioServerParameters | MCPServer | Transport | str | None = None,
        *,
        approval_secret: bytes | None = None,
        telemetry: Telemetry | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._approval_secret = approval_secret or token_bytes(32)
        self._client = Client(target or stdio_server_parameters(self._approval_secret))
        self._http_client = http_client
        self.telemetry = telemetry or Telemetry()
        self.tools: tuple[Tool, ...] = ()

    async def __aenter__(self) -> Self:
        try:
            await self._client.__aenter__()
        except BaseException:
            if self._http_client is not None:
                await self._http_client.aclose()
            raise
        try:
            found: list[Tool] = []
            cursor: str | None = None
            try:
                with self.telemetry.span("mcp_tools_list", method="tools/list"):
                    while True:
                        page = await self._client.list_tools(cursor=cursor)
                        found.extend(page.tools)
                        if page.next_cursor is None:
                            break
                        cursor = page.next_cursor
            except Exception:
                self.telemetry.event("mcp_tools_discovered", success=False)
                raise
            self.tools = tuple(found)
            self.telemetry.event(
                "mcp_tools_discovered",
                tool_names=[tool.name for tool in found],
                tool_count=len(found),
                success=True,
            )
            return self
        except BaseException:
            await self._client.__aexit__(None, None, None)
            if self._http_client is not None:
                await self._http_client.aclose()
            raise

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(None, None, None)
        if self._http_client is not None:
            await self._http_client.aclose()
        self.tools = ()

    @classmethod
    def http(
        cls,
        url: str,
        *,
        bearer_token: str | None,
        approval_secret: bytes,
        telemetry: Telemetry | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> Self:
        """Create a Streamable HTTP session; discovery stays dynamic."""
        if not url.startswith(("http://", "https://")):
            raise ValueError("HTTP MCP needs a URL")
        http_client = http_client or httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {bearer_token}"} if bearer_token else {},
            timeout=30.0,
        )
        return cls(
            streamable_http_client(url, http_client=http_client),
            approval_secret=approval_secret,
            telemetry=telemetry,
            http_client=http_client,
        )

    def schemas_for_model(self) -> list[dict[str, Any]]:
        return model_tool_schemas(self.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name not in {tool.name for tool in self.tools}:
            raise ValueError(f"UNADVERTISED_TOOL: {name}")
        with self.telemetry.span(
            "mcp_tool_call", method="tools/call", tool=name, arguments=arguments
        ):
            result = await self._client.call_tool(name, arguments)
        self.telemetry.event(
            "mcp_tool_result",
            tool=name,
            success=not result.is_error,
            error_code=_logical_error_code(result),
        )
        return result

    async def call_approved_escalation(
        self,
        arguments: dict[str, Any],
        *,
        approval_id: str,
        thread_id: str,
        tool_call_id: str,
    ) -> CallToolResult:
        """Trusted host path; no approval fields enter model-visible arguments."""
        if "create_escalation" not in {tool.name for tool in self.tools}:
            raise ValueError("Escalation tool was not discovered")
        grant = issue_grant(
            self._approval_secret,
            approval_id=approval_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            name="create_escalation",
            arguments=arguments,
        )
        metadata = cast(
            RequestParamsMeta, {APPROVAL_META_KEY: grant.model_dump(mode="json")}
        )
        with self.telemetry.span(
            "mcp_tool_call",
            method="tools/call",
            tool="create_escalation",
            arguments=arguments,
            approved=True,
        ):
            result = await self._client.call_tool(
                "create_escalation", arguments, meta=metadata
            )
        self.telemetry.event(
            "mcp_tool_result",
            tool="create_escalation",
            success=not result.is_error,
            error_code=_logical_error_code(result),
        )
        return result
