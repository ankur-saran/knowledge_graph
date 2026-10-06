"""Fixtures shared by the test modules."""

import shutil
from collections import defaultdict
from pathlib import Path

import pytest

from ubo_sentinel.cli.app import ONTOLOGY_PATH
from ubo_sentinel.models import EntityRow, load_gold_scenarios, load_ontology, read_rows
from ubo_sentinel.pipeline.db import DB_ENV_VAR, connect
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.transformer import build_silver
from ubo_sentinel.rules.pack import DEFAULT_PACK, load_rule_pack, pack_path

# Commands are run from the repository root.
T0 = Path("fixtures/snapshot_t0")
T1 = Path("fixtures/snapshot_t1")
GOLD_PATH = Path("eval/gold_scenarios.yaml")
# The pack the fixtures and the gold scenarios are written for.
PACK = load_rule_pack(pack_path(DEFAULT_PACK))


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    """Every test gets its own database file; none touches `var/`."""
    path = tmp_path / "db" / "ubo.duckdb"
    monkeypatch.setenv(DB_ENV_VAR, str(path))
    return path


@pytest.fixture
def con():
    connection = connect()
    yield connection
    connection.close()


@pytest.fixture
def t0_copy(tmp_path):
    """A copy of the t0 fixtures that a test may change."""
    return Path(shutil.copytree(T0, tmp_path / "snapshot"))


@pytest.fixture(scope="session")
def ontology():
    return load_ontology(ONTOLOGY_PATH)


@pytest.fixture(scope="session")
def gold():
    return load_gold_scenarios(GOLD_PATH)


def build_graph(con, ontology, path=None):
    """Ingest a fixture directory and build Silver and Gold. Returns the set id."""
    snapshot_set = ingest_fixtures(con, path).snapshot_set
    build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    return snapshot_set.snapshot_set_id


@pytest.fixture
def graph_t0(con, ontology):
    """The t0 fixtures built up to Gold. Returns the set id."""
    return build_graph(con, ontology)


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
