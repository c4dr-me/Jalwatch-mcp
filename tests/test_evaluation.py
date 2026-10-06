"""No live model or cloud service is contacted by evaluation tests."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from jalwatch.evaluation.models import JudgeScore, ScoredTrace
from jalwatch.evaluation.run import (
    RUBRIC,
    aggregate,
    evaluate,
    judge_messages,
    load_traces,
    write_report,
)


def test_exactly_ten_observable_fixtures() -> None:
    traces = load_traces()
    assert len(traces) == 10
    assert all(trace.origin == "deterministic_fixture" for trace in traces)
    assert all(trace.events and trace.final_answer for trace in traces)
    assert "hidden reasoning" in RUBRIC
    assert "ERROR RECOVERY" in str(judge_messages(traces[0])[0].content)


def test_score_validation_and_report(tmp_path: Path) -> None:
    for bad in (0, 6, "5"):
        with pytest.raises(ValidationError):
            JudgeScore.model_validate({"score": bad, "reason": "x", "evidence": ["e"]})
    with pytest.raises(ValidationError):
        JudgeScore(score=3, reason="", evidence=["e"])
    traces = load_traces()
    scored = [
        ScoredTrace(
            trace_id=trace.trace_id,
            scenario=trace.scenario,
            judgment=JudgeScore(
                score=(index % 5) + 1, reason="Observed", evidence=["event"]
            ),
        )
        for index, trace in enumerate(traces)
    ]
    report = aggregate(scored, "fake-judge")
    assert report.average == 3
    assert report.minimum == 1 and report.maximum == 5
    assert report.distribution == {1: 2, 2: 2, 3: 2, 4: 2, 5: 2}
    write_report(report, tmp_path)
    assert "01-invented-ref" in (tmp_path / "error-recovery-evaluation.md").read_text()
    with pytest.raises(ValueError):
        aggregate(scored[:-1], "fake-judge")


def test_judge_failure_does_not_publish_partial_report(tmp_path: Path) -> None:
    async def failing(_trace: object) -> object:
        raise RuntimeError("judge unavailable")

    with pytest.raises(RuntimeError, match="judge unavailable"):
        asyncio.run(
            evaluate(load_traces(), failing, judge_model="fake", directory=tmp_path)
        )
    assert list(tmp_path.iterdir()) == []
