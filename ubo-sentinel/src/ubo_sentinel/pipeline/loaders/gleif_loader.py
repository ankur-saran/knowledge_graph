"""GLEIF's golden copy: LEI records (L1), relationship records (RR) and reporting exceptions.

Three CSV files of one refresh, three datasets, source `gleif`. Each dataset
declares the GLEIF columns its mapper reads (`pipeline/mappers.py`); `_raw` is
that projection of the record. The column names are GLEIF's headers (LEI-CDF
3.1, RR-CDF 2.1); a file that lacks one is refused, so a change of format is
noticed at ingest.
"""

from pathlib import Path

import duckdb

from ubo_sentinel.pipeline.bronze import IngestError
from ubo_sentinel.pipeline.loaders.base import IngestSummary
from ubo_sentinel.pipeline.loaders.real_base import Column, Format, RealDataset, ingest_real
from ubo_sentinel.pipeline.mappers import GLEIF_NAME_FIELDS, GLEIF_REPEX_REASON_FIELDS

SOURCE = "gleif"


def _columns(*names: str) -> tuple[Column, ...]:
    return tuple(Column(name) for name in names)


GLEIF_L1 = RealDataset(
    name="gleif_l1",
    table="bronze_entities",
    source=SOURCE,
    file="gleif_lei2",
    format=Format.CSV,
    columns=_columns(
        "LEI",
        "Entity.LegalName",
        *GLEIF_NAME_FIELDS,
        "Entity.RegistrationAuthority.RegistrationAuthorityID",
        "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
        "Entity.LegalJurisdiction",
        "Entity.EntityCategory",
        "Entity.EntityStatus",
        "Entity.SuccessorEntity.1.SuccessorLEI",
        "Registration.RegistrationStatus",
    ),
    key=("LEI",),
)

GLEIF_RR = RealDataset(
    name="gleif_rr",
    table="bronze_relationships",
    source=SOURCE,
    file="gleif_rr",
    format=Format.CSV,
    columns=_columns(
        "Relationship.StartNode.NodeID",
        "Relationship.StartNode.NodeIDType",
        "Relationship.EndNode.NodeID",
        "Relationship.EndNode.NodeIDType",
        "Relationship.RelationshipType",
        "Relationship.RelationshipStatus",
        "DeletedAt",
    ),
    # GLEIF publishes one record per start node, end node and type.
    key=(
        "Relationship.StartNode.NodeID",
        "Relationship.EndNode.NodeID",
        "Relationship.RelationshipType",
    ),
)

GLEIF_REPEX = RealDataset(
    name="gleif_repex",
    table="bronze_repex",
    source=SOURCE,
    file="gleif_repex",
    format=Format.CSV,
    columns=_columns("LEI", "Exception.Category", *GLEIF_REPEX_REASON_FIELDS, "DeletedAt"),
    key=("LEI", "Exception.Category"),
)

GLEIF_DATASETS = (GLEIF_L1, GLEIF_RR, GLEIF_REPEX)

# Every dataset of the real family. A real set holds one snapshot of each; an
# ingest that supplies some takes the others from an earlier real set.
REAL_DATASETS = tuple(d.name for d in GLEIF_DATASETS)


def ingest_gleif(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    """Ingest the GLEIF files of a refresh directory (`ubo ingest --source gleif --path`)."""
    if path is None:
        raise IngestError("A GLEIF ingest reads a refresh directory: pass --path.")
    return ingest_real(con, path, GLEIF_DATASETS, REAL_DATASETS, base=base, source=SOURCE)
