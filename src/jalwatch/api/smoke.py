"""Manual real-HTTP MCP release check; never prints bearer or grant material."""

import argparse
import asyncio
import os
from secrets import token_bytes

import httpx

from jalwatch.api.app import auth_required_from_env
from jalwatch.mcp.client import JalWatchMCPClient


async def check(url: str, token: str, *, live_read: bool) -> None:
    if auth_required_from_env() and not token:
        raise ValueError("Set JALWATCH_API_KEY when authentication is required")
    root = url.rstrip("/")
    async with httpx.AsyncClient(timeout=15) as http:
        health = await http.get(f"{root}/health")
        print("health", health.status_code)
        unauthorized = await http.post(f"{root}/mcp/", json={"jsonrpc": "2.0"})
        print("unauthenticated MCP", unauthorized.status_code)
    async with JalWatchMCPClient.http(
        f"{root}/mcp/", bearer_token=token, approval_secret=token_bytes(32)
    ) as client:
        print("discovered", sorted(tool.name for tool in client.tools))
        if live_read:
            read = await client.call_tool("list_active_cwc_alerts", {"region": "Bihar"})
            alerts = (read.structured_content or {}).get("alerts")
            print("safe read", "error" if read.is_error else "success")
            print(
                "matching alerts",
                len(alerts) if isinstance(alerts, list) else "unknown",
            )
        blocked = await client.call_tool(
            "create_escalation",
            {
                "alert_refs": ["SMOKE-REF"],
                "region": "Bihar",
                "rationale": "Smoke test; must be blocked",
                "severity": "routine",
            },
        )
        print("direct escalation blocked", blocked.is_error)
        if not blocked.is_error or "APPROVAL_REQUIRED" not in str(blocked.content):
            raise RuntimeError("Protected MCP write was not blocked")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--live-read", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        check(
            args.url, os.environ.get("JALWATCH_API_KEY", ""), live_read=args.live_read
        )
    )


if __name__ == "__main__":
    main()
