"""Manual, bounded probe of SACHET's official national RSS and linked CAP XML.

This is diagnostic code, not a production client. It performs no persistent
caching and prints only a small sample of public alert fields.
"""

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
TIMEOUT_SECONDS = 20


def main() -> int:
    print("RSS request:", RSS_URL)
    try:
        with urlopen(
            Request(RSS_URL, headers={"User-Agent": "JalWatch-feasibility/0.1"}),
            timeout=TIMEOUT_SECONDS,
        ) as response:
            print("RSS HTTP:", response.status)
            print("Content-Type:", response.headers.get("Content-Type"))
            feed = response.read(512 * 1024)
    except (HTTPError, URLError, TimeoutError) as exc:
        print("RSS unavailable:", type(exc).__name__, str(exc)[:300])
        return 1
    try:
        root = ElementTree.fromstring(feed)
    except ElementTree.ParseError as exc:
        print("Malformed RSS XML:", exc)
        return 1
    print("RSS root:", root.tag)
    items = root.findall("./channel/item")
    print("RSS item count:", len(items))
    if not items:
        print("Empty feed is valid; no CAP item to inspect today")
        return 0

    for item in items[:2]:
        print(
            "RSS item fields:", {child.tag: (child.text or "")[:250] for child in item}
        )

    cwc_items = [
        item for item in items if "(CWC)" in item.findtext("author", default="")
    ]
    print("CWC item count:", len(cwc_items))
    if not cwc_items:
        print("No CWC item in current feed; empty result is valid")
        return 0
    first = cwc_items[0]
    print(
        "Selected CWC RSS item:",
        {child.tag: (child.text or "")[:250] for child in first},
    )
    cap_url = first.findtext("link", default="")
    if not cap_url.startswith(
        "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier="
    ):
        print("Unexpected CAP link in selected RSS item")
        return 1
    print("CAP request:", cap_url)
    try:
        with urlopen(
            Request(cap_url, headers={"User-Agent": "JalWatch-feasibility/0.1"}),
            timeout=TIMEOUT_SECONDS,
        ) as response:
            print("CAP HTTP:", response.status)
            etag = response.headers.get("ETag")
            print("CAP ETag:", etag)
            cap = response.read(512 * 1024)
    except (HTTPError, URLError, TimeoutError) as exc:
        print("CAP unavailable:", type(exc).__name__, str(exc)[:300])
        return 1
    try:
        cap_root = ElementTree.fromstring(cap)
    except ElementTree.ParseError as exc:
        print("Malformed CAP XML:", exc)
        return 1
    print("CAP root:", cap_root.tag)
    for element in cap_root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in {
            "identifier",
            "sender",
            "sent",
            "event",
            "severity",
            "urgency",
            "certainty",
            "effective",
            "onset",
            "expires",
            "areaDesc",
            "headline",
            "description",
        }:
            print("CAP", name + ":", (element.text or "")[:250])
    if etag:
        conditional = Request(
            cap_url,
            headers={
                "User-Agent": "JalWatch-feasibility/0.1",
                "If-None-Match": etag,
            },
        )
        try:
            with urlopen(conditional, timeout=TIMEOUT_SECONDS) as response:
                print("Conditional CAP HTTP:", response.status)
        except HTTPError as exc:
            print("Conditional CAP HTTP:", exc.code)
        except (URLError, TimeoutError) as exc:
            print("Conditional CAP unavailable:", type(exc).__name__, str(exc)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
