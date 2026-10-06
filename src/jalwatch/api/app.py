"""One ASGI service: authenticated graph API plus mounted MCP Streamable HTTP."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from secrets import token_bytes
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field

from jalwatch.api.runtime import AgentService, GraphService, checkpoint_storage_mode
from jalwatch.hitl.graph import ApprovalDecision
from jalwatch.mcp.server import create_server
from jalwatch.sources.sachet import SachetClient
from jalwatch.tools.sachet_tools import SachetTools


class WebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class ResumeRequest(ApprovalDecision):
    pass


class BearerBoundary:
    """Pure ASGI middleware also protects the mounted MCP transport."""

    def __init__(self, app: Any, api_key: str, auth_required: bool) -> None:
        self.app = app
        self.api_key = api_key
        self.auth_required = auth_required

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] == "/health"
            or not self.auth_required
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        supplied = headers.get(b"authorization", b"").decode("utf-8", "ignore")
        expected = f"Bearer {self.api_key}"
        if not self.api_key or not compare_digest(supplied, expected):
            response = JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Bearer token required",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def allowed_hosts(public_host: str | None) -> list[str]:
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]:*"]
    if public_host:
        if "://" in public_host or "/" in public_host or "*" in public_host:
            raise ValueError("JALWATCH_PUBLIC_HOST must be one exact hostname")
        hosts.append(public_host)
    return hosts


def auth_required_from_env() -> bool:
    value = os.environ.get("JALWATCH_AUTH_REQUIRED", "true").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("JALWATCH_AUTH_REQUIRED must be true or false")
    return value == "true"


def create_app(
    *,
    runtime: AgentService | None = None,
    tools: SachetTools | None = None,
    api_key: str | None = None,
    auth_required: bool | None = None,
    public_host: str | None = None,
) -> FastAPI:
    key = api_key if api_key is not None else os.environ.get("JALWATCH_API_KEY", "")
    require_auth = (
        auth_required if auth_required is not None else auth_required_from_env()
    )
    if (
        require_auth
        and not key
        and os.environ.get("JALWATCH_ENVIRONMENT") == "deployed"
    ):
        raise ValueError("JALWATCH_API_KEY is required in deployed mode")
    host = (
        public_host
        if public_host is not None
        else (
            os.environ.get("JALWATCH_PUBLIC_HOST")
            or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        )
    )
    source = None if tools is not None else SachetClient()
    if tools is not None:
        service_tools = tools
    else:
        assert source is not None
        service_tools = SachetTools(source)
    secret = token_bytes(32)
    mcp = create_server(service_tools, approval_secret=secret)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts(host)),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            app.state.runtime = runtime or GraphService(
                mcp_url=os.environ.get(
                    "JALWATCH_MCP_URL",
                    f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mcp/",
                )
                or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mcp/",
                api_key=key if require_auth else "",
                approval_secret=secret,
                mcp_transport=os.environ.get("JALWATCH_MCP_TRANSPORT", "http"),
            )
            try:
                yield
            finally:
                if runtime is None:
                    await app.state.runtime.aclose()
                if source is not None:
                    await source.aclose()

    app = FastAPI(title="JalWatch India", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BearerBoundary, api_key=key, auth_required=require_auth)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": str(exc.detail),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request body or path is invalid.",
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "JalWatch", "status": "ok", "version": "0.1.0"}

    @app.post("/webhook")
    async def webhook(body: WebhookRequest, request: Request) -> JSONResponse:
        thread_id = body.thread_id or str(uuid4())
        try:
            result = await request.app.state.runtime.invoke(thread_id, body.message)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="JalWatch runtime unavailable"
            ) from exc
        return JSONResponse(
            result, status_code=202 if result["status"] == "approval_required" else 200
        )

    @app.post("/threads/{thread_id}/resume")
    async def resume(
        thread_id: str, body: ResumeRequest, request: Request
    ) -> JSONResponse:
        if not 0 < len(thread_id) <= 128:
            raise HTTPException(status_code=422, detail="Invalid thread_id")
        try:
            result = await request.app.state.runtime.resume(thread_id, body)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="JalWatch runtime unavailable"
            ) from exc
        return JSONResponse(
            result, status_code=202 if result["status"] == "approval_required" else 200
        )

    @app.get("/threads/{thread_id}")
    async def thread_status(thread_id: str, request: Request) -> dict[str, Any]:
        if not 0 < len(thread_id) <= 128:
            raise HTTPException(status_code=422, detail="Invalid thread_id")
        try:
            result = await request.app.state.runtime.status(thread_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="JalWatch runtime unavailable"
            ) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Unknown thread")
        return dict(result)

    @app.get("/operations/storage")
    async def storage() -> dict[str, str]:
        return {"checkpoint_storage": checkpoint_storage_mode()}

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
