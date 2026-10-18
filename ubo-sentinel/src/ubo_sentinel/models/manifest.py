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


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal[1] = 1
    # File key (e.g. "gleif_lei2") -> its entry. Datasets name their file by key.
    files: dict[str, ManifestFile]

    @property
    def refresh_id(self) -> str:
        """Derived from the content: the same publishes give the same id."""
        return short_id([self.model_dump(mode="json")])

    def to_json(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


def load_manifest(refresh_dir: Path) -> Manifest:
    path = refresh_dir / MANIFEST_NAME
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
