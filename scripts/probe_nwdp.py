"""Manual, bounded probe of the official NWDP CWC hourly river-level dataset.

This is diagnostic code, not an application adapter. It makes live requests only
when run explicitly and never writes data to disk.
"""

import csv
import json
from itertools import islice
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DATASET_ID = "68600163-c5a0-4327-aa1a-fa7157b86cce"
CATALOG_URL = "https://nwdp.nwic.in/dataset/" + DATASET_ID
API_URL = "https://nwdp.nwic.in/api/3/action/package_show?" + urlencode(
    {"id": DATASET_ID}
)
TIMEOUT_SECONDS = 20


def main() -> int:
    print("Dataset:", CATALOG_URL)
    print("Metadata request:", API_URL)
    try:
        with urlopen(
            Request(API_URL, headers={"User-Agent": "JalWatch-feasibility/0.1"}),
            timeout=TIMEOUT_SECONDS,
        ) as response:
            print("Metadata HTTP:", response.status)
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print("Metadata unavailable:", type(exc).__name__, str(exc)[:300])
        return 1

    if not payload.get("success") or not isinstance(payload.get("result"), dict):
        print("Unexpected CKAN metadata envelope")
        return 1
    dataset = payload["result"]
    print("Dataset title:", dataset.get("title"))
    print("Dataset modified:", dataset.get("metadata_modified"))
    resources = dataset.get("resources", [])
    print("Resource count:", len(resources))
    current = [
        resource
        for resource in resources
        if "2026" in str(resource.get("name", ""))
        and "2030" in str(resource.get("name", ""))
        and str(resource.get("format", "")).upper() == "CSV"
    ]
    for resource in current[:8]:
        print(
            "Current resource:",
            resource.get("name"),
            resource.get("id"),
            resource.get("url"),
        )
    if not current:
        print("No 2026-2030 CSV resource found in live metadata")
        return 1

    resource = next(
        (
            item
            for item in current
            if "brahmaputra" in str(item.get("name", "")).lower()
        ),
        current[0],
    )
    url = resource.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        print("Selected resource lacks a usable HTTPS URL")
        return 1
    print("Selected resource:", resource.get("name"))
    print("Selected resource URL:", url)
    print("Datastore active:", resource.get("datastore_active"))
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "JalWatch-feasibility/0.1"}),
            timeout=TIMEOUT_SECONDS,
        ) as response:
            print("Resource HTTP:", response.status)
            print("Content-Type:", response.headers.get("Content-Type"))
            # Read only a bounded prefix. This avoids downloading a basin-wide file.
            prefix = response.read(64 * 1024).decode("utf-8-sig", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        print("Resource unavailable:", type(exc).__name__, str(exc)[:300])
        return 1

    reader = csv.DictReader(prefix.splitlines())
    print("CSV columns:", reader.fieldnames)
    for row in islice(reader, 3):
        print("Example row:", json.dumps(row, ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
