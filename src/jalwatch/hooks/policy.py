"""Transport-independent lifecycle policy for JalWatch tool calls."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from langchain_core.messages import BaseMessage, ToolMessage
from pydantic import ValidationError

from jalwatch.domain.models import EscalationRecord


@dataclass(frozen=True)
class HookError:
    status: Literal[400, 502]
    code: str
    message: str
    details: dict[str, object]

    def payload(self) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "status": self.status,
                    "code": self.code,
                    "message": self.message,
                    "details": self.details,
                },
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class HookDecision:
    error: HookError | None = None

    @property
    def allowed(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class ToolContext:
    tool_name: str
    arguments: Mapping[str, Any]
    tool_call_id: str
    messages: Sequence[BaseMessage]
    now: datetime
    approval_id: str | None = None


def _object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value: object, *, nonempty: bool = False) -> list[str] | None:
    if not isinstance(value, list) or (nonempty and not value):
        return None
    if not all(_text(item) for item in value):
        return None
    return value


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    )


def observed_alerts(messages: Sequence[BaseMessage]) -> dict[str, set[str]]:
    """Extract only explicit refs from successful, named JalWatch tool messages."""
    found: dict[str, set[str]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage) or message.status != "success":
            continue
        if message.name not in (
            "list_active_cwc_alerts",
            "search_recent_cwc_alerts",
            "get_alert_details",
        ) or not isinstance(message.content, str):
            continue
        try:
            body = _object(json.loads(message.content))
        except (ValueError, TypeError):
            continue
        if body is None:
            continue
        if message.name == "get_alert_details":
            record = _object(body.get("alert"))
            records = [record] if record is not None else []
        else:
            items = body.get("alerts")
            records = (
                [record for item in items if (record := _object(item)) is not None]
                if isinstance(items, list)
                else []
            )
        for record in records:
            if record is None or not _text(record.get("alert_ref")):
                continue
            ref = record["alert_ref"]
            regions = record.get("normalized_regions")
            if regions is None and message.name == "get_alert_details":
                infos = record.get("info")
                if isinstance(infos, list):
                    regions = [
                        region
                        for info in infos
                        if isinstance(info, dict)
                        for region in (info.get("normalized_regions") or [])
                    ]
            found.setdefault(ref, set()).update(_strings(regions) or [])
    return found


def _reject(
    status: Literal[400, 502], code: str, message: str, **details: object
) -> HookDecision:
    return HookDecision(HookError(status, code, message, details))


def pre_tool_hook(context: ToolContext) -> HookDecision:
    """Check contextual evidence; MCP schema validation handles shape and types."""
    evidence = observed_alerts(context.messages)
    if context.tool_name == "get_alert_details":
        ref = context.arguments.get("alert_ref")
        if not isinstance(ref, str) or not ref.strip():
            return HookDecision()  # MCP validates argument shape.
        if ref not in evidence:
            return _reject(
                400,
                "UNKNOWN_ALERT_REF",
                "Unknown alert_ref. Use an alert discovery tool first.",
                alert_ref=ref,
            )
    if context.tool_name == "create_escalation":
        refs = context.arguments.get("alert_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
        ):
            return HookDecision()  # MCP validates malformed argument shape.
        unknown = [ref for ref in refs if ref not in evidence]
        if unknown:
            return _reject(
                400,
                "UNGROUNDED_ESCALATION_EVIDENCE",
                "Escalation references must first appear in successful alert results.",
                unknown_alert_refs=unknown,
            )
        proposed = context.arguments.get("region")
        supported = {
            region.casefold() for ref in refs for region in evidence.get(ref, set())
        }
        if not supported:
            return _reject(
                400,
                "REGION_EVIDENCE_MISMATCH",
                "No normalized region evidence supports this escalation.",
                region=proposed,
            )
        if not isinstance(proposed, str) or proposed.casefold() not in supported:
            return _reject(
                400,
                "REGION_EVIDENCE_MISMATCH",
                "The proposed region does not match referenced alert evidence.",
                region=proposed,
            )
    return HookDecision()


def _identity(record: dict[str, Any]) -> bool:
    return (
        _text(record.get("alert_ref"))
        and _text(record.get("cap_identifier"))
        and _text(record.get("sender"))
        and _text(record.get("status"))
        and _text(record.get("msg_type"))
        and _text(record.get("source_url"))
        and _strings(record.get("cwc_provenance"), nonempty=True) is not None
        and _time(record.get("sent")) is not None
        and _time(record.get("published_at")) is not None
    )


def _summary(record: dict[str, Any]) -> bool:
    return (
        _identity(record)
        and _strings(record.get("source_severities"), nonempty=True) is not None
        and _strings(record.get("normalized_regions")) is not None
    )


def _active(record: dict[str, Any], now: datetime) -> bool:
    if record.get("status") != "Actual" or record.get("msg_type") not in (
        "Alert",
        "Update",
    ):
        return False
    effective = record.get("effective")
    expires = record.get("expires")
    if not isinstance(effective, list) or not isinstance(expires, list):
        return False
    if not effective or len(effective) != len(expires):
        return False
    has_active_info = False
    for start, end in zip(effective, expires, strict=True):
        started = _time(start) if start is not None else None
        if start is not None and started is None:
            return False
        finish = _time(end)
        if end is not None and finish is None:
            return False
        if finish is not None and finish > now and (started is None or started <= now):
            has_active_info = True
    return has_active_info


def post_tool_hook(context: ToolContext, result: object) -> HookDecision:
    """Validate successful structured results; never repair or reinterpret them."""
    body = _object(result)
    if context.tool_name == "create_escalation":
        try:
            record = EscalationRecord.model_validate(body)
        except ValidationError:
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="invalid_escalation_record",
            )
        if (
            list(record.alert_refs) != context.arguments.get("alert_refs")
            or record.region != context.arguments.get("region")
            or record.rationale != context.arguments.get("rationale")
            or record.severity.value != context.arguments.get("severity")
            or (
                context.approval_id is not None
                and record.approval_id != context.approval_id
            )
        ):
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="escalation_action_mismatch",
            )
        return HookDecision()
    if context.tool_name not in (
        "list_active_cwc_alerts",
        "get_alert_details",
        "search_recent_cwc_alerts",
        "list_affected_regions",
    ):
        return HookDecision()
    if body is None:
        return _reject(
            502,
            "INVALID_TOOL_RESULT",
            "Tool output failed JalWatch post-execution validation.",
            reason="missing_structured_object",
        )

    if context.tool_name == "list_affected_regions":
        regions = _strings(body.get("regions"))
        if regions is None or regions != sorted(set(regions)):
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="regions_not_unique_sorted_strings",
            )
        return HookDecision()

    if context.tool_name == "get_alert_details":
        alert = _object(body.get("alert"))
        if (
            alert is None
            or not _identity(alert)
            or alert.get("alert_ref") != context.arguments.get("alert_ref")
        ):
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="alert_identity_or_provenance",
            )
        infos = alert.get("info")
        if not isinstance(infos, list) or not infos:
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="missing_cap_info",
            )
        for item in infos:
            info = _object(item)
            if (
                info is None
                or _strings(info.get("raw_area_descriptions")) is None
                or _strings(info.get("normalized_regions")) is None
            ):
                return _reject(
                    502,
                    "INVALID_TOOL_RESULT",
                    "Tool output failed JalWatch post-execution validation.",
                    reason="invalid_cap_area",
                )
            for key in ("effective", "onset", "expires"):
                if info.get(key) is not None and _time(info[key]) is None:
                    return _reject(
                        502,
                        "INVALID_TOOL_RESULT",
                        "Tool output failed JalWatch post-execution validation.",
                        reason="invalid_cap_time",
                    )
        if not any(
            _text(info.get("source_severity"))
            for info in infos
            if isinstance(info, dict)
        ):
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="missing_source_severity",
            )
        return HookDecision()

    alerts = body.get("alerts")
    if not isinstance(alerts, list):
        return _reject(
            502,
            "INVALID_TOOL_RESULT",
            "Tool output failed JalWatch post-execution validation.",
            reason="missing_alerts_list",
        )
    for item in alerts:
        alert = _object(item)
        if alert is None or not _summary(alert):
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="invalid_alert_summary",
            )
        if context.tool_name == "list_active_cwc_alerts" and not _active(
            alert, context.now
        ):
            return _reject(
                502,
                "INVALID_TOOL_RESULT",
                "Tool output failed JalWatch post-execution validation.",
                reason="inactive_alert_in_active_result",
            )
        if context.tool_name == "search_recent_cwc_alerts":
            hours = context.arguments.get("hours")
            published = _time(alert.get("published_at"))
            if (
                not isinstance(hours, int)
                or published is None
                or not (
                    context.now - timedelta(hours=hours) <= published <= context.now
                )
            ):
                return _reject(
                    502,
                    "INVALID_TOOL_RESULT",
                    "Tool output failed JalWatch post-execution validation.",
                    reason="outside_requested_window",
                )
    return HookDecision()
