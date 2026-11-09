"""What kind of source a record comes from.

When records of one entity merge, a registry's record describes the entity
before a list's record does, whichever is newer. A source that is not listed
here cannot be normalised.
"""

REGISTRY = 0
LIST = 1
# A holder a reviewer confirmed from an analyst's document (Step 15): after
# registries and lists, so it describes an entity only when nothing else does.
DOCUMENT = 2

# Source -> rank; the lowest rank describes a merged entity.
SOURCE_RANK: dict[str, int] = {
    "fx_registry": REGISTRY,
    "fx_list": LIST,
    "gleif": REGISTRY,
    "opensanctions": LIST,
    "document": DOCUMENT,
}

# Source -> the confidence a real loader gives each of its records. A fixture
# row states its own. A published record is taken as the publisher states it;
# confidence below 1 belongs to links Silver infers, not to the records.
# A document record is a person's confirmation; the proposer's own confidence is not kept.
SOURCE_CONFIDENCE: dict[str, float] = {"gleif": 1.0, "opensanctions": 1.0, "document": 1.0}

# Datasets that are context, not graph (Step 14's addresses): Silver leaves them,
# and `pipeline/context.py` derives them after Gold.
CONTEXT_DATASETS = frozenset({"addresses", "gleif_l1_addresses", "os_addresses"})

# Holdings and holders confirmed from documents (Step 15): optional members of a set
# of any family, published from the audit log (`pipeline/loaders/document_loader.py`).
# Every loader lists them as optional, so a set built on one keeps them.
DOCUMENT_DATASETS = ("doc_entities", "doc_relationships")
