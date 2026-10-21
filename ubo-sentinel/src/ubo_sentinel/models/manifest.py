"""The download manifest: what a refresh fetched, from where, as the publisher stated it.

`ubo download` writes `var/downloads/<refresh_id>/manifest.json`; the real loaders
read it and never the clock. The publisher's timestamp is each record's `as_of`
and the snapshot's `published_at`; the file's entry is kept with its snapshot
(`bronze_snapshots.source_meta`), so a decision can be traced to the publish it
was made on.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ubo_sentinel.models.canonical import canonical_json, short_id

MANIFEST_NAME = "manifest.json"

# Snapshot-set families of published data. A set never mixes families, so a
# sample never supplies a dataset to a set of the publisher's full files.
FAMILY_REAL = "real"
FAMILY_SAMPLE = "sample"

_SHA256 = r"^[0-9a-f]{64}$"


class ManifestFile(BaseModel):
    """One file of a refresh, as stored in the refresh directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Relative to the refresh directory, with forward slashes.
    path: str = Field(pattern=r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
    source: str
    url: str
    # When the publisher published it; never when it was downloaded.
    published_at: datetime
    size: int = Field(ge=0)
    # Of the file the loader reads (unzipped).
    sha256: str = Field(pattern=_SHA256)
    # As the publisher states it, "<algorithm>:<hex>"; None when it states none (GLEIF).
    publisher_checksum: str | None = Field(default=None, pattern=r"^[a-z0-9]+:[0-9a-f]+$")
    # Of the archive as downloaded, when the file came zipped.
    archive_sha256: str | None = Field(default=None, pattern=_SHA256)
    licence: str


class SampleOf(BaseModel):
    """What a sample directory (`ubo sample`) was cut from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_id: str
    # Registry records and list targets it started from, and the seed that chose them.
    n: int = Field(ge=1)
    list_n: int = Field(ge=0)
    seed: int


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal[1] = 1
    # File key (e.g. "gleif_lei2") -> its entry. Datasets name their file by key.
    files: dict[str, ManifestFile]
    # Set when the files are a sample of a refresh rather than the publisher's files.
    sample: SampleOf | None = None

    @property
    def refresh_id(self) -> str:
        """Derived from the content: the same publishes give the same id."""
        return short_id([self.model_dump(mode="json")])

    @property
    def family(self) -> str:
        """The snapshot-set family its files are ingested into: a sample is never `real`."""
        return FAMILY_SAMPLE if self.sample is not None else FAMILY_REAL

    def to_json(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def write(self, directory: Path) -> None:
        """Write `manifest.json`, readable by a person, into a refresh directory."""
        text = json.dumps(self.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        (directory / MANIFEST_NAME).write_text(text, encoding="utf-8", newline="\n")


def load_manifest(refresh_dir: Path) -> Manifest:
    path = refresh_dir / MANIFEST_NAME
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
