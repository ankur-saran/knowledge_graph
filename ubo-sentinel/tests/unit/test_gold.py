"""Gold: edge direction, degrees, the build registry and its digest."""

import pytest
from conftest import build_graph
from typer.testing import CliRunner

from ubo_sentinel.cli.app import app
from ubo_sentinel.pipeline import gold, transformer
from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.db import DB_ENV_VAR, connect
from ubo_sentinel.pipeline.gold import (
    GOLD_TABLES,
    GoldError,
    build_gold,
    create_schema,
    is_built,
    load_build,
)
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.silver import read_relationships
from ubo_sentinel.pipeline.transformer import build_silver

HUB = "lei:FXS00HUB000000000000"

runner = CliRunner()


def count(con, table, set_id):
    return con.execute(
        f"SELECT count(*) FROM {table} WHERE snapshot_set_id = ?", [set_id]
    ).fetchone()[0]


def test_nodes_edges_and_designations_are_silvers(con, graph_t0):
    for gold_table, silver_table in (
        ("graph_nodes", "silver_entities"),
        ("graph_edges", "silver_relationships"),
        ("graph_sanctions", "silver_sanctions"),
    ):
        assert count(con, gold_table, graph_t0) == count(con, silver_table, graph_t0) > 0
    # Each link is stored once from each end.
    assert count(con, "graph_links", graph_t0) == 2 * count(con, "silver_entity_links", graph_t0)


def test_upper_is_the_owner_parent_or_controller(con, graph_t0):
    ends = {
        edge_id: (upper, lower)
        for edge_id, upper, lower in con.execute(
            "SELECT id, upper_id, lower_id FROM graph_edges WHERE snapshot_set_id = ?", [graph_t0]
        ).fetchall()
    }
    relations = set()
    for edge in read_relationships(con, graph_t0):
        relations.add(edge.rel_type)
        if edge.rel_type == "CONSOLIDATED_BY":
            assert ends[edge.id] == (edge.object_id, edge.subject_id)
        else:
            assert ends[edge.id] == (edge.subject_id, edge.object_id)
    assert relations == {"OWNS", "CONSOLIDATED_BY", "CONTROLS"}


def test_degrees_count_the_edges_above_and_below_a_node(con, graph_t0):
    degrees = dict(
        (node_id, (up, down))
        for node_id, up, down in con.execute(
            "SELECT id, up_degree, down_degree FROM graph_nodes WHERE snapshot_set_id = ?",
            [graph_t0],
        ).fetchall()
    )
    # Seven direct children, two ultimate ones, and a founder who controls it.
    assert degrees[HUB] == (1, 9)
    assert degrees["fx_list:s00-founder"] == (0, 1)
    assert sum(up for up, _ in degrees.values()) == count(con, "graph_edges", graph_t0)


def test_every_name_is_under_each_of_its_blocks(con, graph_t0):
    blocks = {
        block
        for (block,) in con.execute(
            "SELECT block FROM graph_name_blocks WHERE snapshot_set_id = ? AND name = ?",
            [graph_t0, "Acme Trading FZE"],
        ).fetchall()
    }
    # The legal form is not part of the normalised name.
    assert blocks == {"acme", "trad"}


def test_building_a_current_graph_changes_nothing(con, graph_t0):
    snapshot_set = resolve_set(con, graph_t0)
    before = con.execute("SELECT * FROM gold_builds").fetchall()
    again = build_gold(con, snapshot_set)
    assert again.status == "already built"
    assert again.gold_digest == load_build(con, graph_t0)["gold_digest"]
    assert con.execute("SELECT * FROM gold_builds").fetchall() == before


def test_a_graph_is_rebuilt_when_silver_changes(con, graph_t0, ontology, monkeypatch):
    snapshot_set = resolve_set(con, graph_t0)
    before = load_build(con, graph_t0)

    # A rebuild that gives the same Silver leaves the graph current.
    build_silver(con, snapshot_set, ontology, rebuild=True)
    assert is_built(con, graph_t0)

    # A higher link floor drops the review-band link: another Silver, so the graph is stale.
    monkeypatch.setattr(transformer, "LINK_FLOOR", 0.9)
    build_silver(con, snapshot_set, ontology, rebuild=True)
    assert not is_built(con, graph_t0)

    rebuilt = build_gold(con, snapshot_set)
    assert rebuilt.status == "rebuilt"
    assert rebuilt.gold_digest != before["gold_digest"]
    assert rebuilt.counts["graph_links"] < before["counts"]["graph_links"]
    assert is_built(con, graph_t0)


def test_the_same_files_give_the_same_graph_in_another_database(
    con, graph_t0, ontology, tmp_path, monkeypatch
):
    monkeypatch.setenv(DB_ENV_VAR, str(tmp_path / "other.duckdb"))
    other = connect()
    try:
        assert build_graph(other, ontology) == graph_t0
        assert (
            load_build(other, graph_t0)["gold_digest"] == load_build(con, graph_t0)["gold_digest"]
        )
    finally:
        other.close()


def test_a_graph_needs_silver(con):
    snapshot_set = ingest_fixtures(con).snapshot_set
    with pytest.raises(GoldError, match="ubo normalize"):
        build_gold(con, snapshot_set)


def test_an_edge_whose_end_is_not_a_node_fails_the_build(con, ontology):
    snapshot_set = ingest_fixtures(con).snapshot_set
    build_silver(con, snapshot_set, ontology)
    con.execute("DELETE FROM silver_entities WHERE id = ?", [HUB])

    with pytest.raises(GoldError, match="not a node"):
        build_gold(con, snapshot_set)
    for table in (*GOLD_TABLES, "gold_builds"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_two_rows_with_one_key_refuse_the_build(con, ontology, monkeypatch):
    """Gold has no PRIMARY KEY (it made a rebuild's delete slow); the build checks the keys."""
    snapshot_set = ingest_fixtures(con).snapshot_set
    build_silver(con, snapshot_set, ontology)
    write_links = gold._write_links

    def twice(con, set_id):
        write_links(con, set_id)
        write_links(con, set_id)

    monkeypatch.setattr(gold, "_write_links", twice)
    with pytest.raises(GoldError, match="graph_links has .* rows that share a key"):
        build_gold(con, snapshot_set)
    for table in (*GOLD_TABLES, "gold_builds"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_a_rebuild_leaves_one_row_per_key(con, graph_t0, ontology, monkeypatch):
    snapshot_set = resolve_set(con, graph_t0)
    monkeypatch.setattr(transformer, "LINK_FLOOR", 0.9)
    build_silver(con, snapshot_set, ontology, rebuild=True)
    rebuilt = build_gold(con, snapshot_set)
    assert rebuilt.status == "rebuilt"
    assert rebuilt.counts == {table: count(con, table, graph_t0) for table in GOLD_TABLES}
    assert count(con, "graph_nodes", graph_t0) == count(con, "silver_entities", graph_t0)


def test_gold_made_with_keys_and_indexes_is_dropped_and_built_again(con, graph_t0):
    con.execute("DROP TABLE graph_sanctions")
    con.execute(
        "CREATE TABLE graph_sanctions (snapshot_set_id TEXT NOT NULL, id TEXT NOT NULL,"
        " PRIMARY KEY (snapshot_set_id, id))"
    )
    con.execute("CREATE INDEX graph_nodes_old ON graph_nodes (snapshot_set_id, lei)")
    create_schema(con)
    assert not is_built(con, graph_t0)
    assert con.execute(
        "SELECT (SELECT count(*) FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'"
        "        AND table_name LIKE 'graph_%'),"
        " (SELECT count(*) FROM duckdb_indexes() WHERE table_name LIKE 'graph_%')"
    ).fetchone() == (0, 0)
    assert build_gold(con, resolve_set(con, graph_t0)).status == "built"


# --- the command ---------------------------------------------------------------------


def test_build_graph_command_prints_counts_and_is_idempotent():
    assert runner.invoke(app, ["ingest", "--source", "fixtures"]).exit_code == 0
    assert runner.invoke(app, ["normalize"]).exit_code == 0

    first = runner.invoke(app, ["build-graph"])
    assert first.exit_code == 0, first.output
    assert "(built)" in first.output and "Digest: " in first.output
    for table in GOLD_TABLES:
        assert table in first.output

    assert "Context: built, 0 addresses" in first.output

    second = runner.invoke(app, ["build-graph"])
    assert second.exit_code == 0
    assert "(already built)" in second.output and "Context: already built" in second.output
    lines = second.output.replace("already built", "built").splitlines()[1:]
    assert lines == first.output.splitlines()[1:]


def test_build_graph_command_refuses_what_it_cannot_build():
    unknown = runner.invoke(app, ["build-graph"])
    assert unknown.exit_code == 2
    assert "ubo ingest" in unknown.output

    assert runner.invoke(app, ["ingest", "--source", "fixtures"]).exit_code == 0
    unbuilt = runner.invoke(app, ["build-graph"])
    assert unbuilt.exit_code == 1
    assert "ubo normalize" in unbuilt.output


def test_build_graph_command_is_for_engineers():
    assert runner.invoke(app, ["build-graph", "--role", "analyst"]).exit_code == 1
