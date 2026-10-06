"""Deterministic SACHET-backed reads and a local protected escalation action."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from jalwatch.agent.tool_contracts import (
    CreateEscalation,
    GetAlertDetails,
    ListActiveCwcAlerts,
    SearchRecentCwcAlerts,
)
from jalwatch.domain.alerts import CapAlert, CapInfo, RssAlertEntry, reference_key
from jalwatch.domain.models import EscalationProposal, EscalationRecord
from jalwatch.sources.sachet import AlertNotFound, SachetClient


def is_active(alert: CapAlert, now: datetime) -> bool:
    """Conservative CAP activity test; chain supersession is handled separately."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if alert.status != "Actual" or alert.msg_type not in ("Alert", "Update"):
        return False
    return any(_info_is_active(info, now) for info in alert.info)


def _info_is_active(info: CapInfo, now: datetime) -> bool:
    return (
        (info.effective is None or info.effective <= now)
        and info.expires is not None
        and info.expires > now
    )


def _info_is_flood_or_river(info: CapInfo) -> bool:
    return info.event is not None and (
        "flood" in info.event.casefold() or "river" in info.event.casefold()
    )


def is_flood_or_river(alert: CapAlert) -> bool:
    return any(_info_is_flood_or_river(info) for info in alert.info)


def _referenced_keys(alert: CapAlert) -> set[str]:
    keys = set()
    for reference in alert.references:
        pieces = reference.split(",", 2)
        if len(pieces) == 3:
            try:
                sent = datetime.fromisoformat(pieces[2].replace("Z", "+00:00"))
            except ValueError:
                continue
            if sent.tzinfo is not None and sent.utcoffset() is not None:
                keys.add(reference_key(pieces[0], pieces[1], sent))
    return keys


def current_alerts(alerts: list[CapAlert], now: datetime) -> list[CapAlert]:
    """Remove referenced versions when their Update/Cancel appears in this feed."""
    superseded = set().union(
        *(
            _referenced_keys(alert)
            for alert in alerts
            if alert.status == "Actual" and alert.msg_type in ("Update", "Cancel")
        )
    )
    return [
        alert
        for alert in alerts
        if alert.is_cwc_related
        and alert.status == "Actual"
        and alert.msg_type in ("Alert", "Update")
        and any(
            _info_is_flood_or_river(info) and _info_is_active(info, now)
            for info in alert.info
        )
        and reference_key(alert.sender, alert.cap_identifier, alert.sent)
        not in superseded
    ]


def _summary(alert: CapAlert) -> dict[str, object]:
    return {
        "alert_ref": alert.alert_ref,
        "cap_identifier": alert.cap_identifier,
        "sender": alert.sender,
        "sent": alert.sent.isoformat(),
        "published_at": alert.published_at.isoformat(),
        "status": alert.status,
        "msg_type": alert.msg_type,
        "source_severities": list(alert.source_severities),
        "normalized_regions": list(alert.normalized_regions),
        "raw_area_descriptions": [
            area for info in alert.info for area in info.raw_area_descriptions
        ],
        "headlines": [info.headline for info in alert.info if info.headline],
        "effective": [
            info.effective.isoformat() if info.effective else None
            for info in alert.info
        ],
        "expires": [
            info.expires.isoformat() if info.expires else None for info in alert.info
        ],
        "source_url": str(alert.source_url),
        "cwc_provenance": list(alert.cwc_provenance),
    }


class SachetTools:
    def __init__(
        self, client: SachetClient, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.client = client
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _all_alerts(self) -> list[CapAlert]:
        entries = await self.client.list_entries()
        semaphore = asyncio.Semaphore(8)

        async def fetch(entry: RssAlertEntry) -> CapAlert:
            async with semaphore:
                return await self.client.get_cap(entry)

        return list(await asyncio.gather(*(fetch(entry) for entry in entries)))

    async def list_active_cwc_alerts(
        self, args: ListActiveCwcAlerts
    ) -> dict[str, object]:
        alerts = current_alerts(await self._all_alerts(), self.clock())
        if args.region:
            alerts = [
                a
                for a in alerts
                if any(
                    r.casefold() == args.region.casefold() for r in a.normalized_regions
                )
            ]
        if args.severity:
            alerts = [a for a in alerts if args.severity in a.source_severities]
        return {
            "alerts": [_summary(a) for a in alerts],
            "source_url": "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml",
        }

    async def get_alert_details(self, args: GetAlertDetails) -> dict[str, object]:
        alert = await self.client.get_by_ref(args.alert_ref)
        if not alert.is_cwc_related:
            raise AlertNotFound(args.alert_ref)
        return {"alert": alert.model_dump(mode="json")}

    async def search_recent_cwc_alerts(
        self, args: SearchRecentCwcAlerts
    ) -> dict[str, object]:
        now = self.clock()
        start = now - timedelta(hours=args.hours)
        alerts = [
            a
            for a in await self._all_alerts()
            if a.is_cwc_related
            and is_flood_or_river(a)
            and start <= a.published_at <= now
        ]
        if args.region:
            alerts = [
                a
                for a in alerts
                if any(
                    r.casefold() == args.region.casefold() for r in a.normalized_regions
                )
            ]
        return {
            "alerts": [_summary(a) for a in alerts],
            "scope": "currently exposed SACHET feed only",
            "start": start.isoformat(),
            "end": now.isoformat(),
        }

    async def list_affected_regions(self) -> dict[str, object]:
        alerts = current_alerts(await self._all_alerts(), self.clock())
        return {
            "regions": sorted(
                {region for alert in alerts for region in alert.normalized_regions}
            )
        }


class InMemoryEscalationStore:
    """Local action store; unrelated to graph checkpoint persistence."""

    def __init__(self) -> None:
        self.records: dict[str, EscalationProposal] = {}
        self.approved_records: dict[str, tuple[CreateEscalation, EscalationRecord]] = {}

    def create(self, args: CreateEscalation) -> EscalationProposal:
        proposal = EscalationProposal(
            proposal_id=str(uuid4()),
            alert_refs=args.alert_refs,
            region=args.region,
            rationale=args.rationale,
            severity=args.severity,
        )
        self.records[proposal.proposal_id] = proposal
        return proposal

    def create_approved(
        self, args: CreateEscalation, *, approval_id: str
    ) -> EscalationRecord:
        """Create once per approval ID; reject reuse with a changed action."""
        previous = self.approved_records.get(approval_id)
        if previous is not None:
            if previous[0] != args:
                raise ValueError("Approval ID was already used for another action")
            return previous[1]
        record = EscalationRecord(
            escalation_id=str(uuid4()),
            approval_id=approval_id,
            alert_refs=args.alert_refs,
            region=args.region,
            rationale=args.rationale,
            severity=args.severity,
            created_at=datetime.now(UTC),
        )
        self.approved_records[approval_id] = (args, record)
        return record
