"""Schema-only SACHET alert capabilities; no implementation or execution registry."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from jalwatch.domain.models import EscalationSeverity, NonEmptyText

MAX_RECENT_HOURS = 72


class ListActiveCwcAlerts(BaseModel):
    """List currently active CWC-related flood/river alerts from NDMA SACHET.

    Identify CWC using RSS author and CAP provenance/reference evidence;
    CAP sender alone may identify a state disaster-management agency.
    """

    model_config = ConfigDict(title="list_active_cwc_alerts", extra="forbid")

    region: NonEmptyText | None = Field(
        default=None, description="Optional requested Indian region or state."
    )
    severity: NonEmptyText | None = Field(
        default=None,
        description="Optional exact source-provided CAP severity label.",
    )


class GetAlertDetails(BaseModel):
    """Retrieve authoritative CAP details for an alert found in the official feed."""

    model_config = ConfigDict(title="get_alert_details", extra="forbid")

    alert_ref: NonEmptyText = Field(
        description="Opaque discovery reference returned by SACHET alert listing."
    )


class SearchRecentCwcAlerts(BaseModel):
    """Find CWC-related alerts published recently, including expired alerts."""

    model_config = ConfigDict(title="search_recent_cwc_alerts", extra="forbid")

    region: NonEmptyText | None = Field(
        default=None, description="Optional requested Indian region or state."
    )
    hours: Annotated[int, Field(strict=True, ge=1, le=MAX_RECENT_HOURS)] = Field(
        description=(
            "Publication lookback in hours, 1 through 72; feed retention may "
            "limit results."
        )
    )


class ListAffectedRegions(BaseModel):
    """Deterministically list distinct regions in current CWC-related alerts."""

    model_config = ConfigDict(title="list_affected_regions", extra="forbid")


class CreateEscalation(BaseModel):
    """Request a local operational escalation grounded in official CAP alerts.

    Execution requires explicit human approval at a later Human-in-the-Loop
    boundary. This request is not approval or execution. It cannot issue public
    emergency warnings or control reservoirs.
    """

    model_config = ConfigDict(title="create_escalation", extra="forbid")

    alert_refs: tuple[NonEmptyText, ...] = Field(
        min_length=1, description="Official alerts supporting this request."
    )
    region: NonEmptyText = Field(description="Operational region for review.")
    rationale: NonEmptyText = Field(
        description="Concise justification grounded in retrieved alert evidence."
    )
    severity: EscalationSeverity = Field(
        description="Local operational priority, distinct from official CAP severity."
    )


TOOL_CONTRACTS: tuple[type[BaseModel], ...] = (
    ListActiveCwcAlerts,
    GetAlertDetails,
    SearchRecentCwcAlerts,
    ListAffectedRegions,
    CreateEscalation,
)
