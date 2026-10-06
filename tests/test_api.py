"""Offline ASGI and real SDK Streamable HTTP contract tests."""

import asyncio
from typing import Any

import httpx
import httpx2

from jalwatch.agent.tool_contracts import ListActiveCwcAlerts
from jalwatch.api.app import create_app
from jalwatch.hitl.graph import ApprovalDecision
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.tools.sachet_tools import SachetTools


class FakeRuntime:
    def __init__(self) -> None:
        self.thread: str | None = None

    async def invoke(self, thread_id: str, message: str) -> dict[str, Any]:
        self.thread = thread_id
        if "approve" in message:
            return {
                "thread_id": thread_id,
                "status": "approval_required",
                "approval": {"approval_id": "demo-1", "region": "Bihar"},
            }
        return {"thread_id": thread_id, "status": "completed", "response": "No alerts"}

    async def resume(
        self, thread_id: str, decision: ApprovalDecision
    ) -> dict[str, Any]:
        if thread_id != self.thread:
            raise ValueError("No pending approval for this thread_id")
        return {
            "thread_id": thread_id,
            "status": "completed",
            "response": "Created" if decision.decision == "approve" else "Not created",
        }

    async def status(self, thread_id: str) -> dict[str, Any] | None:
        if thread_id != self.thread:
            return None
        return {"thread_id": thread_id, "status": "approval_required"}


class FakeTools(SachetTools):
    def __init__(self) -> None:
        pass

    async def list_active_cwc_alerts(
        self, args: ListActiveCwcAlerts
    ) -> dict[str, object]:
        return {"alerts": [], "region": args.region}


def test_api_auth_and_hitl() -> None:
    async def check() -> None:
        runtime = FakeRuntime()
        app = create_app(runtime=runtime, tools=FakeTools(), api_key="test-secret")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client,
        ):
            assert (await client.get("/health")).status_code == 200
            assert (
                await client.post("/webhook", json={"message": "check"})
            ).status_code == 401
            bad = {"Authorization": "Bearer wrong"}
            assert (await client.get("/threads/x", headers=bad)).status_code == 401
            auth = {"Authorization": "Bearer test-secret"}
            assert (
                await client.post("/webhook", headers=auth, json={})
            ).status_code == 422
            done = await client.post(
                "/webhook", headers=auth, json={"message": "check"}
            )
            assert done.status_code == 200
            assert done.json()["status"] == "completed"
            pending = await client.post(
                "/webhook",
                headers=auth,
                json={"thread_id": "case-1", "message": "approve?"},
            )
            assert pending.status_code == 202
            assert pending.json()["approval"]["approval_id"] == "demo-1"
            assert (
                await client.get("/threads/case-1", headers=auth)
            ).status_code == 200
            assert (await client.get("/threads/other", headers=auth)).status_code == 404
            unknown = await client.post(
                "/threads/other/resume",
                headers=auth,
                json={"decision": "approve"},
            )
            assert unknown.status_code == 409
            rejected = await client.post(
                "/threads/case-1/resume",
                headers=auth,
                json={"decision": "reject"},
            )
            assert rejected.json()["response"] == "Not created"
            approved = await client.post(
                "/threads/case-1/resume",
                headers=auth,
                json={"decision": "approve"},
            )
            assert approved.json()["response"] == "Created"
            assert "Traceback" not in unknown.text

    asyncio.run(check())


def test_mcp_http_discovery_read_and_protected_write() -> None:
    async def check() -> None:
        app = create_app(runtime=FakeRuntime(), tools=FakeTools(), api_key="mcp-secret")
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            http_client = httpx2.AsyncClient(
                transport=transport,
                headers={"Authorization": "Bearer mcp-secret"},
                timeout=30,
            )
            async with JalWatchMCPClient.http(
                "http://localhost/mcp/",
                bearer_token="mcp-secret",
                approval_secret=b"x" * 32,
                http_client=http_client,
            ) as client:
                names = {tool.name for tool in client.tools}
                assert names == {
                    "list_active_cwc_alerts",
                    "get_alert_details",
                    "search_recent_cwc_alerts",
                    "list_affected_regions",
                    "create_escalation",
                }
                assert {
                    schema["function"]["name"] for schema in client.schemas_for_model()
                } == names
                read = await client.call_tool(
                    "list_active_cwc_alerts", {"region": "Bihar"}
                )
                assert not read.is_error
                assert read.structured_content is not None
                blocked = await client.call_tool(
                    "create_escalation",
                    {
                        "alert_refs": ["REF-1"],
                        "region": "Bihar",
                        "rationale": "Review needed",
                        "severity": "elevated",
                    },
                )
                assert blocked.is_error
                assert "APPROVAL_REQUIRED" in str(blocked.content)

    asyncio.run(check())


def test_mcp_http_auth_blocks_sdk_initialize() -> None:
    async def check() -> None:
        app = create_app(runtime=FakeRuntime(), tools=FakeTools(), api_key="secret")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client,
        ):
            result = await client.post("/mcp/", json={"jsonrpc": "2.0"})
            assert result.status_code == 401

    asyncio.run(check())


def test_public_host_allowlist_and_deployed_key(monkeypatch: Any) -> None:
    monkeypatch.setenv("JALWATCH_ENVIRONMENT", "deployed")
    monkeypatch.delenv("JALWATCH_API_KEY", raising=False)
    try:
        create_app(runtime=FakeRuntime(), tools=FakeTools())
        raise AssertionError("Missing deployed API key was accepted")
    except ValueError as exc:
        assert "JALWATCH_API_KEY" in str(exc)

    async def check() -> None:
        app = create_app(
            runtime=FakeRuntime(),
            tools=FakeTools(),
            api_key="secret",
            public_host="jalwatch.onrender.com",
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://jalwatch.onrender.com",
            ) as client,
        ):
            headers = {"Authorization": "Bearer secret"}
            response = await client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "x",
                    "method": "initialize",
                    "params": {},
                },
            )
            assert response.status_code != 421
            response = await client.post(
                "/mcp/",
                headers={**headers, "Host": "attacker.example"},
                json={
                    "jsonrpc": "2.0",
                    "id": "x",
                    "method": "initialize",
                    "params": {},
                },
            )
            assert response.status_code == 421

    asyncio.run(check())
