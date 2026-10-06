"""Tool contracts validate inputs but have no executable implementations."""

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from jalwatch.agent.tool_contracts import (
    MAX_RECENT_HOURS,
    TOOL_CONTRACTS,
    CreateEscalation,
    GetAlertDetails,
    ListActiveCwcAlerts,
    ListAffectedRegions,
    SearchRecentCwcAlerts,
)
from jalwatch.domain.models import EscalationSeverity


def test_schemas_convert_to_exact_tool_names() -> None:
    schemas = [convert_to_openai_tool(contract) for contract in TOOL_CONTRACTS]
    assert [schema["function"]["name"] for schema in schemas] == [
        "list_active_cwc_alerts",
        "get_alert_details",
        "search_recent_cwc_alerts",
        "list_affected_regions",
        "create_escalation",
    ]
    escalation = schemas[-1]["function"]
    assert "human approval" in escalation["description"]
    assert set(escalation["parameters"]["required"]) == {
        "alert_refs",
        "region",
        "rationale",
        "severity",
    }
    assert (
        not {
            "human_approved",
            "approved_by",
            "authorization_token",
            "approval_status",
        }
        & escalation["parameters"]["properties"].keys()
    )


def test_active_alert_listing_accepts_optional_filters() -> None:
    assert ListActiveCwcAlerts().model_dump() == {"region": None, "severity": None}
    filtered = ListActiveCwcAlerts(region="Bihar", severity="Severe")
    assert filtered.region == "Bihar"
    assert filtered.severity == "Severe"


def test_alert_details_requires_a_discovered_alert_ref() -> None:
    assert GetAlertDetails(alert_ref="1791259767743027").alert_ref == "1791259767743027"
    with pytest.raises(ValidationError, match="alert_ref"):
        GetAlertDetails(alert_ref=" ")


@pytest.mark.parametrize("hours", [0, -1, MAX_RECENT_HOURS + 1, True, 1.5, "2"])
def test_recent_search_rejects_invalid_hours(hours: object) -> None:
    with pytest.raises(ValidationError, match="hours"):
        SearchRecentCwcAlerts.model_validate({"hours": hours})


@pytest.mark.parametrize("hours", [1, MAX_RECENT_HOURS])
def test_recent_search_accepts_bounds(hours: int) -> None:
    search = SearchRecentCwcAlerts(region="Bihar", hours=hours)
    assert search.hours == hours
    assert search.region == "Bihar"


def test_affected_region_discovery_has_no_inputs() -> None:
    assert ListAffectedRegions().model_dump() == {}
    with pytest.raises(ValidationError, match="region"):
        ListAffectedRegions.model_validate({"region": "Bihar"})


@pytest.mark.parametrize("field", ["region", "severity"])
def test_alert_filters_reject_blank_values(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        ListActiveCwcAlerts.model_validate({field: " "})


def test_escalation_requires_alert_evidence_and_rejects_self_approval() -> None:
    payload = {
        "alert_refs": ["1791259767743027"],
        "region": "Jharkhand",
        "rationale": "Fictional evidence for a test only",
        "severity": "elevated",
    }
    escalation = CreateEscalation.model_validate(payload)
    assert escalation.alert_refs == ("1791259767743027",)
    assert escalation.severity is EscalationSeverity.ELEVATED
    with pytest.raises(ValidationError, match="alert_refs"):
        CreateEscalation.model_validate({**payload, "alert_refs": []})
    for field in ("human_approved", "approved_by", "authorization_token"):
        with pytest.raises(ValidationError, match=field):
            CreateEscalation.model_validate({**payload, field: "claimed"})
    with pytest.raises(ValidationError, match="severity"):
        CreateEscalation.model_validate({**payload, "severity": "Severe"})
