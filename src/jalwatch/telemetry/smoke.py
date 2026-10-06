"""Optional live Langfuse/Groq/SACHET telemetry smoke; never run in pytest."""

import argparse
import asyncio
import os
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from jalwatch.agent.state import create_initial_state
from jalwatch.hitl.graph import create_hitl_graph
from jalwatch.llm.groq import create_groq_model
from jalwatch.mcp.client import JalWatchMCPClient
from jalwatch.persistence.ollama import OllamaSummaryModel
from jalwatch.persistence.runtime import open_checkpoint_saver
from jalwatch.telemetry.langfuse import (
    LangfuseBackend,
    configured_telemetry,
    observed_run,
)
from jalwatch.telemetry.recorder import session_id


async def run(region: str) -> None:
    telemetry = configured_telemetry()
    thread_id = f"telemetry-smoke-{uuid4()}"
    model = create_groq_model()
    summarizer = (
        OllamaSummaryModel() if os.environ.get("JALWATCH_SUMMARY_MODEL") else None
    )
    async with (
        open_checkpoint_saver() as saver,
        JalWatchMCPClient(telemetry=telemetry) as client,
    ):
        graph = create_hitl_graph(
            model,
            client,
            checkpointer=saver,
            summarizer=summarizer,
            telemetry=telemetry,
        )
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        with observed_run(
            telemetry,
            thread_id=thread_id,
            operation="invoke",
            resumed_from_checkpoint=False,
        ):
            result = await graph.ainvoke(
                create_initial_state(
                    f"What active CWC alerts are currently available for {region}?",
                    selected_region=region,
                ),
                config=config,
            )
            trace_id = (
                telemetry.backend.client.get_current_trace_id()
                if isinstance(telemetry.backend, LangfuseBackend)
                else None
            )
        print("Telemetry enabled:", isinstance(telemetry.backend, LangfuseBackend))
        print("Thread:", thread_id)
        print("Session:", session_id(thread_id))
        if trace_id:
            print("Trace:", trace_id)
        print("Operations: jalwatch_run, reasoning, tools/list, tools/call, hooks")
        if result.get("__interrupt__"):
            print("Paused for human review; no escalation was executed.")
        else:
            print("Final answer:", str(result["messages"][-1].content)[:500])
    telemetry.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="Bihar")
    args = parser.parse_args()
    asyncio.run(run(args.region))


if __name__ == "__main__":
    main()
