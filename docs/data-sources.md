# JalWatch India data-source feasibility spike

Verified **2026-10-06**. This is a diagnostic result, not an implemented source
adapter. The Python probes in `scripts/` make live calls only when run manually.
No production tool, cache, state, model, or graph was changed.

## Decision: narrow the MVP before Task 3

SACHET's national RSS and a linked CAP alert worked. The NWDP CWC dataset is
listed publicly, but the live dataset page and CKAN metadata request were
unreachable from the available access paths. We therefore could not fetch a
2026–2030 resource, inspect its real columns, establish a canonical station ID,
or prove that current and 1–30 day observations are retrievable. Implementing
the original three NWDP station tools now would require guessing the source
schema. The Task 2 contracts were therefore refined for **JalWatch India — CWC
Flood Alert Triage & Operational Escalation Agent**, using verified SACHET RSS
and CAP access. Task 3 later implemented only the SACHET-backed MVP. Re-run the
NWDP probe from a network that can reach the portal before reviving any station
tool contract.

## CWC river-level observations: NWDP

- **Authority and producer:** [National Water Data Portal (NWDP)](https://nwdp.nwic.in/),
  National Water Informatics Centre, Ministry of Jal Shakti; dataset producer
  Central Water Commission (CWC).
- **Dataset:** [River Water Level (Telemetry - Hourly), CWC](https://nwdp.nwic.in/dataset/68600163-c5a0-4327-aa1a-fa7157b86cce).
  The catalog description says records contain a date, station identifier with
  geographic hierarchy, and river-level values. It advertises CSV and API and
  basin resources. Hourly is the stated observation cadence, not a verified
  current publication or freshness guarantee.
- **Mechanism attempted:** CKAN-style `package_show` metadata at
  `https://nwdp.nwic.in/api/3/action/package_show?id=68600163-c5a0-4327-aa1a-fa7157b86cce`,
  followed by the selected resource URL if metadata succeeds. NWDP's
  [about page](https://nwdp.nwic.in/about) identifies CKAN, but this exact API
  endpoint remains **unverified**. The manual probe selects a 2026–2030 CSV
  resource (Brahmaputra if present) and reads at most 64 KiB.
- **Live result:** the local metadata GET timed out after 20 seconds. The
  browser retrieval of the dataset page also timed out; another structured
  fetch path could not load it. No CSV or API response was obtained. An indexed
  catalog excerpt was available, but its displayed metadata dates are from
  2025, so it cannot establish the present resource list or schema. The
  reported 2026–2030 resources and their accessibility remain unconfirmed here.
- **Actual CSV columns, measurement field/unit, geographic fields, timestamp
  encoding, missing-value convention, and canonical station ID:** **unknown**.
  Do not infer them from the dataset description.
- **History volume:** if a complete hourly series exists, a 1–30 day window
  would contain roughly 24–720 rows *per station*. This is an estimate, not a
  measured response count. Server-side station/date filtering, pagination, and
  API rate/authorization rules were not verified.

`list_monitoring_stations(region)`, `get_station_status(station_id)`, and
`get_water_history(station_id, days)` are **unproven** against this source.
State, district, basin, and station-code mapping must be checked in an actual
resource or supported API before choosing the canonical ID. For status, the
future adapter must select the latest observation by parsed source timestamp,
report its age, and distinguish absent, missing-value, and stale records. A
time-stamped CAP flood description is not a substitute for a station-series
record: it does not provide a confirmed NWDP station identifier.

No authoritative `StationObservation` instance can be shown from this spike.
Its existing fields appear conceptually adequate, but the real mapping is
pending: `station_id` needs a verified NWDP code; `metric`, `unit`, and
`observed_at` need actual columns; `value=None` can represent a source
missing measurement; `retrieved_at` is fetch time; `source_url` should point
to the exact NWDP resource or supported API. `official_status` must remain
source text only. No Task 1 schema amendment is justified by unavailable data.

## Flood and water alerts: NDMA SACHET

- **Authority:** [SACHET National Disaster Alert Portal](https://sachet.ndma.gov.in/CapFeed),
  National Disaster Management Authority (NDMA). CWC is among its listed
  alerting partners.
- **Mechanism:** the public [national RSS feed](https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml)
  (`application/xml`, RSS `rss/channel/item`) links to individual
  [CAP 1.2 XML documents](https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=1791259767743027)
  (`text/xml;charset=UTF-8`, namespace
  `urn:oasis:names:tc:emergency:cap:1.2`). The official
  [integration guide](https://sachet.ndma.gov.in/docs/Integration_Guide_For_Agencies.pdf)
  documents `FetchXMLFile?identifier=...`, ETag, and conditional
  `If-None-Match` requests; a repeat may return 304. The probe prints the
  ETag and makes one conditional repeat. The observed repeat returned HTTP
  **304**. A production cache is deferred.
- **Observed 2026-10-06, about 09:02 UTC:** HTTP 200; 99 RSS items, 29 with
  `author` ending `(CWC)`. Counts are a snapshot, not a promised feed size or
  proof that all items were active at fetch time. RSS item fields observed:
  `title`, empty `description`, `category`, `link`, `author`, `guid`, and
  `pubDate`. The example flood item's RSS `category` was `Met`, so category
  alone must not be used as a water-alert filter.
- **Linked CAP example:**
  [identifier `1791259767743027`](https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=1791259767743027)
  returned `identifier=IN-1791259767743027_27`, `sender=Jharkhand-SDMA`,
  `status=Actual`, `msgType=Update`, `scope=Public`,
  `references=CWC,IN-1791259767743027_5,...`, and an `info` block with
  `language=en-IN`, `category=Met`, `event=Flood`, `urgency=Expected`,
  `severity=Severe`, `certainty=Likely`, `effective`, `onset`, `expires`,
  `headline`, `description`, and `area/areaDesc`.
  The area description was `Ganga, Sahibganj, Sahibganj, Jharkhand`.
  The alert text included 27.72 m at 08:00 local time, but this is alert
  narrative, not a station-series row. CAP `sent` was
  `2026-10-06T13:25:00+05:30`; `expires` was
  `2026-10-07T06:00:00+05:30`.
- **Agency and region filtering:** the RSS `author` identified CWC for this
  item, while CAP `sender` identified Jharkhand SDMA and `references` named
  CWC. An adapter should preserve both fields and use a documented provenance
  rule; `sender == CWC` would miss this alert. CAP `event` and expiration
  should determine water relevance and activity. Region can be normalized
  from `areaDesc`; it is free text, so mapping aliases/state names needs care.
  Do not infer latitude/longitude from the sample CAP `altitude` and `ceiling`
  fields.
- **Update cadence:** RSS `pubDate` varied by item and multiple notices were
  published within minutes. There is no verified fixed polling interval or
  completeness guarantee. Use CAP `sent`, `effective`, and `expires` for
  alert freshness, retaining the feed retrieval time separately.

Example mapping into the existing `WaterAlert` model (values are from the
official CAP above; `retrieved_at` is assigned by the client when fetched):

```text
alert_id      = "IN-1791259767743027_27"  # CAP identifier
region        = "Jharkhand"                # normalized from areaDesc
severity      = "Severe"                   # source label, not agent inference
message       = CAP description            # full source text
issued_at     = 2026-10-06T13:25:00+05:30 # CAP sent
retrieved_at  = client fetch time
source_url    = CAP link from RSS item
station_ids   = ()                         # no NWDP station ID confirmed
```

The current model can represent this alert without a Task 1 amendment.
`urgency`, `certainty`, `event`, `effective`, `onset`, `expires`, CAP sender,
references, and full area detail are **not** separate fields in `WaterAlert`.
They matter to filtering, attribution, and validity checks and should remain
available in the source adapter during normalization. Task 3's separate `CapAlert`
record and detailed tool output preserve these fields without squeezing them
into `WaterAlert.message`.
The refined Task 2 `list_active_cwc_alerts(region?, severity?)` contract is
feasible as an input schema. Its future output must make source severity,
activity/expiry, region, and provenance clear enough for grounded reasoning.

## Original-contract findings and refined MVP

| Planned contract | Finding |
| --- | --- |
| `list_monitoring_stations(region)` | Blocked: real NWDP station ID and geographic fields unverified. |
| `get_station_status(station_id)` | Blocked: no accessible current NWDP row, unit, timestamp, or null convention. |
| `get_water_history(station_id, days)` | Blocked: hourly series advertised, but 1–30 day filters and volume unverified. |
| `get_active_water_alerts(region)` | Feasible with RSS discovery plus linked CAP XML, expiry and region filtering. |
| `create_escalation(...)` | Can remain a local simulated operational write; no government API is needed. Later execution still requires explicit human approval. |

The refined Task 2 contracts, implemented as guarded local tools in Task 3, are
`list_active_cwc_alerts(region?, severity?)`, `get_alert_details(alert_ref)`,
`search_recent_cwc_alerts(hours, region?)`, `list_affected_regions()`, and
`create_escalation(alert_refs, region, rationale, severity)`. Recent search is
bounded to 1–72 hours, subject to actual feed retention. A discovery result
provides an opaque `alert_ref` from RSS `guid`; the versioned authoritative CAP
`identifier` is retained separately as `cap_identifier`. The source client
resolves the reference from its validated feed entry, never from an LLM-supplied
URL. `list_affected_regions` deterministically aggregates active sourced alerts.
The three NWDP station
contracts are removed from the Task 2 model binding. Station models remain in
Task 1 for a possible future extension and are unused by this MVP.

Keep eventual source-specific code behind small boundaries:

```text
SachetClient -> parse/normalize CAP alert -> CapAlert -> structured tool result
NWDPClient   -> validate/normalize source row -> StationObservation (future only)
```

The eventual tools should return normalized domain records with source URLs,
not raw CSV/XML or prompt-shaped strings. For NWDP, prefer a documented API
**if** it supports station and date filtering and its behavior is confirmed;
that would avoid downloading basin-wide files for one station. Otherwise use
official CSV resources with bounded streaming/partitioning. This comparison
is provisional because neither access method could be tested. For SACHET,
RSS plus CAP XML is the verified structured route; use ETag on repeated CAP
fetches and avoid scraping rendered pages.

## Task 3 implementation notes

The SACHET adapter uses the official national RSS endpoint and follows each
validated `FetchXMLFile?identifier=...` link. `alert_ref` is the opaque RSS
`guid`/fetch handle (the observed example was `1791259767743027`);
`cap_identifier` is the CAP `<identifier>` (the observed message was
`IN-1791259767743027_27`). They are not interchangeable. XML parsing uses
`defusedxml`; requests have a 10-second timeout and a JalWatch User-Agent.
The in-process CAP cache stores ETag and XML content. A repeat sends
`If-None-Match`, and 304 returns the cached parsed result. It is not a graph
checkpoint or durable cache.

A manual direct-tool smoke on **2026-10-06** returned HTTP-backed Bihar CWC
flood alerts through the JSON-RPC dispatcher, including the separate RSS
`alert_ref`, versioned `cap_identifier`, source `Severe` label, expiry time,
raw area description, and both RSS-author and CAP-reference CWC provenance.
This verifies the read path at that instant; current alerts may change.

The active predicate requires CAP `status=Actual`, `msgType=Alert` or `Update`,
effective no later than the injected current time when provided, and a future
`expires`. Missing expiry is treated as not confidently active. A valid Update
or Cancel suppresses a referenced earlier message when both versions are in
the currently exposed feed. CAP references use `sender,identifier,sent` identity.
If an earlier version or a link in the update chain has fallen out of the feed,
complete chain reconstruction is impossible; the tool does not infer it.

CWC provenance is recorded as structured evidence from RSS author `(CWC)`, CAP
sender/source exactly `CWC`, or a CAP reference whose sender is `CWC`. Text in
the narrative description is not a classification signal. The source's raw
`areaDesc` strings remain intact; normalized regions are only explicit Indian
state/UT name matches. Unknown areas yield no normalized region, so a regional
filter may omit a real alert whose location cannot be matched confidently.

`search_recent_cwc_alerts` filters publication time within the requested 1–72
hours **among entries still available in the current SACHET feed**. It is not
a historical archive. The four read tools return structured data through
JSON-RPC and ToolMessages. The local `create_escalation` capability is
provisioned but normal model dispatch returns `APPROVAL_REQUIRED`; Task 6 must
provide real human approval before consequential execution.

## Failure and freshness handling for later tools

| Condition | Later tool behavior to design |
| --- | --- |
| Upstream unreachable / timeout | Structured recoverable source-unavailable error; never claim a negative or safe result. |
| HTTP non-200 | Distinguish 304 cached CAP response from 4xx/5xx failures; report status and source. |
| Empty dataset/feed | Valid empty set only after successful parse; distinguish from failed fetch. |
| Malformed CSV row / RSS item / CAP XML | Skip or fail with structured schema error, with counts and provenance; do not silently fabricate values. |
| Missing measurement | Preserve `None` if the source explicitly lacks a value; no zero substitution. |
| Missing station | Return a distinct not-found/unknown-station error after authoritative lookup. |
| Stale timestamp | Compare source time to retrieval time and an explicit freshness policy; mark stale, never silently present as current. |
| Unexpected schema change | Reject unmapped fields/columns at the adapter boundary and surface a source-schema error. |

Neither probe is part of pytest, and neither has reusable production parsing
functions. The probes use Python's standard library, so no dependencies or
lockfile changes were needed. Run manually from the repository root in
PowerShell:

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = 'E:\mcp\.tools\uv-cache'
.\.tools\uv\bin\uv.exe run python scripts\probe_nwdp.py
.\.tools\uv\bin\uv.exe run python scripts\probe_sachet.py
```
