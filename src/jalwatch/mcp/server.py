"""Stdio MCP adapter over the existing SACHET tool service."""

import asyncio
import logging
import os
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from jalwatch.agent.tool_contracts import (
    CreateEscalation,
    GetAlertDetails,
    ListActiveCwcAlerts,
    SearchRecentCwcAlerts,
)
from jalwatch.domain.models import EscalationSeverity
from jalwatch.hitl.grant import APPROVAL_META_KEY, APPROVAL_SECRET_ENV, verify_grant
from jalwatch.sources.sachet import SachetClient, SourceError
from jalwatch.tools.sachet_tools import InMemoryEscalationStore, SachetTools


def create_server(
    tools: SachetTools,
    store: InMemoryEscalationStore | None = None,
    approval_secret: bytes | None = None,
) -> MCPServer:
    """Expose Task 3 services, with server-side write protection."""
    server = MCPServer("JalWatch India")
    escalation_store = store or InMemoryEscalationStore()
    read = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False)

    @server.tool(annotations=read)
    async def list_active_cwc_alerts(
        region: Annotated[str, Field(min_length=1)] | None = None,
        severity: Annotated[str, Field(min_length=1)] | None = None,
    ) -> dict[str, object]:
        """List active CWC flood/river alerts from official NDMA SACHET CAP data."""
        try:
            return await tools.list_active_cwc_alerts(
                ListActiveCwcAlerts(region=region, severity=severity)
            )
        except SourceError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @server.tool(annotations=read)
    async def get_alert_details(
        alert_ref: Annotated[str, Field(min_length=1)],
    ) -> dict[str, object]:
        """Get authoritative CAP details for an opaque SACHET alert reference."""
        try:
            return await tools.get_alert_details(GetAlertDetails(alert_ref=alert_ref))
        except SourceError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @server.tool(annotations=read)
    async def search_recent_cwc_alerts(
        hours: Annotated[int, Field(strict=True, ge=1, le=72)],
        region: Annotated[str, Field(min_length=1)] | None = None,
    ) -> dict[str, object]:
        """Search CWC alerts in the current SACHET feed from the last 1–72 hours."""
        try:
            return await tools.search_recent_cwc_alerts(
                SearchRecentCwcAlerts(hours=hours, region=region)
            )
        except SourceError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @server.tool(annotations=read)
    async def list_affected_regions() -> dict[str, object]:
        """List sorted Indian regions in currently active CWC alerts."""
        try:
            return await tools.list_affected_regions()
        except SourceError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @server.tool(annotations=write)
    async def create_escalation(
        alert_refs: Annotated[list[str], Field(min_length=1)],
        region: Annotated[str, Field(min_length=1)],
        rationale: Annotated[str, Field(min_length=1)],
        severity: EscalationSeverity,
        ctx: Context,
    ) -> dict[str, object]:
        """Create a local escalation only with trusted human-approval metadata."""
        args = CreateEscalation(
            alert_refs=tuple(alert_refs),
            region=region,
            rationale=rationale,
            severity=severity,
        )
        metadata = ctx.request_context.meta or {}
        raw_grant = metadata.get(APPROVAL_META_KEY)
        if raw_grant is None:
            raise ToolError(
                "APPROVAL_REQUIRED: Human approval required before execution"
            )
        grant = verify_grant(
            approval_secret,
            raw_grant,
            name="create_escalation",
            arguments=args.model_dump(mode="json"),
        )
        if grant is None:
            raise ToolError("INVALID_APPROVAL_GRANT: Trusted approval is invalid")
        try:
            return escalation_store.create_approved(
                args, approval_id=grant.approval_id
            ).model_dump(mode="json")
        except ValueError as exc:
            raise ToolError(
                "INVALID_APPROVAL_GRANT: Approval ID action mismatch"
            ) from exc

    return server


async def _serve() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    source = SachetClient()
    secret_hex = os.environ.get(APPROVAL_SECRET_ENV)
    try:
        secret = bytes.fromhex(secret_hex) if secret_hex else None
    except ValueError:
        secret = None
    server = create_server(SachetTools(source), approval_secret=secret)
    try:
        await server.run_stdio_async()
    finally:
        await source.aclose()


if __name__ == "__main__":
    asyncio.run(_serve())
