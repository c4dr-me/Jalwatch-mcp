# JalWatch India

**CWC Flood Alert Triage & Operational Escalation Agent**

JalWatch is a single stateful decision-support agent for official Indian flood alerts. It discovers current CWC-related NDMA SACHET RSS entries, reads authoritative CAP details, reasons over the evidence, and can propose an **internal** operational escalation. A human must approve the exact proposed action before execution. JalWatch does **not** predict floods independently, control reservoirs, or issue public emergency warnings.

## Architecture

```text
Client → FastAPI /webhook → LangGraph reasoning → pre/post hooks
                                           ↓
                         dynamic MCP HTTP tools/list, tools/call
                                           ↓
                       mounted /mcp/ → SACHET RSS/CAP tools
LangGraph → SQLite checkpoints, safe compaction, HITL interrupt/resume
Langfuse v4 observes the runtime; it does not control decisions.
```

Local Task 4 demos retain stdio MCP. The hosted agent uses Streamable HTTP with configurable bearer authentication. Task 3’s explicit JSON-RPC registry and dispatcher remain as assignment evidence. The LLM sees dynamically discovered tool schemas; the server enforces the protected write boundary. See [architecture](docs/architecture.md), [data sources](docs/data-sources.md), and [release audit](docs/release-audit.md).

## Assignment status

| Task | Status |
|---|---|
| 1 State Management | Complete |
| 2 Reasoning Node | Complete |
| 3 Tool Provisioning and JSON-RPC | Complete |
| 4 MCP | Complete |
| 5 Lifecycle Hooks | Complete |
| 6 Human-in-the-Loop | Complete |
| 7 Compaction and SQLite Checkpoints | Complete |
| 8 Langfuse Telemetry | Complete |
| 9 Evaluation and Deployment | Implementation complete; live judge and local API/MCP verified; Docker/Render verification pending |

There is no Task 10. The ten recorded evaluation traces are deterministic fixtures, not production traffic. A live judge report is authoritative only after all ten were scored; see [evaluation guide](docs/evaluation.md).

## Setup and local run

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). From the repository root:

```powershell
uv sync --locked
Copy-Item .env.example .env
# Edit .env with your own values; never commit it.
uv run --env-file .env uvicorn jalwatch.api.app:app --host 0.0.0.0 --port 8000
```

Set `GROQ_API_KEY` and `JALWATCH_LLM_MODEL` to a currently supported Groq tool-capable model before live agent calls. `GET /health` is always public and does not call Groq or SACHET.

### Choose an authentication mode

| Mode | Environment | API and `/mcp/` access | Intended use |
|---|---|---|---|
| Authenticated (default) | `JALWATCH_AUTH_REQUIRED=true` and a non-empty `JALWATCH_API_KEY` | Send `Authorization: Bearer <key>` | Controlled deployment |
| Public demo | `JALWATCH_AUTH_REQUIRED=false` | No token required | Short-lived assignment demonstration |

For authenticated local use, set both variables in `.env`, start the server, and call:

```powershell
$headers = @{ Authorization = "Bearer <your JALWATCH_API_KEY>" }
Invoke-RestMethod http://127.0.0.1:8000/webhook -Method Post -Headers $headers -ContentType application/json -Body '{"message":"Check current CWC alerts for Bihar."}'
```

For a no-auth local demo, set `JALWATCH_AUTH_REQUIRED=false` in `.env`, restart the server, and omit `-Headers`:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/webhook -Method Post -ContentType application/json -Body '{"message":"Check current CWC alerts for Bihar."}'
```

`render.yaml` selects public-demo mode. For an **existing** Render service, set `JALWATCH_AUTH_REQUIRED=false` in that service's Environment page and redeploy; pushing code alone does not change a Dashboard-managed environment. Switch it back to `true` and set `JALWATCH_API_KEY` to restore bearer protection. Public-demo mode exposes `/webhook`, `/threads/*`, operational status, and `/mcp/` to anyone who knows the URL; avoid real operational data or approvals in this mode. Direct MCP `create_escalation` still requires JalWatch's separate HITL grant.

`POST /webhook` accepts optional `thread_id` and a message. A completed run returns `{thread_id,status:"completed",response}`. An interrupted escalation returns HTTP 202 with `status:"approval_required"` and a safe review payload. Resume the **same** thread with `POST /threads/{thread_id}/resume` and `{"decision":"approve"}` or `{"decision":"reject"}`; this is `Command(resume=...)`, not a user chat message. `GET /threads/{thread_id}` returns safe status. Unknown threads return 404/409. See [deployment](docs/deployment.md).

## MCP, data, and safety

Local stdio: `uv run python -m jalwatch.mcp.smoke`. Remote MCP: `/mcp/` (trailing slash), with bearer authentication when `JALWATCH_AUTH_REQUIRED=true`. The HTTP client initializes once, calls `tools/list`, binds discovered JSON Schemas, then uses `tools/call`. Direct `create_escalation` calls return `APPROVAL_REQUIRED` in either auth mode. Approval is a separate human interrupt and signed exact-action metadata; no approval field appears in model-visible arguments.

### Connect a coding agent to the deployed MCP

Use the Streamable HTTP endpoint `https://jalwatch-mcp.onrender.com/mcp/` (including the trailing slash). For **no-auth mode**, configure that URL with no bearer token. For **authenticated mode**, configure the same URL and supply `Authorization: Bearer <JALWATCH_API_KEY>` through the agent's secret/environment-variable mechanism. Do not paste the key into a tracked configuration file. Both modes discover tools through `tools/list`; direct `create_escalation` remains blocked without human approval.

For Codex CLI on Windows, from `E:\mcp` run:

```powershell
& .\scripts\codex-jalwatch.ps1 mcp list  # Confirm registration
& .\scripts\codex-jalwatch.ps1           # Start a new Codex session
```

The launcher reads `JALWATCH_API_KEY` from the ignored `.env` file when present. For no-auth mode, remove or leave that value empty; Codex then sends no bearer header. For authenticated mode, put the key in `.env`; the launcher passes only the environment-variable name to Codex. Existing Codex sessions do not gain new MCP tools dynamically. In the new session, ask: “List the tools from the jalwatch MCP server, then call `list_affected_regions`.” If your coding agent has an MCP settings UI, select **Streamable HTTP**, enter the endpoint above, and set the bearer header only for authenticated mode.

The official source is [NDMA SACHET](https://sachet.ndma.gov.in/) RSS plus CAP XML. The earlier NWDP station telemetry path could not be verified reliably, so this MVP focuses on CWC-related alerts. Feed presence alone does not mean a CAP alert is active. Source severity and local escalation priority remain distinct. See [data-source findings](docs/data-sources.md).

## Evaluation and validation

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run --env-file .env python -m jalwatch.evaluation.run
```

Set `JALWATCH_JUDGE_MODEL` to a currently supported Groq structured-output model for the live ten-trace Error Recovery judge. Reports are written to `reports/error-recovery-evaluation.json` and `.md` only after all ten judgments succeed. Langfuse is optional and uses `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL`.

Task 7 local demos: `uv run python -m jalwatch.persistence.compaction_demo` and `uv run python -m jalwatch.persistence.demo start --thread-id restart-demo-001`, then `uv run python -m jalwatch.persistence.demo resume --thread-id restart-demo-001 --decision approve` in another process. Local Ollama summary remains available through `JALWATCH_SUMMARY_BACKEND=ollama` and `JALWATCH_SUMMARY_MODEL`; deployed compaction uses the tool-free Groq summary adapter.

## Docker and Render

```powershell
docker build -t jalwatch-india .
docker run --rm -p 8000:8000 -e PORT=8000 -e JALWATCH_AUTH_REQUIRED=false -e GROQ_API_KEY=<key> -e JALWATCH_LLM_MODEL=<model> jalwatch-india
```

[render.yaml](render.yaml) defines one Docker web service with public-demo auth mode and public `/health`. Render prompts for Groq secrets. The app uses Render’s `RENDER_EXTERNAL_HOSTNAME` in MCP host protection, or an exact `JALWATCH_PUBLIC_HOST` override. Free Render storage is ephemeral: SQLite checkpoint state can disappear on restart/redeploy. For a paid persistent disk mounted at `/var/data`, set `JALWATCH_CHECKPOINT_DB=/var/data/jalwatch-checkpoints.sqlite` and `JALWATCH_PERSISTENT_DISK=1`. Local Task 7 disk-restart behavior is separately verified.

The service uses one worker because SQLite and the local escalation store are process-local. The deployed MCP URL is `https://jalwatch-mcp.onrender.com/mcp/`; verify the new auth mode after redeploy. Docker and Render details are in [deployment](docs/deployment.md).

## Viva summary

**State** is checkpointed working memory; **reducer** appends messages; **MCP** discovers and executes tools through JSON-RPC; **hooks** enforce contextual preconditions and result postconditions; **HITL** pauses at an interrupt and resumes by thread ID; **compaction** summarizes closed older message prefixes; **telemetry** observes latency and usage without affecting correctness; **evaluation** grades observable error recovery, not hidden reasoning.

