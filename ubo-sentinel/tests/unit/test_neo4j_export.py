"""The Neo4j export (Step 13.2): files for LOAD CSV, a live load, and what each role sees."""

import csv
import io
import json
import os

import pytest
from e2e import cli

from ubo_sentinel.graph.export import NODE_COLUMNS, read_graph
from ubo_sentinel.graph.export import masked_values as export_masked_values
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import MASK
from ubo_sentinel.pipeline.exporters import neo4j_export
from ubo_sentinel.pipeline.exporters.neo4j_export import LIST_SEPARATOR, push, render, write


@pytest.fixture
def store(fresh_con):
    con, (t0,) = fresh_con("t0")
    with GraphStore.open(t0, con) as opened:
        yield opened


def _rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


def test_the_export_holds_the_whole_graph_in_key_order(store, ontology):
    rows = read_graph(store, "reviewer", ontology)
    counts = store.counts
    assert len(rows.nodes) == counts["graph_nodes"]
    assert len(rows.edges) == counts["graph_edges"]
    assert len(rows.designations) == counts["graph_sanctions"]
    # Gold stores a link from each end; the export once per pair.
    assert len(rows.links) * 2 == counts["graph_links"]
    assert [r["id"] for r in rows.nodes] == sorted(r["id"] for r in rows.nodes)
    assert not rows.masked
    files = render(rows)
    nodes = _rows(files["nodes.csv"])
    assert list(nodes[0]) == ["snapshot_set_id", *NODE_COLUMNS]
    assert {row["snapshot_set_id"] for row in nodes} == {store.set_id}


def test_the_same_set_exports_the_same_bytes(store, ontology, tmp_path):
    first = write(read_graph(store, "analyst", ontology), tmp_path / "a")
    second = write(read_graph(store, "analyst", ontology), tmp_path / "b")
    assert first == second
    for name in neo4j_export.FILES + ("import.cypher", "manifest.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
        assert b"\r\n" not in (tmp_path / "a" / name).read_bytes()
    assert first["gold_digest"] == store.gold_digest
    assert first["snapshot_set_id"] == store.set_id


def test_an_analyst_s_export_shows_no_person(store, ontology):
    rows = read_graph(store, "analyst", ontology)
    assert rows.masked
    hidden = export_masked_values(rows, store, ontology)
    assert len(hidden) >= 8
    text = b"".join(render(rows).values()).decode("utf-8")
    assert [value for value in hidden if value in text] == []
    persons = [row for row in rows.nodes if row["label"] == "Person"]
    assert persons and all(row["legal_name"] == MASK for row in persons)
    assert all(row["id"].startswith("person:") for row in persons)
    # The edges and designations still reach the pseudonyms: the graph stays whole.
    ids = {row["id"] for row in rows.nodes}
    assert all({e["subject_id"], e["object_id"]} <= ids for e in rows.edges)
    assert all({d["entity_id"], d["designated_entity_id"]} <= ids for d in rows.designations)


def test_a_reviewer_s_export_differs_only_by_what_is_masked(store, ontology):
    analyst = render(read_graph(store, "analyst", ontology))
    reviewer = render(read_graph(store, "reviewer", ontology))
    assert analyst["import.cypher"] == reviewer["import.cypher"]
    assert analyst["nodes.csv"] != reviewer["nodes.csv"]
    assert json.loads(reviewer["manifest.json"])["masked"] is False


def test_a_role_that_sees_metadata_only_exports_nothing(store, ontology):
    with pytest.raises(PermissionError, match="metadata only"):
        read_graph(store, "engineer", ontology)
    with pytest.raises(PermissionError, match="Unknown role"):
        read_graph(store, "nobody", ontology)


def test_aliases_and_numbers_survive_the_csv(store, ontology):
    rows = read_graph(store, "reviewer", ontology)
    files = render(rows)
    with_aliases = next(r for r in rows.nodes if len(r["aliases"]) > 1)
    exported = next(r for r in _rows(files["nodes.csv"]) if r["id"] == with_aliases["id"])
    assert exported["aliases"].split(LIST_SEPARATOR) == with_aliases["aliases"]
    edge = next(r for r in rows.edges if r["pct"] is not None)
    exported = next(r for r in _rows(files["relationships.csv"]) if r["id"] == edge["id"])
    # The percentage as exact text, never a float's rounding.
    assert exported["pct"] == edge["pct"] and "." in exported["pct"]


def test_the_import_script_loads_every_file_with_keys(store, ontology):
    script = render(read_graph(store, "reviewer", ontology))["import.cypher"].decode()
    for name in neo4j_export.FILES:
        assert f"'file:///{name}'" in script
    for relation in ("OWNS", "CONSOLIDATED_BY", "CONTROLS"):
        assert f"[r:{relation} " in script
    assert script.count("CREATE CONSTRAINT") == 2
    assert script.count("{") == script.count("}") and script.count("(") == script.count(")")
    assert all(line.endswith(";") for line in script.splitlines() if not line.startswith("//"))


class FakeSession:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.log.append((query, params))
        return self

    def consume(self):
        return None

    def execute_write(self, work):
        return work(self)


class FakeDriver:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def session(self):
        return FakeSession(self.log)


def test_a_live_load_merges_on_the_keys_in_batches(store, ontology, monkeypatch):
    monkeypatch.setattr(neo4j_export, "BATCH", 10)
    rows = read_graph(store, "analyst", ontology)
    log, seen = [], {}

    def factory(uri, auth):
        seen.update(uri=uri, auth=auth)
        return FakeDriver(log)

    counts = push(
        rows, "bolt://example:7687", ("neo4j", "pw"), replace=True, driver_factory=factory
    )
    assert counts == rows.counts() and seen["uri"] == "bolt://example:7687"
    queries = [query for query, _ in log]
    assert queries[:2] == list(neo4j_export.CONSTRAINTS)
    assert queries[2] == neo4j_export.DELETE_SET and log[2][1] == {"set": rows.snapshot_set_id}
    loaded = [params for query, params in log if "UNWIND" in query and "MERGE (n:Entity" in query]
    assert sum(len(p["rows"]) for p in loaded) == len(rows.nodes)
    assert all(len(p["rows"]) <= 10 and p["set"] == rows.snapshot_set_id for p in loaded)
    # Every edge row is offered to each relation's statement, which keeps its own.
    per_relation = [p for q, p in log if "UNWIND [row IN $rows WHERE row.rel_type" in q]
    assert sum(len(p["rows"]) for p in per_relation) == 3 * len(rows.edges)
    assert all(isinstance(r["pct"], float | None) for p in per_relation for r in p["rows"])


def test_the_command_writes_files_under_the_var_directory(fresh_db):
    fresh_db("t0")
    result = cli("export", "neo4j", "--snapshot", "fixtures")
    assert result.exit_code == 0, result.output
    assert "(persons masked)" in result.output
    set_id = result.output.split()[3].rstrip(":")
    out = os.path.join(os.environ["UBO_VAR"], "export", set_id)
    manifest = json.loads(open(os.path.join(out, "manifest.json"), encoding="utf-8").read())
    assert manifest["export_digest"] in result.output


def test_the_command_refuses_what_it_cannot_do(fresh_db, monkeypatch, tmp_path):
    fresh_db("t0")
    assert cli("export", "neo4j", "--replace").exit_code == 2
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    result = cli("export", "neo4j", "--uri", "bolt://example:7687")
    assert result.exit_code == 2 and "NEO4J_PASSWORD" in result.output
    assert cli("export", "neo4j", "--role", "engineer").exit_code == 1
    assert cli("export", "neo4j", "--snapshot", "nope").exit_code == 2


def test_the_command_reports_a_database_that_refuses(fresh_db, monkeypatch):
    fresh_db("t0")
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")

    def unreachable(*args, **kwargs):
        raise ConnectionError("no server")

    monkeypatch.setattr(neo4j_export, "push", unreachable)
    result = cli("export", "neo4j", "--uri", "bolt://example:7687", "--role", "reviewer")
    assert result.exit_code == 1 and "ConnectionError" in result.output

    def missing(*args, **kwargs):
        raise ImportError("neo4j")

    monkeypatch.setattr(neo4j_export, "push", missing)
    result = cli("export", "neo4j", "--uri", "bolt://example:7687")
    assert result.exit_code == 2 and "uv sync --extra optional" in result.output

    monkeypatch.setattr(neo4j_export, "push", lambda rows, *a, **k: rows.counts())
    result = cli("export", "neo4j", "--uri", "bolt://example:7687")
    assert result.exit_code == 0 and "Loaded snapshot set" in result.output
