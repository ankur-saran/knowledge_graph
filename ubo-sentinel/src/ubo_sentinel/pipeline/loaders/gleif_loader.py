"""GLEIF's golden copy: LEI records (L1), relationship records (RR) and reporting exceptions.

Three CSV files of one refresh, three datasets, source `gleif`. Each dataset
declares the GLEIF columns its mapper reads (`pipeline/mappers.py`); `_raw` is
that projection of the record. The column names are GLEIF's headers (LEI-CDF
3.1, RR-CDF 2.1); a file that lacks one is refused, so a change of format is
noticed at ingest.
"""

from ubo_sentinel.pipeline.loaders.real_base import Column, Format, RealDataset
from ubo_sentinel.pipeline.mappers import GLEIF_NAME_FIELDS, GLEIF_REPEX_REASON_FIELDS

SOURCE = "gleif"


def _columns(*names: str) -> tuple[Column, ...]:
    return tuple(Column(name) for name in names)


# When a record was deleted: GLEIF's delta files carry it, its golden copy does not.
DELETED_AT = Column("DeletedAt", optional=True)


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
    columns=(
        *_columns(
            "Relationship.StartNode.NodeID",
            "Relationship.StartNode.NodeIDType",
            "Relationship.EndNode.NodeID",
            "Relationship.EndNode.NodeIDType",
            "Relationship.RelationshipType",
            "Relationship.RelationshipStatus",
        ),
        DELETED_AT,
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
    columns=(*_columns("LEI", "Exception.Category", *GLEIF_REPEX_REASON_FIELDS), DELETED_AT),
    key=("LEI", "Exception.Category"),
)

GLEIF_DATASETS = (GLEIF_L1, GLEIF_RR, GLEIF_REPEX)

GLEIF_ADDRESS_PARTS = ("FirstAddressLine", "AdditionalAddressLine.1", "City", "Region",
                       "PostalCode", "Country")  # fmt: skip
GLEIF_ADDRESS_KINDS = {"LegalAddress": "LEGAL", "HeadquartersAddress": "HEADQUARTERS"}

# The legal and headquarters address of each LEI record (Step 14): a dataset of
# its own, read from the same file, so `gleif_l1` and its snapshot are as they
# were. Optional in a real set: context an investigation reads, never the graph.
GLEIF_L1_ADDRESSES = RealDataset(
    name="gleif_l1_addresses",
    table="bronze_addresses",
    source=SOURCE,
    file="gleif_lei2",
    format=Format.CSV,
    columns=_columns(
        "LEI",
        *(f"Entity.{kind}.{part}" for kind in GLEIF_ADDRESS_KINDS for part in GLEIF_ADDRESS_PARTS),
    ),
    key=("LEI",),
)

GLEIF_CONTEXT_DATASETS = (GLEIF_L1_ADDRESSES,)
