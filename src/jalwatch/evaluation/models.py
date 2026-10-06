"""Typed, inspectable evaluation records without hidden reasoning."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["tool_request", "tool_result", "hook_error", "mcp_error", "approval"]
    tool: str | None = None
    code: str | None = None
    outcome: str


class HistoricalTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str
    scenario: str
    origin: Literal["deterministic_fixture"]
    user_request: str
    events: list[TraceEvent]
    final_answer: str
    expected_recovery: str


class JudgeScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int = Field(ge=1, le=5, strict=True)
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class ScoredTrace(BaseModel):
    trace_id: str
    scenario: str
    judgment: JudgeScore


class EvaluationReport(BaseModel):
    rubric_version: str
    judge_model: str
    evaluated_at: datetime
    scores: list[ScoredTrace]
    average: float
    minimum: int
    maximum: int
    distribution: dict[int, int]
    weak_cases: list[str]
