"""Small failure-isolated runtime telemetry boundary."""

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from hashlib import sha256
from typing import Any, Protocol

from langchain_core.callbacks import BaseCallbackHandler

BLOCKED_KEYS = frozenset(
    {
        "groq_api_key",
        "jalwatch_api_key",
        "langfuse_secret_key",
        "jalwatch_mcp_approval_secret",
        "authorization",
        "signature",
        "approval_grant",
        "com.jalwatch/approval",
        "env",
        "environment_variables",
        "raw_xml",
        "rss_xml",
        "cap_xml",
    }
)


def redact(value: Any) -> Any:
    """Remove known credentials, authorization material, and oversized payloads."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).casefold() in BLOCKED_KEYS
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = value
        for key in (
            "GROQ_API_KEY",
            "JALWATCH_API_KEY",
            "LANGFUSE_SECRET_KEY",
            "JALWATCH_MCP_APPROVAL_SECRET",
        ):
            secret = os.environ.get(key)
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result[:1000] + ("…" if len(result) > 1000 else "")
    return value


def session_id(thread_id: str) -> str:
    """Stable, non-reversible session correlation across process restarts."""
    return "jalwatch-" + sha256(thread_id.encode("utf-8")).hexdigest()[:24]


class TelemetryBackend(Protocol):
    def event(self, name: str, data: dict[str, Any]) -> None: ...
    def span(self, name: str, data: dict[str, Any]) -> AbstractContextManager[None]: ...
    def callbacks(self) -> list[BaseCallbackHandler]: ...
    def flush(self) -> None: ...


class NoopBackend:
    def event(self, name: str, data: dict[str, Any]) -> None:
        return None

    @contextmanager
    def span(self, name: str, data: dict[str, Any]) -> Iterator[None]:
        yield

    def callbacks(self) -> list[BaseCallbackHandler]:
        return []

    def flush(self) -> None:
        return None


class Telemetry:
    """A failing exporter never changes agent control flow."""

    def __init__(self, backend: TelemetryBackend | None = None) -> None:
        self.backend = backend or NoopBackend()

    def event(self, name: str, **data: Any) -> None:
        with suppress(Exception):
            self.backend.event(name, redact(data))

    @contextmanager
    def span(self, name: str, **data: Any) -> Iterator[None]:
        try:
            scope = self.backend.span(name, redact(data))
            scope.__enter__()
        except Exception:
            yield
            return
        try:
            yield
        except BaseException as exc:
            with suppress(Exception):
                scope.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            with suppress(Exception):
                scope.__exit__(None, None, None)

    def callbacks(self) -> list[BaseCallbackHandler]:
        try:
            return self.backend.callbacks()
        except Exception:
            return []

    def flush(self) -> None:
        with suppress(Exception):
            self.backend.flush()
