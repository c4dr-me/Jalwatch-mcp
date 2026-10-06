"""Deterministic, offline compaction demonstration."""

import asyncio
from collections.abc import Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage

from jalwatch.agent.state import create_initial_state
from jalwatch.persistence.compaction import SummaryResult, create_compaction_node


class DemoSummarizer:
    async def summarize(
        self, previous_summary: str | None, messages: Sequence[AnyMessage]
    ) -> SummaryResult:
        return {
            "text": (
                f"Earlier request and {len(messages)} selected messages "
                "were summarized."
            ),
            "input_tokens": None,
            "output_tokens": None,
        }


async def main() -> None:
    state = create_initial_state("Review current CWC alerts in Bihar")
    for index in range(10):
        state["messages"].append(
            HumanMessage(content=f"Question {index}", id=f"question-{index}")
            if index % 2 == 0
            else AIMessage(content=f"Answer {index}", id=f"answer-{index}")
        )
    before = len(state["messages"])
    update = await create_compaction_node(DemoSummarizer())(state)
    removals = update["messages"]
    assert isinstance(removals, list)
    assert all(isinstance(item, RemoveMessage) for item in removals)
    after_count = before - len(removals)
    print("Before:", before)
    print("Compacted:", len(removals))
    print("Summary:", update["conversation_summary"])
    print("After:", after_count)


if __name__ == "__main__":
    asyncio.run(main())
