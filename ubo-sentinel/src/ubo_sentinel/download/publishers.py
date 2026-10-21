"""Where each publisher says its latest files are.

GLEIF: the golden-copy API names the latest publish and, per file type, the
full CSV as a zip; GLEIF states no checksum. OpenSanctions: the `sanctions`
collection's `index.json` names its version and, per resource, the URL, size
and SHA-1 of the file. Only the `sanctions` collection is fetched: PEPs and
crime are out of scope and far larger. Neither needs an API key for bulk files.
"""

from dataclasses import dataclass
from datetime import datetime

import httpx

GLEIF_LATEST = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
OPENSANCTIONS_INDEX = "https://data.opensanctions.org/datasets/latest/sanctions/index.json"

GLEIF_LICENCE = "CC0-1.0"
OPENSANCTIONS_LICENCE = "CC-BY-NC-4.0"

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


PUBLISHERS = {"gleif": gleif_files, "opensanctions": opensanctions_files}
