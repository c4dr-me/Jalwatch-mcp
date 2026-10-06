"""Optional Langfuse v4/OpenTelemetry adapter."""

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler

from jalwatch.telemetry.recorder import Telemetry, session_id


class LangfuseBackend:
    def __init__(self) -> None:
        self.client = get_client()

    def event(self, name: str, data: dict[str, Any]) -> None:
        with self.client.start_as_current_observation(
            as_type="span", name=name, metadata=data
        ):
            pass

    @contextmanager
    def span(self, name: str, data: dict[str, Any]) -> Iterator[None]:
        with self.client.start_as_current_observation(
            as_type="span", name=name, metadata=data
        ):
            yield

    def callbacks(self) -> list[BaseCallbackHandler]:
        return [CallbackHandler()]

    def flush(self) -> None:
        self.client.flush()


def configured_telemetry() -> Telemetry:
    if not all(
        os.environ.get(key)
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
    ):
        return Telemetry()
    try:
        return Telemetry(LangfuseBackend())
    except Exception:
        return Telemetry()


@contextmanager
def observed_run(
    telemetry: Telemetry,
    *,
    thread_id: str,
    operation: str,
    resumed_from_checkpoint: bool,
) -> Iterator[None]:
    session = session_id(thread_id)
    with telemetry.span(
        "jalwatch_run",
        thread_id=thread_id,
        operation=operation,
        resumed_from_checkpoint=resumed_from_checkpoint,
        environment=os.environ.get("JALWATCH_ENVIRONMENT", "local"),
        session_id=session,
    ):
        # propagate_attributes applies the same session to child observations.
        if isinstance(telemetry.backend, LangfuseBackend):
            try:
                scope = propagate_attributes(session_id=session)
                scope.__enter__()
            except Exception:
                yield
                return
            try:
                yield
            finally:
                with suppress(Exception):
                    scope.__exit__(None, None, None)
        else:
            yield
