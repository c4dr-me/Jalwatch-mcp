"""One async SQLite checkpointer per application runtime."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from jalwatch.domain.models import (
    ApprovalStatus,
    EscalationProposal,
    EscalationSeverity,
    PendingEscalationApproval,
    StationObservation,
    ToolError,
    WaterAlert,
)

DEFAULT_DB = Path("data/jalwatch-checkpoints.sqlite")


def checkpoint_path(path: str | Path | None = None) -> Path:
    chosen = path or os.environ.get("JALWATCH_CHECKPOINT_DB") or DEFAULT_DB
    result = Path(chosen)
    if str(result) == ":memory:":
        raise ValueError("Persistent runtime needs a file-backed SQLite path")
    return result


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            (cls.__module__, cls.__name__)
            for cls in (
                ApprovalStatus,
                EscalationProposal,
                EscalationSeverity,
                PendingEscalationApproval,
                StationObservation,
                ToolError,
                WaterAlert,
            )
        ]
    )


@asynccontextmanager
async def open_checkpoint_saver(
    path: str | Path | None = None,
) -> AsyncIterator[AsyncSqliteSaver]:
    target = checkpoint_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(target)) as saver:
        saver.serde = checkpoint_serializer()
        yield saver
