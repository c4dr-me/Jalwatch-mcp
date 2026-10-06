"""Sourced SACHET records; CAP fields retain their authoritative wording."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, HttpUrl

from jalwatch.domain.models import NonEmptyText


class RssAlertEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_ref: NonEmptyText
    cap_url: HttpUrl
    author: str
    published_at: AwareDatetime
    title: str
    category: str | None = None


class CapInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str | None = None
    event: str | None = None
    urgency: str | None = None
    source_severity: str | None = None
    certainty: str | None = None
    effective: AwareDatetime | None = None
    onset: AwareDatetime | None = None
    expires: AwareDatetime | None = None
    headline: str | None = None
    description: str | None = None
    raw_area_descriptions: tuple[str, ...] = ()
    normalized_regions: tuple[str, ...] = ()


class CapAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_ref: NonEmptyText
    cap_identifier: NonEmptyText
    sender: NonEmptyText
    sent: AwareDatetime
    status: NonEmptyText
    msg_type: NonEmptyText
    source: str | None = None
    references: tuple[str, ...] = ()
    info: tuple[CapInfo, ...] = ()
    source_url: HttpUrl
    rss_author: str
    published_at: AwareDatetime
    cwc_provenance: tuple[str, ...] = ()

    @property
    def normalized_regions(self) -> tuple[str, ...]:
        return tuple(
            sorted({region for info in self.info for region in info.normalized_regions})
        )

    @property
    def is_cwc_related(self) -> bool:
        return bool(self.cwc_provenance)

    @property
    def source_severities(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                info.source_severity for info in self.info if info.source_severity
            )
        )


def reference_key(sender: str, identifier: str, sent: datetime) -> str:
    """CAP reference identity is sender, identifier, and sent timestamp."""
    return f"{sender},{identifier},{sent.isoformat()}"
