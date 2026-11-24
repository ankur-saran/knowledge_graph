"""The real family: GLEIF, OpenSanctions and Companies House, from one refresh directory.

A real snapshot set holds one snapshot of every dataset in `REAL_DATASETS`.
`--source real` ingests them all; `--source gleif`, `--source opensanctions`
or `--source companies_house` ingests one source and takes the others' datasets
from the newest real set (or `--base`), so a first ingest of one source alone is
refused.

Companies House's datasets (Step 17) are optional members: a refresh directory
whose manifest has no Companies House file still ingests, and the new set holds
none. The quality check (`pipeline/quality.py`) then refuses it as a fall to zero
if the last accepted set had them, so `real` never loses UK stakes unawares.
"""

from collections.abc import Sequence
from pathlib import Path

import duckdb

from ubo_sentinel.models.manifest import load_manifest
from ubo_sentinel.pipeline.bronze import FAMILY_REAL, IngestError
from ubo_sentinel.pipeline.loaders.base import IngestSummary
from ubo_sentinel.pipeline.loaders.companies_house_loader import CH_DATASETS
from ubo_sentinel.pipeline.loaders.gleif_loader import GLEIF_CONTEXT_DATASETS, GLEIF_DATASETS
from ubo_sentinel.pipeline.loaders.opensanctions_loader import OS_CONTEXT_DATASETS, OS_DATASETS
from ubo_sentinel.pipeline.loaders.real_base import RealDataset, ingest_real
from ubo_sentinel.pipeline.sources import DOCUMENT_DATASETS

REAL_DATASETS = tuple(d.name for d in (*GLEIF_DATASETS, *OS_DATASETS))
# Addresses (Step 14) and Companies House (Step 17): members of a set when
# ingested, never needed for one.
REAL_OPTIONAL_DATASETS = tuple(
    d.name for d in (*GLEIF_CONTEXT_DATASETS, *OS_CONTEXT_DATASETS, *CH_DATASETS)
)
# Optional datasets read only when the refresh directory has their file.
_WHEN_PRESENT = frozenset(d.name for d in CH_DATASETS)


def _present(path: Path, datasets: Sequence[RealDataset]) -> list[RealDataset]:
    """The datasets to read: an optional source's only when the manifest lists its file."""
    try:
        files = load_manifest(path).files
    except (OSError, ValueError):
        # `ingest_real` reports an unreadable manifest.
        return list(datasets)
    return [d for d in datasets if d.name not in _WHEN_PRESENT or d.file in files]


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
        con, path, _present(path, datasets), REAL_DATASETS, base=base, source=source,
        optional=(*REAL_OPTIONAL_DATASETS, *DOCUMENT_DATASETS),
    )  # fmt: skip


def ingest_all(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    """Every real dataset of a refresh directory (`ubo ingest --source real --path`)."""
    datasets = (
        *GLEIF_DATASETS,
        *GLEIF_CONTEXT_DATASETS,
        *OS_DATASETS,
        *OS_CONTEXT_DATASETS,
        *CH_DATASETS,
    )
    return _ingest(con, path, base, datasets, FAMILY_REAL)


def ingest_gleif(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    return _ingest(con, path, base, (*GLEIF_DATASETS, *GLEIF_CONTEXT_DATASETS), "gleif")


def ingest_opensanctions(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    return _ingest(con, path, base, (*OS_DATASETS, *OS_CONTEXT_DATASETS), "opensanctions")


def ingest_companies_house(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    return _ingest(con, path, base, CH_DATASETS, "companies_house")
