"""Offline fixture MCP stdio process used only by the restart demonstration."""

import asyncio
import os

from jalwatch.hitl.demo import OfflineService
from jalwatch.hitl.grant import APPROVAL_SECRET_ENV
from jalwatch.mcp.server import create_server
from jalwatch.tools.sachet_tools import InMemoryEscalationStore


async def main() -> None:
    secret = bytes.fromhex(os.environ[APPROVAL_SECRET_ENV])
    region = os.environ.get("JALWATCH_DEMO_REGION", "Bihar")
    server = create_server(
        OfflineService(region),
        store=InMemoryEscalationStore(),
        approval_secret=secret,
    )
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
