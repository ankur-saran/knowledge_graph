"""Where each publisher says its latest files are.

GLEIF: the golden-copy API names the latest publish and, per file type, the
full CSV as a zip; GLEIF states no checksum. OpenSanctions: the `sanctions`
collection's `index.json` names its version and, per resource, the URL, size
and SHA-1 of the file. Only the `sanctions` collection is fetched: PEPs and
crime are out of scope and far larger. Companies House (Step 17): two download
pages list the dated parts of the company file (CSV) and of the PSC snapshot
(JSON lines), each a zip of one file; no checksum is published. None of the
three needs an API key for bulk files.
"""

import re
from dataclasses import dataclass
from datetime import datetime

import httpx

GLEIF_LATEST = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
OPENSANCTIONS_INDEX = "https://data.opensanctions.org/datasets/latest/sanctions/index.json"

GLEIF_LICENCE = "CC0-1.0"
OPENSANCTIONS_LICENCE = "CC-BY-NC-4.0"
COMPANIES_HOUSE_LICENCE = "OGL-3.0"

COMPANIES_HOUSE_BASE = "https://download.companieshouse.gov.uk/"
# Manifest file key -> (download page, part file name with date, part and count
# groups, path in the refresh directory, whether each part repeats a header row).
COMPANIES_HOUSE_FILES = {
    "ch_companies": (
        COMPANIES_HOUSE_BASE + "en_output.html",
        r"BasicCompanyData-(\d{4}-\d{2}-\d{2})-part(\d+)_(\d+)\.zip",
        "companies_house/companies.csv",
        True,
    ),
    "ch_psc": (
        COMPANIES_HOUSE_BASE + "en_pscdata.html",
        r"psc-snapshot-(\d{4}-\d{2}-\d{2})_(\d+)of(\d+)\.zip",
        "companies_house/psc.txt",
        False,
    ),
}

# Manifest file key -> (GLEIF file type, path in the refresh directory).
GLEIF_FILES = {
    "gleif_lei2": ("lei2", "gleif/lei2.csv"),
    "gleif_rr": ("rr", "gleif/rr.csv"),
    "gleif_repex": ("repex", "gleif/repex.csv"),
}
OPENSANCTIONS_RESOURCE = "entities.ftm.json"
OPENSANCTIONS_FILE = ("opensanctions_entities", "opensanctions/entities.ftm.json")


class PublisherError(ValueError):
    """A publisher's index is not what this code reads."""


@dataclass(frozen=True)
class Planned:
    """One file a publisher offers, as its index describes it."""

    key: str
    source: str
    path: str
    url: str
    published_at: datetime
    licence: str
    # True when the URL is a zip holding the file.
    zipped: bool
    # "<algorithm>:<hex>" of the file as fetched, when the publisher states one.
    publisher_checksum: str | None = None
    # The URLs of the zips the file is joined from, in order, when the publisher
    # splits it (Companies House); `url` is then the first.
    parts: tuple[str, ...] = ()
    # True when each part repeats the file's header row (a CSV split in parts).
    repeats_header: bool = False


def _get_json(client: httpx.Client, url: str) -> dict:
    response = client.get(url)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise PublisherError(f"{url}: not JSON ({exc}).") from exc


def gleif_files(client: httpx.Client) -> list[Planned]:
    data = _get_json(client, GLEIF_LATEST)
    planned = []
    try:
        publish = data["data"]
        for key, (kind, path) in GLEIF_FILES.items():
            csv = publish[kind]["full_file"]["csv"]
            planned.append(
                Planned(
                    key=key,
                    source="gleif",
                    path=path,
                    url=csv["url"],
                    published_at=datetime.fromisoformat(publish[kind]["publish_date"]),
                    licence=GLEIF_LICENCE,
                    zipped=True,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise PublisherError(f"{GLEIF_LATEST}: unexpected content ({exc!r}).") from exc
    return planned


def opensanctions_files(client: httpx.Client) -> list[Planned]:
    data = _get_json(client, OPENSANCTIONS_INDEX)
    try:
        (resource,) = [r for r in data["resources"] if r["name"] == OPENSANCTIONS_RESOURCE]
        key, path = OPENSANCTIONS_FILE
        return [
            Planned(
                key=key,
                source="opensanctions",
                path=path,
                url=resource["url"],
                published_at=datetime.fromisoformat(data["updated_at"]),
                licence=OPENSANCTIONS_LICENCE,
                zipped=False,
                publisher_checksum=f"sha1:{resource['checksum'].lower()}",
            )
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise PublisherError(f"{OPENSANCTIONS_INDEX}: unexpected content ({exc!r}).") from exc


def companies_house_files(client: httpx.Client) -> list[Planned]:
    """The newest complete set of parts of each Companies House file its page lists."""
    planned = []
    for key, (page, pattern, path, repeats_header) in COMPANIES_HOUSE_FILES.items():
        response = client.get(page)
        response.raise_for_status()
        found: dict[str, dict[int, tuple[str, int]]] = {}
        for match in re.finditer(pattern, response.text):
            date, part, count = match[1], int(match[2]), int(match[3])
            found.setdefault(date, {})[part] = (match[0], count)
        if not found:
            raise PublisherError(f"{page}: lists no file named like {pattern}.")
        date = max(found)
        parts = found[date]
        counts = {count for _, count in parts.values()}
        if len(counts) != 1 or sorted(parts) != list(range(1, counts.pop() + 1)):
            raise PublisherError(f"{page}: the parts of the {date} file are not all listed.")
        urls = tuple(COMPANIES_HOUSE_BASE + parts[n][0] for n in sorted(parts))
        planned.append(
            Planned(
                key=key,
                source="companies_house",
                path=path,
                url=urls[0],
                published_at=datetime.fromisoformat(date),
                licence=COMPANIES_HOUSE_LICENCE,
                zipped=True,
                parts=urls,
                repeats_header=repeats_header,
            )
        )
    return planned


PUBLISHERS = {
    "gleif": gleif_files,
    "opensanctions": opensanctions_files,
    "companies_house": companies_house_files,
}
