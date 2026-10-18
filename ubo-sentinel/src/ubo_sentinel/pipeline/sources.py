"""What kind of source a record comes from.

When records of one entity merge, a registry's record describes the entity
before a list's record does, whichever is newer. A source that is not listed
here cannot be normalised.
"""

REGISTRY = 0
LIST = 1

# Source -> rank; the lowest rank describes a merged entity.
SOURCE_RANK: dict[str, int] = {"fx_registry": REGISTRY, "fx_list": LIST}

# Source -> the confidence a real loader gives each of its records. A fixture
# row states its own. A published record is taken as the publisher states it;
# confidence below 1 belongs to links Silver infers, not to the records.
SOURCE_CONFIDENCE: dict[str, float] = {"gleif": 1.0, "opensanctions": 1.0}
