"""Loaders read a source's files into Bronze. They never open a network connection."""

from collections.abc import Callable
from pathlib import Path

import duckdb

from ubo_sentinel.pipeline.loaders.base import DatasetSummary, IngestSummary
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.loaders.real_loader import (
    ingest_all,
    ingest_gleif,
    ingest_opensanctions,
)

Loader = Callable[[duckdb.DuckDBPyConnection, Path | None, str | None], IngestSummary]

# `ubo ingest --source <name>` -> loader. A source is added in the step that implements it.
LOADERS: dict[str, Loader] = {
    "fixtures": ingest_fixtures,
    "real": ingest_all,
    "gleif": ingest_gleif,
    "opensanctions": ingest_opensanctions,
}

__all__ = ["LOADERS", "DatasetSummary", "IngestSummary", "Loader"]
