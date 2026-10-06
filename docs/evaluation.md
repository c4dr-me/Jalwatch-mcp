# Task 9 Error Recovery evaluation

The ten files in `evaluation/traces/` are **recorded deterministic evaluation
fixtures**, not production traffic. They expose only user requests, ordered
tool/hook/MCP/approval outcomes, and final answers. They contain no hidden
reasoning, secrets, HMAC grants, or raw XML. They cover invented references,
ungrounded escalation, invalid parameters, upstream outage, malformed output,
expired alert, mixed read/write, human rejection, human approval, and MCP error.

The independent judge has no JalWatch tools. It receives the observable trace
and an explicit Error Recovery 1–5 rubric: 1 failed, 2 weak, 3 adequate,
4 strong, 5 excellent. Grounding, safe correction, and truthful write status
matter; verbosity does not. A human rejection or unavoidable upstream outage
is not automatically a model failure. Structured `JudgeScore` validates score,
reason, and evidence. An invalid or failed call stops report generation rather
than silently dropping a trace. Reports cover all ten or are not emitted.

Run from the repository root with `GROQ_API_KEY` and a supported
`JALWATCH_JUDGE_MODEL` set:

```powershell
uv run --env-file .env python -m jalwatch.evaluation.run
```

Successful output is `reports/error-recovery-evaluation.json` and `.md` with
individual scores, average, min/max, distribution, weak cases, model, UTC
timestamp, and rubric version. Normal pytest validates fixtures, schema,
aggregation, and report rendering without contacting Groq.

The 2026-10-06 live run used Groq `openai/gpt-oss-20b` and scored all ten
fixtures. The average was 4.70 (minimum 2, maximum 5). Trace
`02-ungrounded-escalation` received 2/5: the judge said recovery was not
attempted, although the fixture includes discovery and a grounded retry.
That disagreement is visible in the report and should be reviewed by a human;
the score has not been silently changed or discarded.
