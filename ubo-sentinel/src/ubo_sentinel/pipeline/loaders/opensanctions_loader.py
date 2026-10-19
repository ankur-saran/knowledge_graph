"""OpenSanctions' sanctions collection: one FollowTheMoney NDJSON file, three datasets.

`os_entities` (the companies, organisations and people), `os_sanctions` (the
`Sanction` records: one designation, on one list) and `os_relationships` (the
links between them), source `opensanctions`. The file is hashed once. Each
dataset holds the records of its schemas and declares the fields its mapper
reads (`pipeline/mappers.py`); records of other schemas (addresses,
identifications) are in no dataset.
"""

from ubo_sentinel.pipeline.loaders.real_base import Column, Format, Kind, RealDataset
from ubo_sentinel.pipeline.mappers import (
    OS_ENTITY_SCHEMAS,
    OS_LINK_SCHEMAS,
    OS_OTHER_LINK_SCHEMAS,
    OS_OTHER_SCHEMAS,
)

SOURCE = "opensanctions"
FILE = "opensanctions_entities"


def _record(*properties: str) -> tuple[Column, ...]:
    """The fields of every FtM record a mapper reads, and the given properties."""
    return (
        Column("id"),
        Column("schema"),
        Column("caption"),
        Column("datasets", kind=Kind.TEXT_LIST),
        # The ids this record had before OpenSanctions merged records into it.
        Column("referents", kind=Kind.TEXT_LIST),
        *(Column(name, within="properties", kind=Kind.TEXT_LIST) for name in properties),
    )


OS_ENTITIES = RealDataset(
    name="os_entities",
    table="bronze_entities",
    source=SOURCE,
    file=FILE,
    format=Format.NDJSON,
    columns=(
        *_record(
            "name",
            "alias",
            "previousName",
            "weakAlias",
            "leiCode",
            "jurisdiction",
            "country",
            # Kept for a registration crosswalk (the proposal "Identifier schemes").
            "registrationNumber",
            "ogrnCode",
            "innCode",
        ),
        Column("target", kind=Kind.BOOL),
    ),
    key=("id",),
    # Vessels, aircraft and securities are in Bronze; the mapper counts and leaves them.
    select=("schema", frozenset({*OS_ENTITY_SCHEMAS, *OS_OTHER_SCHEMAS})),
)

OS_SANCTIONS = RealDataset(
    name="os_sanctions",
    table="bronze_sanctions",
    source=SOURCE,
    file=FILE,
    format=Format.NDJSON,
    columns=_record(
        "entity", "program", "programId", "listingDate", "startDate", "endDate", "authority"
    ),
    key=("id",),
    select=("schema", frozenset({"Sanction"})),
)

OS_RELATIONSHIPS = RealDataset(
    name="os_relationships",
    table="bronze_relationships",
    source=SOURCE,
    file=FILE,
    format=Format.NDJSON,
    columns=_record("owner", "asset", "percentage", "director", "organization", "role"),
    key=("id",),
    # Links the graph does not hold (family, associates) are in Bronze, counted.
    select=("schema", frozenset({*OS_LINK_SCHEMAS, *OS_OTHER_LINK_SCHEMAS})),
)

OS_DATASETS = (OS_ENTITIES, OS_SANCTIONS, OS_RELATIONSHIPS)
