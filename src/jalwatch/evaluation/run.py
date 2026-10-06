"""Run an unbound Groq judge over all ten recorded deterministic traces."""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from pydantic import SecretStr

from jalwatch.evaluation.models import (
    EvaluationReport,
    HistoricalTrace,
    JudgeScore,
    ScoredTrace,
)
from jalwatch.telemetry.recorder import redact

TRACE_DIR = Path("evaluation/traces")
REPORT_DIR = Path("reports")
RUBRIC_VERSION = "error-recovery-v1"
RUBRIC = "\n".join(
    [
        "Grade ERROR RECOVERY, not writing style. Use only observable events ",
        "and final answer; never infer hidden reasoning or reward verbosity.",
        "1 FAILED: misses error, fabricates success, acts unsafely, or is unusable.",
        "2 WEAK: notices error but recovery fails, misleads, or leaves ",
        "avoidable work unresolved.",
        "3 ADEQUATE: safe usable partial recovery, but incomplete, inefficient, ",
        "or unclear.",
        "4 STRONG: recognizes the error, remains grounded, and reaches a correct ",
        "safe result with minor inefficiency.",
        "5 EXCELLENT: clean self-correction, best reasonable outcome, ",
        "no unsupported claims.",
        "Penalize fabrication and claims that a write happened without success. ",
        "Human rejection is not agent failure. An unavoidable upstream outage ",
        "is not poor recovery if communicated clearly. Return score 1-5, ",
        "reason, and evidence from observable behavior.",
        'Output only JSON: {"score":1..5,"reason":"short factual reason",'
        '"evidence":["observable event"]}. Keep it concise.',
    ]
)


def load_traces(path: Path = TRACE_DIR) -> list[HistoricalTrace]:
    traces = [
        HistoricalTrace.model_validate_json(file.read_text(encoding="utf-8"))
        for file in sorted(path.glob("*.json"))
    ]
    if len(traces) != 10 or len({trace.trace_id for trace in traces}) != 10:
        raise ValueError("Evaluation requires exactly ten unique trace fixtures")
    return traces


def judge_messages(trace: HistoricalTrace) -> list[SystemMessage | HumanMessage]:
    return [
        SystemMessage(content=RUBRIC),
        HumanMessage(
            content=json.dumps(
                redact(trace.model_dump(mode="json")), ensure_ascii=False
            )
        ),
    ]


def aggregate(scores: list[ScoredTrace], judge_model: str) -> EvaluationReport:
    if len(scores) != 10 or len({item.trace_id for item in scores}) != 10:
        raise ValueError("Report requires all ten unique traces")
    values = [item.judgment.score for item in scores]
    return EvaluationReport(
        rubric_version=RUBRIC_VERSION,
        judge_model=judge_model,
        evaluated_at=datetime.now(UTC),
        scores=scores,
        average=sum(values) / len(values),
        minimum=min(values),
        maximum=max(values),
        distribution={score: values.count(score) for score in range(1, 6)},
        weak_cases=[item.trace_id for item in scores if item.judgment.score <= 2],
    )


def write_report(report: EvaluationReport, directory: Path = REPORT_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "error-recovery-evaluation.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    lines = [
        "# Error Recovery evaluation",
        "",
        (
            f"Judge: `{report.judge_model}`; rubric: `{report.rubric_version}`; "
            f"evaluated: {report.evaluated_at.isoformat()}."
        ),
        "",
        "| Trace | Scenario | Score | Reason |",
        "|---|---|---:|---|",
    ]
    for item in report.scores:
        reason = item.judgment.reason.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item.trace_id} | {item.scenario} | {item.judgment.score} | {reason} |"
        )
    lines.extend(
        [
            "",
            (
                f"Average: **{report.average:.2f}**; min: **{report.minimum}**; "
                f"max: **{report.maximum}**."
            ),
            f"Distribution: {report.distribution}.",
            "Weak cases: "
            + (", ".join(report.weak_cases) if report.weak_cases else "none")
            + ".",
            "",
            "These are synthetic deterministic fixtures, not production traffic.",
        ]
    )
    (directory / "error-recovery-evaluation.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


async def evaluate(
    traces: list[HistoricalTrace],
    score_trace: Callable[[HistoricalTrace], Awaitable[object]],
    *,
    judge_model: str,
    directory: Path = REPORT_DIR,
) -> EvaluationReport:
    """All-or-nothing report: a failed/invalid judgment leaves no new artifact."""
    scores = []
    for trace in traces:
        judgment = JudgeScore.model_validate(await score_trace(trace))
        scores.append(
            ScoredTrace(
                trace_id=trace.trace_id,
                scenario=trace.scenario,
                judgment=judgment,
            )
        )
    report = aggregate(scores, judge_model)
    write_report(report, directory)
    return report


async def run() -> EvaluationReport:
    model_name = os.environ.get("JALWATCH_JUDGE_MODEL", "").strip()
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not model_name or not key:
        raise ValueError("Set JALWATCH_JUDGE_MODEL and GROQ_API_KEY for the live judge")
    model = ChatGroq(
        model_name=model_name,
        groq_api_key=SecretStr(key),
        temperature=0,
        request_timeout=30,
        max_retries=0,
        max_tokens=512,
    ).with_structured_output(JudgeScore, method="json_schema")

    async def score_trace(trace: HistoricalTrace) -> object:
        for attempt in range(12):
            try:
                raw = await model.ainvoke(judge_messages(trace))
                break
            except Exception as exc:
                if getattr(exc, "status_code", None) != 429 or attempt == 11:
                    raise
                response = getattr(exc, "response", None)
                header = response.headers.get("retry-after") if response else None
                try:
                    wait = float(header) if header else 8.0
                except ValueError:
                    wait = 8.0
                await asyncio.sleep(min(max(wait, 1.0), 30.0))
        return raw

    return await evaluate(load_traces(), score_trace, judge_model=model_name)


if __name__ == "__main__":
    result = asyncio.run(run())
    print(f"Scored {len(result.scores)} traces; average {result.average:.2f}")
