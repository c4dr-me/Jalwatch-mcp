"""Short-lived HMAC grant carried only in trusted MCP request metadata."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from jalwatch.domain.models import NonEmptyText

APPROVAL_META_KEY = "com.jalwatch/approval"
APPROVAL_SECRET_ENV = "JALWATCH_MCP_APPROVAL_SECRET"
GRANT_LIFETIME = timedelta(seconds=60)


class ApprovalGrant(BaseModel):
    """Public correlation plus a signature; never includes the secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: NonEmptyText
    thread_id: NonEmptyText
    tool_call_id: NonEmptyText
    expires_at: AwareDatetime
    signature: NonEmptyText


def _canonical_payload(
    approval_id: str,
    thread_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict[str, Any],
    expires_at: datetime,
) -> bytes:
    return json.dumps(
        {
            "approval_id": approval_id,
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,
            "tool_name": name,
            "arguments": arguments,
            "expires_at": expires_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def action_digest(arguments: dict[str, Any]) -> str:
    """Non-secret checksum tying the paused proposal to the reviewed action."""
    payload = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def issue_grant(
    secret: bytes,
    *,
    approval_id: str,
    thread_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
) -> ApprovalGrant:
    issued = now or datetime.now(UTC)
    expires_at = issued + GRANT_LIFETIME
    signature = hmac.new(
        secret,
        _canonical_payload(
            approval_id, thread_id, tool_call_id, name, arguments, expires_at
        ),
        hashlib.sha256,
    ).hexdigest()
    return ApprovalGrant(
        approval_id=approval_id,
        thread_id=thread_id,
        tool_call_id=tool_call_id,
        expires_at=expires_at,
        signature=signature,
    )


def verify_grant(
    secret: bytes | None,
    metadata: object,
    *,
    name: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
) -> ApprovalGrant | None:
    if secret is None:
        return None
    try:
        grant = ApprovalGrant.model_validate(metadata)
        if grant.expires_at <= (now or datetime.now(UTC)):
            return None
        expected = hmac.new(
            secret,
            _canonical_payload(
                grant.approval_id,
                grant.thread_id,
                grant.tool_call_id,
                name,
                arguments,
                grant.expires_at,
            ),
            hashlib.sha256,
        ).hexdigest()
    except (ValidationError, TypeError, ValueError, OverflowError):
        return None
    return grant if hmac.compare_digest(expected, grant.signature) else None
