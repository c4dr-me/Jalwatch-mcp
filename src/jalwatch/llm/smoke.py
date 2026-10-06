"""Optional live Groq checks. Run explicitly with python -m jalwatch.llm.smoke."""

import argparse
import asyncio
import json

from jalwatch.agent.reasoning import create_reasoning_node
from jalwatch.agent.state import create_initial_state
from jalwatch.llm.groq import create_groq_model
from jalwatch.protocol.jsonrpc import tool_calls_to_requests


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("direct", "tool", "both"), default="both")
    args = parser.parse_args()
    node = create_reasoning_node(create_groq_model())
    questions = {
        "direct": "What kinds of CWC flood-alert questions can you help with?",
        "tool": "What active CWC flood alerts are there in Bihar?",
    }
    for case, question in questions.items():
        if args.case not in (case, "both"):
            continue
        response = (await node(create_initial_state(question)))["messages"][0]
        requests = tool_calls_to_requests(response)
        if case == "direct":
            if requests or not response.text.strip():
                raise RuntimeError(
                    "Direct-answer check expected text without tool calls"
                )
            print(f"Direct answer:\n{response.text}")
        else:
            if not requests:
                raise RuntimeError(
                    "Data question expected a structured tool call. "
                    "No alert result is available; check the model's tool support."
                )
            # No additional_kwargs/reasoning metadata or ungrounded alert text.
            print("Requested tools only; no tools executed:")
            print(
                json.dumps(
                    [request.model_dump(mode="json") for request in requests], indent=2
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
