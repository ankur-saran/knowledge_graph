"""Fixtures shared by the test modules."""

import shutil
import socket
from collections import defaultdict
from pathlib import Path

import duckdb
import pytest
from publishers import Publishers

from ubo_sentinel.audit.log import VAR_ENV_VAR
from ubo_sentinel.cli.app import ONTOLOGY_PATH
from ubo_sentinel.download import http
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
    """Every test gets its own database file and audit log; none touches `var/`."""
    path = tmp_path / "db" / "ubo.duckdb"
    monkeypatch.setenv(DB_ENV_VAR, str(path))
    monkeypatch.setenv(VAR_ENV_VAR, str(tmp_path / "var"))
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test opens a socket: downloading is separate from everything tested here."""

    def refuse(*args, **kwargs):
        raise RuntimeError("The tests do not use the network.")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, refuse)


@pytest.fixture
def publishers(monkeypatch):
    """GLEIF and OpenSanctions on a mock transport, for `ubo download` and `ubo refresh`."""
    served = Publishers()
    monkeypatch.setattr(http, "http_client", served.client)
    return served


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


@pytest.fixture(scope="session")
def templates(tmp_path_factory, ontology):
    """Databases built once per session: `t0`, and `t0_t1` (the t1 list loaded after t0).

    Returns name -> (database file, the set ids it holds in the order built).
    Ids are content-derived, so a copy of a template is the database a test
    would have built itself, in a fraction of the time.
    """
    built = {}
    root = tmp_path_factory.mktemp("templates")

    def get(name):
        if name not in built:
            path = root / f"{name}.duckdb"
            connection = duckdb.connect(str(path))
            paths = {"t0": [None], "t0_t1": [None, T1]}[name]
            set_ids = [build_graph(connection, ontology, item) for item in paths]
            connection.close()
            built[name] = (path, set_ids)
        return built[name]

    return get


@pytest.fixture
def fresh_db(database, templates):
    """Copy a template to this test's database. Returns the set ids it holds."""

    def copy(name="t0"):
        path, set_ids = templates(name)
        database.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, database)
        return set_ids

    return copy


@pytest.fixture
def fresh_con(fresh_db):
    """Copy a template, then open it: (connection, set ids). Not with `con`, which
    opens the file first (Windows will not replace an open file)."""
    opened = []

    def open_(name="t0"):
        set_ids = fresh_db(name)
        opened.append(connect())
        return opened[-1], set_ids

    yield open_
    for connection in opened:
        connection.close()
