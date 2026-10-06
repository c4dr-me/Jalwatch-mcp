"""Groq configuration and binding checks without any network invocation."""

import os
import subprocess
import sys

import pytest
from langchain_core.runnables import RunnableBinding

from jalwatch.agent.reasoning import create_reasoning_node
from jalwatch.agent.tool_contracts import TOOL_CONTRACTS
from jalwatch.llm.groq import create_groq_model


@pytest.mark.parametrize(
    "key,model,missing",
    [
        ("", "test-model", "GROQ_API_KEY"),
        ("unit-test-placeholder", "", "JALWATCH_LLM_MODEL"),
        ("  ", "  ", "GROQ_API_KEY, JALWATCH_LLM_MODEL"),
    ],
)
def test_missing_configuration_is_clear(
    monkeypatch: pytest.MonkeyPatch, key: str, model: str, missing: str
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", key)
    monkeypatch.setenv("JALWATCH_LLM_MODEL", model)
    with pytest.raises(ValueError, match=missing):
        create_groq_model()


def test_real_model_construction_and_binding_are_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("JALWATCH_LLM_MODEL", "test-model-placeholder")
    model = create_groq_model()
    assert model.model_name == "test-model-placeholder"
    assert model.groq_api_key is not None
    assert "unit-test-placeholder" not in repr(model.groq_api_key)

    bound = model.bind_tools(TOOL_CONTRACTS, tool_choice="auto")
    assert isinstance(bound, RunnableBinding)
    assert bound.kwargs["tool_choice"] == "auto"
    assert len(bound.kwargs["tools"]) == 5
    # Construction binds schemas only. Do not invoke the real node in tests.
    assert callable(create_reasoning_node(model))


def test_modules_import_without_credentials() -> None:
    environment = os.environ.copy()
    environment.pop("GROQ_API_KEY", None)
    environment.pop("JALWATCH_LLM_MODEL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import jalwatch.agent.reasoning; import jalwatch.llm.groq; "
                "import jalwatch.llm.smoke; import jalwatch.protocol.jsonrpc"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
