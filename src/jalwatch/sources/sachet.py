"""Official NDMA SACHET RSS/CAP transport, safe parsing, and normalization."""

import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from pydantic import HttpUrl, ValidationError

from jalwatch.domain.alerts import CapAlert, CapInfo, RssAlertEntry

RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
CAP_PATH = "/cap_public_website/FetchXMLFile"
CAP_NS = "{urn:oasis:names:tc:emergency:cap:1.2}"
MAX_XML_BYTES = 2_000_000
STATE_NAMES = (
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands",
    "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh",
    "Lakshadweep",
    "Puducherry",
)


class SourceError(Exception):
    """Safe, structured upstream failure for tool execution."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AlertNotFound(SourceError):
    def __init__(self, alert_ref: str) -> None:
        super().__init__("ALERT_NOT_FOUND", f"Alert reference not found: {alert_ref}")


def normalize_regions(area_description: str) -> tuple[str, ...]:
    """Match explicit state/UT names only; never infer a state from a district."""
    matches = {
        name
        for name in STATE_NAMES
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", area_description, re.I)
    }
    return tuple(sorted(matches))


def _timestamp(value: str | None, field: str) -> datetime:
    if not value:
        raise SourceError("SOURCE_SCHEMA", f"Missing {field} timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError("SOURCE_SCHEMA", f"Invalid {field} timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SourceError("SOURCE_SCHEMA", f"Timezone missing from {field}")
    return result


def _xml(data: bytes) -> Element:
    if not data or len(data) > MAX_XML_BYTES:
        raise SourceError("SOURCE_SCHEMA", "Empty or oversized XML document")
    try:
        return fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise SourceError("SOURCE_SCHEMA", "Malformed or unsafe XML document") from exc


def _text(element: Element, path: str) -> str | None:
    found = element.find(path)
    return found.text.strip() if found is not None and found.text else None


def parse_rss(data: bytes) -> list[RssAlertEntry]:
    root = _xml(data)
    if root.tag != "rss":
        raise SourceError("SOURCE_SCHEMA", "Expected RSS root")
    entries: list[RssAlertEntry] = []
    for item in root.findall("./channel/item"):
        alert_ref = _text(item, "guid")
        link = _text(item, "link")
        pub_date = _text(item, "pubDate")
        if not alert_ref or not link or not pub_date:
            raise SourceError("SOURCE_SCHEMA", "RSS item lacks guid, link, or pubDate")
        parsed = urlparse(link)
        identifiers = parse_qs(parsed.query).get("identifier", [])
        if (
            parsed.scheme != "https"
            or parsed.hostname != "sachet.ndma.gov.in"
            or parsed.path != CAP_PATH
            or identifiers != [alert_ref]
        ):
            raise SourceError(
                "SOURCE_SCHEMA", "RSS CAP link is not an official matching reference"
            )
        try:
            published_at = parsedate_to_datetime(pub_date)
        except (TypeError, ValueError) as exc:
            raise SourceError("SOURCE_SCHEMA", "Invalid RSS pubDate") from exc
        if published_at.tzinfo is None:
            raise SourceError("SOURCE_SCHEMA", "RSS pubDate lacks timezone")
        try:
            entries.append(
                RssAlertEntry(
                    alert_ref=alert_ref,
                    cap_url=HttpUrl(link),
                    author=_text(item, "author") or "",
                    published_at=published_at,
                    title=_text(item, "title") or "",
                    category=_text(item, "category"),
                )
            )
        except ValidationError as exc:
            raise SourceError("SOURCE_SCHEMA", "Invalid RSS item fields") from exc
    return entries


def parse_cap(data: bytes, entry: RssAlertEntry) -> CapAlert:
    root = _xml(data)
    if root.tag != CAP_NS + "alert":
        raise SourceError("SOURCE_SCHEMA", "Expected CAP 1.2 alert root")
    identifier = _text(root, CAP_NS + "identifier")
    sender = _text(root, CAP_NS + "sender")
    status = _text(root, CAP_NS + "status")
    msg_type = _text(root, CAP_NS + "msgType")
    if not identifier or not sender or not status or not msg_type:
        raise SourceError("SOURCE_SCHEMA", "CAP missing required alert fields")
    infos: list[CapInfo] = []
    for info in root.findall(CAP_NS + "info"):
        areas = tuple(
            filter(
                None,
                (
                    _text(area, CAP_NS + "areaDesc")
                    for area in info.findall(CAP_NS + "area")
                ),
            )
        )
        infos.append(
            CapInfo(
                language=_text(info, CAP_NS + "language"),
                event=_text(info, CAP_NS + "event"),
                urgency=_text(info, CAP_NS + "urgency"),
                source_severity=_text(info, CAP_NS + "severity"),
                certainty=_text(info, CAP_NS + "certainty"),
                effective=_optional_timestamp(
                    _text(info, CAP_NS + "effective"), "effective"
                ),
                onset=_optional_timestamp(_text(info, CAP_NS + "onset"), "onset"),
                expires=_optional_timestamp(_text(info, CAP_NS + "expires"), "expires"),
                headline=_text(info, CAP_NS + "headline"),
                description=_text(info, CAP_NS + "description"),
                raw_area_descriptions=areas,
                normalized_regions=tuple(
                    sorted(
                        {region for area in areas for region in normalize_regions(area)}
                    )
                ),
            )
        )
    references = tuple((_text(root, CAP_NS + "references") or "").split())
    source = _text(root, CAP_NS + "source")
    provenance = []
    if re.search(r"\(CWC\)\s*$", entry.author, re.I):
        provenance.append("rss_author:CWC")
    if sender.casefold() == "cwc":
        provenance.append("cap_sender:CWC")
    if source and source.casefold() == "cwc":
        provenance.append("cap_source:CWC")
    if any(
        len(parts) == 3 and parts[0].casefold() == "cwc"
        for reference in references
        if (parts := reference.split(",", 2))
    ):
        provenance.append("cap_references:CWC")
    try:
        return CapAlert(
            alert_ref=entry.alert_ref,
            cap_identifier=identifier,
            sender=sender,
            sent=_timestamp(_text(root, CAP_NS + "sent"), "sent"),
            status=status,
            msg_type=msg_type,
            source=source,
            references=references,
            info=tuple(infos),
            source_url=entry.cap_url,
            rss_author=entry.author,
            published_at=entry.published_at,
            cwc_provenance=tuple(provenance),
        )
    except ValidationError as exc:
        raise SourceError("SOURCE_SCHEMA", "Invalid CAP alert fields") from exc


def _optional_timestamp(value: str | None, field: str) -> datetime | None:
    return _timestamp(value, field) if value is not None else None


class SachetClient:
    """Small async client with in-process CAP ETag/content caching."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "JalWatch-India/0.1 (academic decision-support)"},
        )
        self._cap_cache: dict[str, tuple[str | None, bytes]] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        try:
            response = await self._http.get(url, headers=headers, timeout=10.0)
        except httpx.RequestError as exc:
            raise SourceError(
                "UPSTREAM_UNAVAILABLE", "SACHET request failed or timed out"
            ) from exc
        if response.status_code not in (200, 304):
            raise SourceError(
                "UPSTREAM_UNAVAILABLE", f"SACHET returned HTTP {response.status_code}"
            )
        return response

    async def list_entries(self) -> list[RssAlertEntry]:
        response = await self._get(RSS_URL)
        if response.status_code != 200:
            raise SourceError("UPSTREAM_UNAVAILABLE", "Unexpected RSS response")
        return parse_rss(response.content)

    async def get_cap(self, entry: RssAlertEntry) -> CapAlert:
        url = str(entry.cap_url)
        cached = self._cap_cache.get(url)
        headers = {"If-None-Match": cached[0]} if cached and cached[0] else None
        response = await self._get(url, headers)
        if response.status_code == 304:
            if cached is None:
                raise SourceError("SOURCE_SCHEMA", "CAP 304 without cached content")
            data = cached[1]
        else:
            data = response.content
        alert = parse_cap(data, entry)
        if response.status_code == 200:
            self._cap_cache[url] = (response.headers.get("ETag"), data)
        return alert

    async def get_by_ref(self, alert_ref: str) -> CapAlert:
        entries = await self.list_entries()
        entry = next((item for item in entries if item.alert_ref == alert_ref), None)
        if entry is None:
            raise AlertNotFound(alert_ref)
        return await self.get_cap(entry)
