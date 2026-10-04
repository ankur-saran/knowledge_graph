"""Loaders read a source's files into Bronze. They never open a network connection."""

from collections.abc import Callable
from pathlib import Path

import duckdb

from ubo_sentinel.pipeline.loaders.fixture_loader import IngestSummary, ingest_fixtures

Loader = Callable[[duckdb.DuckDBPyConnection, Path | None, str | None], IngestSummary]

# `ubo ingest --source <name>` -> loader. A source is added in the step that implements it.
LOADERS: dict[str, Loader] = {"fixtures": ingest_fixtures}
