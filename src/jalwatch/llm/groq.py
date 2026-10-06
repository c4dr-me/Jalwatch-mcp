"""Create the real Groq model only on explicit request."""

import os

from langchain_groq import ChatGroq
from pydantic import SecretStr


def create_groq_model() -> ChatGroq:
    """Read required environment variables; do not load .env or invoke the API."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    model_name = os.environ.get("JALWATCH_LLM_MODEL", "").strip()
    missing = []
    if not api_key:
        missing.append("GROQ_API_KEY")
    if not model_name:
        missing.append("JALWATCH_LLM_MODEL")
    if missing:
        raise ValueError(f"Missing required Groq configuration: {', '.join(missing)}")

    return ChatGroq(
        model_name=model_name,
        groq_api_key=SecretStr(api_key),
        temperature=0,
        request_timeout=30.0,
        max_retries=0,
    )
