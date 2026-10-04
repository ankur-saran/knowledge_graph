"""Fixtures shared by the test modules."""

from collections import defaultdict
from pathlib import Path

import pytest

from ubo_sentinel.models import EntityRow, read_rows

# Commands are run from the repository root.
T0 = Path("fixtures/snapshot_t0")


@pytest.fixture(scope="session")
def entities():
    return read_rows(T0 / "entities.csv", EntityRow)


@pytest.fixture(scope="session")
def canonical(entities):
    """Record ref -> canonical entity id, worked out from the files alone.

    Records that share an LEI, or a registration authority and number, are one
    entity. Its id is the LEI id when a member has an LEI, otherwise the
    smallest member id. Silver must arrive at the same ids by its own route.
    """
    parent = {row.ref: row.ref for row in entities}

    def find(ref):
        while parent[ref] != ref:
            ref = parent[ref]
        return ref

    first_with_key = {}
    for row in entities:
        keys = [("lei", row.lei)] if row.lei else []
        if row.registration_authority_id and row.registration_number:
            keys.append(("reg", row.registration_authority_id, row.registration_number))
        for key in keys:
            parent[find(row.ref)] = find(first_with_key.setdefault(key, row.ref))

    members = defaultdict(list)
    for row in entities:
        members[find(row.ref)].append(row)
    ids = {}
    for group in members.values():
        with_lei = [row.canonical_id() for row in group if row.lei]
        group_id = min(with_lei or [row.canonical_id() for row in group])
        ids.update({row.ref: group_id for row in group})
    return ids
