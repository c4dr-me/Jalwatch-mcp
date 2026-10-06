"""Validated domain records, independent of prompts and LLM providers."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    HttpUrl,
    StringConstraints,
)

type NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]


class ApprovalStatus(StrEnum):
    """A decision about one proposal; these values do not authorize execution."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class EscalationSeverity(StrEnum):
    """Local operational priority, distinct from official CAP severity."""

    ROUTINE = "routine"
    ELEVATED = "elevated"
    URGENT = "urgent"


class StationObservation(BaseModel):
    """One sourced measurement, used for both current and historical evidence.

    Metric and unit retain the source's terminology. A missing value is None,
    never an invented zero. Official status text is not an agent risk assessment.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: NonEmptyText
    metric: NonEmptyText
    value: FiniteFloat | None
    unit: NonEmptyText
    observed_at: AwareDatetime
    retrieved_at: AwareDatetime
    source_url: HttpUrl
    official_status: NonEmptyText | None = None


class WaterAlert(BaseModel):
    """An official alert as retrieved, with the source's own severity label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: NonEmptyText
    region: NonEmptyText
    severity: NonEmptyText
    message: NonEmptyText
    issued_at: AwareDatetime
    retrieved_at: AwareDatetime
    source_url: HttpUrl
    station_ids: tuple[NonEmptyText, ...] = ()


class EscalationProposal(BaseModel):
    """A reviewable draft and its decision, not an executed escalation.

    A changed draft must be a new pending proposal; transition enforcement and
    human identity/approval handling belong to Task 6.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: NonEmptyText
    alert_refs: tuple[NonEmptyText, ...] = Field(min_length=1)
    region: NonEmptyText
    rationale: NonEmptyText
    severity: EscalationSeverity
    approval_status: ApprovalStatus = ApprovalStatus.PENDING


class PendingEscalationApproval(BaseModel):
    """Minimal checkpointed correlation for one paused consequential call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: NonEmptyText
    tool_call_id: NonEmptyText
    action_digest: NonEmptyText


class EscalationRecord(BaseModel):
    """One executed local operation, distinct from an approval proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    escalation_id: NonEmptyText
    approval_id: NonEmptyText
    alert_refs: tuple[NonEmptyText, ...] = Field(min_length=1)
    region: NonEmptyText
    rationale: NonEmptyText
    severity: EscalationSeverity
    created_at: AwareDatetime
    status: Literal["created"] = "created"


class ToolError(BaseModel):
    """An unresolved recoverable failure retained independently of chat history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: NonEmptyText
    tool_name: NonEmptyText
    code: NonEmptyText
    message: NonEmptyText
