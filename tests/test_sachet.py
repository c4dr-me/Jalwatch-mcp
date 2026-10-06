"""Offline SACHET parsing, CAP activity, ETag, and source-backed tool tests."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

from jalwatch.agent.tool_contracts import (
    GetAlertDetails,
    ListActiveCwcAlerts,
    SearchRecentCwcAlerts,
)
from jalwatch.sources.sachet import (
    AlertNotFound,
    SachetClient,
    SourceError,
    normalize_regions,
    parse_cap,
    parse_rss,
)
from jalwatch.tools.sachet_tools import SachetTools, current_alerts, is_active

FIXTURES = Path(__file__).parent / "fixtures"
RSS = (FIXTURES / "sachet_rss.xml").read_bytes()
CAP = (FIXTURES / "sachet_cap.xml").read_bytes()
NOW = datetime(2026, 10, 6, 9, tzinfo=UTC)


def cap_for(ref: str) -> bytes:
    if ref == "1001":
        return CAP
    if ref == "1002":
        return (
            CAP.replace(b"IN-1001_27", b"IN-1002_1")
            .replace(b"Jharkhand-SDMA", b"Bihar-SDMA")
            .replace(b"Jharkhand", b"Bihar")
            .replace(b"Severe", b"Moderate")
            .replace(b"IN-1001_5", b"IN-1002_0")
        )
    return (
        CAP.replace(b"IN-1001_27", b"IN-1003_1")
        .replace(
            b"<cap:references>CWC,IN-1001_5,2026-10-06T09:38:58+05:30</cap:references>",
            b"",
        )
        .replace(
            b"<cap:event>Flood</cap:event>", b"<cap:event>Thunderstorm</cap:event>"
        )
    )


def make_client() -> tuple[SachetClient, httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("rss_india.xml"):
            return httpx.Response(200, content=RSS)
        ref = request.url.params.get("identifier", "")
        if request.headers.get("If-None-Match") == '"etag-one"':
            return httpx.Response(304)
        return httpx.Response(200, content=cap_for(ref), headers={"ETag": '"etag-one"'})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SachetClient(http), http, requests


def test_rss_and_cap_keep_distinct_ids_all_info_and_raw_areas() -> None:
    entries = parse_rss(RSS)
    assert len(entries) == 3
    assert entries[0].alert_ref == "1001"
    assert entries[0].published_at.tzinfo is not None
    alert = parse_cap(CAP, entries[0])
    assert alert.alert_ref == "1001"
    assert alert.cap_identifier == "IN-1001_27"
    assert alert.sender == "Jharkhand-SDMA"
    assert alert.cwc_provenance == ("rss_author:CWC", "cap_references:CWC")
    assert len(alert.info) == 2
    assert len(alert.info[0].raw_area_descriptions) == 2
    assert (
        alert.info[0].raw_area_descriptions[0]
        == "Ganga, Sahibganj, Sahibganj, Jharkhand"
    )
    assert alert.normalized_regions == ("Jharkhand",)
    assert alert.sent.tzinfo is not None
    assert alert.info[0].expires is not None
    assert alert.info[0].expires.tzinfo is not None
    assert normalize_regions("Unknown district without state") == ()
    assert not parse_cap(cap_for("1003"), entries[2]).is_cwc_related


def test_bad_xml_or_untrusted_cap_url_is_rejected() -> None:
    with pytest.raises(SourceError, match="unsafe XML"):
        parse_rss(b'<!DOCTYPE rss [<!ENTITY x "oops">]><rss>&x;</rss>')
    changed = RSS.replace(b"sachet.ndma.gov.in", b"example.invalid")
    with pytest.raises(SourceError, match="official matching reference"):
        parse_rss(changed)
    missing_guid = RSS.replace(b"<guid>1001</guid>", b"", 1)
    with pytest.raises(SourceError, match="lacks guid"):
        parse_rss(missing_guid)


def test_cap_reference_can_establish_cwc_provenance_without_cwc_rss_author() -> None:
    non_cwc_entry = parse_rss(RSS)[2]
    alert = parse_cap(CAP, non_cwc_entry)
    assert alert.is_cwc_related
    assert alert.cwc_provenance == ("cap_references:CWC",)


def test_cap_active_expiry_future_cancel_test_and_update_chain() -> None:
    alert = parse_cap(CAP, parse_rss(RSS)[0])
    assert is_active(alert, NOW)
    assert not is_active(alert, NOW + timedelta(days=2))
    info = alert.info[0].model_copy(update={"effective": NOW + timedelta(hours=1)})
    assert not is_active(alert.model_copy(update={"info": (info,)}), NOW)
    for status in ("Test", "Exercise", "Draft"):
        assert not is_active(alert.model_copy(update={"status": status}), NOW)
    assert not is_active(alert.model_copy(update={"msg_type": "Cancel"}), NOW)
    no_expiry = alert.info[0].model_copy(update={"expires": None})
    assert not is_active(alert.model_copy(update={"info": (no_expiry,)}), NOW)
    unrelated_active = alert.info[1].model_copy(update={"event": "Thunderstorm"})
    expired_flood = alert.info[0].model_copy(
        update={"expires": NOW - timedelta(hours=1)}
    )
    mixed = alert.model_copy(update={"info": (expired_flood, unrelated_active)})
    assert is_active(mixed, NOW)
    assert current_alerts([mixed], NOW) == []
    old = alert.model_copy(
        update={
            "sender": "CWC",
            "cap_identifier": "IN-1001_5",
            "sent": datetime.fromisoformat("2026-10-06T09:38:58+05:30"),
            "msg_type": "Alert",
            "references": (),
        }
    )
    assert is_active(old, NOW)
    assert current_alerts([old, alert], NOW) == [alert]
    cancel = alert.model_copy(
        update={
            "cap_identifier": "IN-1001_28",
            "msg_type": "Cancel",
            "references": ("Jharkhand-SDMA,IN-1001_27,2026-10-06T13:25:00+05:30",),
        }
    )
    assert current_alerts([old, alert, cancel], NOW) == []


def test_etag_cache_and_unknown_reference() -> None:
    async def check() -> None:
        client, http, requests = make_client()
        try:
            entry = (await client.list_entries())[0]
            first = await client.get_cap(entry)
            second = await client.get_cap(entry)
            assert first == second
            cap_requests = [r for r in requests if "FetchXMLFile" in r.url.path]
            assert len(cap_requests) == 2
            assert cap_requests[0].headers.get("If-None-Match") is None
            assert cap_requests[1].headers["If-None-Match"] == '"etag-one"'
            with pytest.raises(AlertNotFound):
                await client.get_by_ref("unknown")
        finally:
            await http.aclose()

    asyncio.run(check())


def test_read_tools_filter_and_sort_without_network() -> None:
    async def check() -> None:
        client, http, _ = make_client()
        tools = SachetTools(client, clock=lambda: NOW)
        try:
            all_active = await tools.list_active_cwc_alerts(ListActiveCwcAlerts())
            assert len(cast(list[object], all_active["alerts"])) == 2
            bihar = await tools.list_active_cwc_alerts(
                ListActiveCwcAlerts(region="Bihar")
            )
            assert len(cast(list[object], bihar["alerts"])) == 1
            severe = await tools.list_active_cwc_alerts(
                ListActiveCwcAlerts(severity="Severe")
            )
            assert len(cast(list[object], severe["alerts"])) == 1
            detail = await tools.get_alert_details(GetAlertDetails(alert_ref="1001"))
            assert (
                cast(dict[str, object], detail["alert"])["cap_identifier"]
                == "IN-1001_27"
            )
            recent = await tools.search_recent_cwc_alerts(
                SearchRecentCwcAlerts(hours=2)
            )
            assert len(cast(list[object], recent["alerts"])) == 2
            short = await tools.search_recent_cwc_alerts(SearchRecentCwcAlerts(hours=1))
            assert len(cast(list[object], short["alerts"])) == 1
            assert (await tools.list_affected_regions())["regions"] == [
                "Bihar",
                "Jharkhand",
            ]
        finally:
            await http.aclose()

    asyncio.run(check())
