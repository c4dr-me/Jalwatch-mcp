"""Deploy-compatible, tool-free summarizer behind the Task 7 interface."""

from collections.abc import Sequence

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from jalwatch.persistence.compaction import SummaryResult, summary_input
from jalwatch.persistence.ollama import SUMMARY_SYSTEM_PROMPT


class GroqSummaryModel:
    def __init__(self, model: ChatGroq) -> None:
        self._model = model
        self.model = str(model.model_name)

    async def summarize(
        self, previous_summary: str | None, messages: Sequence[AnyMessage]
    ) -> SummaryResult:
        response = await self._model.ainvoke(
            [
                SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"Previous summary: {previous_summary or '(none)'}\n\n"
                        f"Selected messages: {summary_input(messages)}"
                    )
                ),
            ]
        )
        content = response.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Summarizer returned no text")
        usage = response.usage_metadata
        return {
            "text": content,
            "input_tokens": usage.get("input_tokens") if usage else None,
            "output_tokens": usage.get("output_tokens") if usage else None,
        }
