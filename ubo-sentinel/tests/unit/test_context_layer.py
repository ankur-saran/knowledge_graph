"""The context layer (Step 14): addresses beside the graph, never in it."""

import shutil
from pathlib import Path

import pytest
from conftest import INV, build_graph
from real_format import SAMPLES

from ubo_sentinel.pipeline import context as context_module
from ubo_sentinel.pipeline.address import normalise_address
from ubo_sentinel.pipeline.bronze import ALIAS_FIXTURES, resolve_set
from ubo_sentinel.pipeline.context import (
    ContextError,
    ContextNotBuilt,
    build_context,
    context_digest,
)
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.gold import GOLD_TABLES, build_gold
from ubo_sentinel.pipeline.loaders.real_loader import ingest_all
from ubo_sentinel.pipeline.silver import SILVER_TABLES
from ubo_sentinel.pipeline.transformer import build_silver


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("12 Orchard Street, Suite 4", "12 ORCHARD ST. Ste 4"),
        ("P.O. Box 77, Nicosia", "PO Box 77 Nicosia"),
        ("Post Office Box 77, Nicosia", "P.O.Box 77 Nicosia"),
        ("Hauptstraße 5, Zürich", "HAUPTSTRASSE 5 ZURICH"),
        ("22 Kunaev Street, Room 5, Almaty", "22 Kunaev St., Rm 5, Almaty"),
    ],
)
def test_one_place_written_two_ways_is_one_address(a, b):
    assert normalise_address(a) == normalise_address(b) is not None


def test_numbers_and_word_order_tell_places_apart():
    # House 22, room 5 is not house 5, room 22.
    assert normalise_address("22 Kunaev St, Room 5") != normalise_address("5 Kunaev St, Room 22")
    assert normalise_address("12 Orchard St") != normalise_address("14 Orchard St")
    # A lone letter is a letter, not a post box.
    assert normalise_address("Unit P, 4 Quay") == "unit p 4 quay"
    for nothing in (None, "", "  ", "ab", "Box"):
        assert normalise_address(nothing) is None


def addresses(con, set_id):
    return con.execute(
        "SELECT entity_id, kind, address_norm, country FROM ctx_addresses"
        " WHERE snapshot_set_id = ? ORDER BY ALL",
        [set_id],
    ).fetchall()


def test_the_investigation_set_places_each_address_on_its_entity(fresh_con):
    con, (t0, inv) = fresh_con("t0_inv")
    rows = addresses(con, inv)
    kunaev = {e for e, _, norm, country in rows if norm == "22 kunaev st rm 5 almaty"}
    assert kunaev == {"lei:FXI03DESIG0000000000", "fx_list:i06-owner", "fx_list:s03-owner-a"}
    # The registry and the list wrote one Limassol office two ways.
    limassol = {e for e, _, norm, _ in rows if norm.startswith("14 archbishop makarios")}
    assert limassol == {"lei:FXI01TARGET000000000", "fx_list:i01-designated"}
    counts = context_module._load(con, inv)["counts"]
    # One record names no entity of the set: counted, never placed.
    assert (counts["unresolved"], counts["unreadable"]) == (1, 0)
    assert counts["ctx_addresses"] == len(rows)
    # t0 holds no address, and `fixtures` is still t0: the set without one is complete.
    assert addresses(con, t0) == []
    assert resolve_set(con, ALIAS_FIXTURES).snapshot_set_id == t0


def graph_rows(con, set_id):
    """Every Silver and Gold row of a set, but the set id each row carries."""
    return {
        table: con.execute(
            f"SELECT * EXCLUDE (snapshot_set_id) FROM {table}"
            " WHERE snapshot_set_id = ? ORDER BY ALL",
            [set_id],
        ).fetchall()
        for table in (*SILVER_TABLES, *GOLD_TABLES)
    }


def test_addresses_change_nothing_in_the_graph(fresh_con, ontology, tmp_path, monkeypatch):
    """A set with addresses is a new set, as any set is; the graph a screen reads in it is
    the one it would read without them, row for row. Sets without them are untouched."""
    con, (_, inv) = fresh_con("t0_inv")
    with_them = graph_rows(con, inv)
    digest = context_digest(con, inv)
    con.close()
    bare = shutil.copytree(INV, tmp_path / "bare")
    (bare / "addresses.csv").unlink()
    monkeypatch.setenv("UBO_DB", str(tmp_path / "other.duckdb"))
    other = connect()
    build_graph(other, ontology)
    without = build_graph(other, ontology, bare)
    assert without != inv
    assert graph_rows(other, without) == with_them
    assert addresses(other, without) == []
    # The same files in another database give the same context digest.
    assert build_graph(other, ontology, INV) == inv
    assert context_digest(other, inv) == digest
    other.close()


def test_a_second_build_changes_nothing_and_a_new_version_rebuilds(fresh_con, monkeypatch):
    con, (_, inv) = fresh_con("t0_inv")
    snapshot_set = resolve_set(con, inv)
    digest = context_digest(con, inv)
    again = build_context(con, snapshot_set)
    assert (again.status, again.context_digest) == ("already built", digest)
    monkeypatch.setattr(context_module, "CONTEXT_VERSION", context_module.CONTEXT_VERSION + 1)
    with pytest.raises(ContextNotBuilt, match="ubo build-graph"):
        context_digest(con, inv)
    rebuilt = build_context(con, snapshot_set)
    assert (rebuilt.status, rebuilt.context_digest) == ("rebuilt", digest)


def test_no_layer_without_a_current_graph(fresh_con):
    con, (t0, inv) = fresh_con("t0_inv")
    con.execute("DELETE FROM ctx_builds WHERE snapshot_set_id = ?", [inv])
    with pytest.raises(ContextNotBuilt, match="no current context layer"):
        context_digest(con, inv)
    con.execute("DELETE FROM gold_builds WHERE snapshot_set_id = ?", [t0])
    with pytest.raises(ContextNotBuilt, match="no current graph"):
        context_digest(con, t0)
    with pytest.raises(ContextError, match="no current graph"):
        build_context(con, resolve_set(con, t0))


def test_a_changed_address_row_is_refused(fresh_con):
    con, (_, inv) = fresh_con("t0_inv")
    con.execute("DELETE FROM ctx_builds")
    con.execute(
        "UPDATE bronze_addresses SET _raw = json_object('entity_ref', 'fx_registry:i01-target',"
        " 'kind', 'LEGAL', 'address', '1 Elsewhere Road', 'country', 'CY'),"
        " _row_hash = sha256('changed') WHERE _source_record_id = 'i01-addr-target'"
    )
    with pytest.raises(ContextError, match="not the rows"):
        build_context(con, resolve_set(con, inv))


def test_registry_and_list_addresses_from_the_published_files(con, ontology):
    snapshot_set = ingest_all(con, SAMPLES).snapshot_set
    assert {"gleif_l1_addresses", "os_addresses"} <= set(snapshot_set.snapshot_ids)
    build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    summary = build_context(con, snapshot_set)
    rows = addresses(con, snapshot_set.snapshot_set_id)
    kinds = {kind for _, kind, _, _ in rows}
    assert kinds == {"LEGAL", "HEADQUARTERS", "OTHER"}
    # The two list companies at one Dubai office, written two ways.
    dubai = {(norm, country) for _, kind, norm, country in rows if "gold tower" in norm}
    assert dubai == {("ofc 1204 gold tower jumeirah lakes towers dubai", "AE")}
    assert len([row for row in rows if "gold tower" in row[2]]) == 2
    assert summary.counts["unresolved"] == 0


def test_the_investigation_fixtures_are_t0_and_their_cases():
    """`snapshot_inv` adds rows to t0's and changes none, so every t0 scenario reads the same."""
    t0 = Path("fixtures/snapshot_t0")
    for name in ("entities.csv", "relationships.csv", "sanctions.csv"):
        ours, theirs = (INV / name).read_bytes(), (t0 / name).read_bytes()
        assert ours.startswith(theirs) and len(ours) > len(theirs), name
        added = ours[len(theirs) :].decode("utf-8").splitlines()
        assert all(line.split(",")[1].startswith("i0") for line in added), name
    assert not (INV / "repex.csv").exists()
    assert b"\r" not in (INV / "addresses.csv").read_bytes()
