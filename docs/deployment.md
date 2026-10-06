# Local and Render deployment

## Boundaries

One Uvicorn process serves FastAPI routes and a mounted MCP v2 Streamable HTTP
server at `/mcp/`. FastAPI's top-level lifespan explicitly enters
`mcp.session_manager.run()`; mounted sub-app lifespans do not run themselves.
The agent lazily opens an HTTP MCP client after startup, calls
`tools/list`, binds discovered schemas, and reuses the connection. There is no
HTTP self-call while startup is blocked and no second server process.

`/health` is public and lightweight. Authentication defaults to enabled:
`/webhook`, `/threads/*`, `/operations/storage`, and `/mcp/*` then require
`Authorization: Bearer <key>`; comparison is constant-time. Deployed mode fails
startup without `JALWATCH_API_KEY` while authentication is enabled. The
assignment-demo setting `JALWATCH_AUTH_REQUIRED=false` opens those routes
without a bearer token; the Render Blueprint uses it. This also exposes
thread status and approval/resume routes publicly, so use it only for a demo.
The API key is distinct from Groq credentials and the Task 6 ephemeral HMAC
approval secret. The SDK's DNS-rebinding protection remains on:
localhost hosts and the exact `JALWATCH_PUBLIC_HOST` or Render-provided
`RENDER_EXTERNAL_HOSTNAME` are allowed. Browser CORS is not enabled.

MCP direct writes without a trusted exact-action grant still return
`APPROVAL_REQUIRED`. Only the host's resumed, approved graph path attaches
application-only metadata; the model-visible schema has no approval argument.

## Local commands

```powershell
uv sync --locked
uv run --env-file .env uvicorn jalwatch.api.app:app --host 0.0.0.0 --port 8000
```

`POST /webhook` accepts `{"thread_id":"optional","message":"..."}`.
A pending review returns HTTP 202. Resume with
`POST /threads/{thread_id}/resume` and
`{"decision":"approve"}` or `{"decision":"reject"}`. The same thread ID
selects its SQLite checkpoint. API 400/502 style ToolMessages are graph data,
not automatic web HTTP status codes.

Use the official MCP client against `http://127.0.0.1:8000/mcp/`, passing the
bearer token only when authentication is enabled. `tools/list` and read `tools/call` are safe; direct
`create_escalation` stays blocked. The stdio command
`uv run python -m jalwatch.mcp.smoke` remains the Task 4 local proof.

## Render

Connect this Git repository as a Render Blueprint using `render.yaml`. Enter
`GROQ_API_KEY` and `JALWATCH_LLM_MODEL` when prompted. Set
`JALWATCH_API_KEY` only when `JALWATCH_AUTH_REQUIRED=true`;
never put values in Git. Render sets `PORT` and `RENDER_EXTERNAL_HOSTNAME`.
After deployment verify HTTPS `/health`, the configured auth mode,
`/webhook`, MCP initialize/list/read, direct write rejection, and absence of
421 host errors. The deployed MCP endpoint is
`https://jalwatch-mcp.onrender.com/mcp/`; repeat the auth-mode checks after
changing Render environment settings.

Free Render storage is ephemeral. SQLite works while the instance lives but
cannot guarantee restart recovery after spin-down/redeploy. A paid disk mounted
at `/var/data` can use
`JALWATCH_CHECKPOINT_DB=/var/data/jalwatch-checkpoints.sqlite` and
`JALWATCH_PERSISTENT_DISK=1`. This is independent of the Task 7 local
disk-restart proof. SQLite and the in-process escalation store suit one worker;
they are not a multi-replica durable transaction service. A crash after a
write but before checkpoint remains a production limitation.

Deployment compaction uses tool-free Groq summarization through the existing
summary interface. Local Task 7 still supports offline Ollama. The Dockerfile
uses Python 3.12, locked runtime uv installation, a non-root user, one worker,
and no copied `.env` or SQLite file.
