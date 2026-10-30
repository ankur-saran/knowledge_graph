"""The real family: GLEIF and OpenSanctions, from one refresh directory.

A real snapshot set holds one snapshot of every dataset in `REAL_DATASETS`.
`--source real` ingests them all; `--source gleif` or `--source opensanctions`
ingests one source and takes the other's datasets from the newest real set
(or `--base`), so a first ingest of one source alone is refused.
"""

from collections.abc import Sequence
from pathlib import Path

import duckdb

from ubo_sentinel.pipeline.bronze import FAMILY_REAL, IngestError
from ubo_sentinel.pipeline.loaders.base import IngestSummary
from ubo_sentinel.pipeline.loaders.gleif_loader import GLEIF_CONTEXT_DATASETS, GLEIF_DATASETS
from ubo_sentinel.pipeline.loaders.opensanctions_loader import OS_CONTEXT_DATASETS, OS_DATASETS
from ubo_sentinel.pipeline.loaders.real_base import RealDataset, ingest_real

REAL_DATASETS = tuple(d.name for d in (*GLEIF_DATASETS, *OS_DATASETS))
# Addresses (Step 14): a member of a set when ingested, never needed for one.
REAL_OPTIONAL_DATASETS = tuple(d.name for d in (*GLEIF_CONTEXT_DATASETS, *OS_CONTEXT_DATASETS))


def _ingest(
    con: duckdb.DuckDBPyConnection,
    path: Path | None,
    base: str | None,
    datasets: Sequence[RealDataset],
    source: str,
) -> IngestSummary:
    if path is None:
        raise IngestError(f"A {source} ingest reads a refresh directory: pass --path.")
    return ingest_real(
        con, path, datasets, REAL_DATASETS, base=base, source=source,
        optional=REAL_OPTIONAL_DATASETS,
    )  # fmt: skip


def ingest_all(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    """Every real dataset of a refresh directory (`ubo ingest --source real --path`)."""
    datasets = (*GLEIF_DATASETS, *GLEIF_CONTEXT_DATASETS, *OS_DATASETS, *OS_CONTEXT_DATASETS)
    return _ingest(con, path, base, datasets, FAMILY_REAL)


def ingest_gleif(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    return _ingest(con, path, base, (*GLEIF_DATASETS, *GLEIF_CONTEXT_DATASETS), "gleif")


def ingest_opensanctions(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    return _ingest(con, path, base, (*OS_DATASETS, *OS_CONTEXT_DATASETS), "opensanctions")
