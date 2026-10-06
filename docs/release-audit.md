# Release audit (2026-10-06)

The repository was inspected before release edits. The original 98-test suite,
Ruff, formatting, and strict mypy all passed. This classification prevents
cleanup from erasing evidence of earlier assignment tasks.

| Class | Items | Decision |
|---|---|---|
| Keep: production | `src/jalwatch/agent`, `domain`, `sources`, `tools/sachet_tools.py`, `hooks/policy.py`, `mcp/server.py`, `mcp/client.py`, `mcp/execution.py`, `hitl/graph.py`, `hitl/grant.py`, `persistence`, `telemetry`, API/evaluation modules | Running application and Task 9 |
| Keep: assignment evidence | Task 3 `protocol/jsonrpc.py`, `tools/registry.py`, `agent/execution.py`, `agent/graph.py`; Task 4 stdio server/client/demo; Task 5 hook tests; Task 6 HITL demo/tests; Task 7 compaction/restart demos; Task 8 telemetry tests; NWDP/SACHET feasibility probes; `assignment.txt` | Directly demonstrate completed work, including the NWDP feasibility decision |
| Merge | No safe duplicate source implementation found; Task 3 and Task 4 paths differ deliberately | Preserve both |
| Delete | No source file qualified as dead after audit | None |
| Ignore | `.env`, `.venv`, `.tools`, Python/test/type/lint caches, `dist`, `Microsoft/Windows/PowerShell/ModuleAnalysisCache`, `data/*.sqlite*`, logs, coverage, local telemetry exports | Covered by `.gitignore`/`.dockerignore` |

The workspace had generated `dist`, a PowerShell cache, and a local SQLite
checkpoint file. Automatic approval review rejected the recursive cleanup
command, so they were left on disk and excluded from Git/Docker. No repository
source or historical assignment evidence was deleted.
