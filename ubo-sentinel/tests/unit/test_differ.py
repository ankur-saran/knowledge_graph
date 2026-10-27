"""The snapshot differ: what changed between two sets, in the fields the rules read."""

import json
import shutil

import pytest
from conftest import build_graph
from e2e import cli
from real_format import SAMPLES, restamp

from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.differ import DiffRefused, SetNotBuilt, diff_sets


def summary(diff):
    return {(c.domain, c.kind, c.cause, tuple(c.anchor_ids), tuple(c.fields)) for c in diff.changes}


def test_the_changed_list_is_one_added_one_removed_and_one_changed_designation(fresh_con):
    con, (t0, t1) = fresh_con("t0_t1")
    diff = diff_sets(con, t0, t1)
    assert summary(diff) == {
        ("designation", "ADDED", "LIST", ("lei:FXS01PARENT000000000",), ()),
        ("designation", "REMOVED", "LIST", ("fx_list:s08-owner",), ()),
        ("designation", "CHANGED", "LIST", ("fx_list:s17-owner",), ("is_active",)),
    }
    assert diff.counts() == {"LIST": {"ADDED": 1, "CHANGED": 1, "REMOVED": 1}}
    changed = next(c for c in diff.changes if c.kind == "CHANGED")
    assert (changed.before["is_active"], changed.after["is_active"]) == (False, True)
    # Every other row of t1 is a new snapshot's: provenance moved, and is never compared.
    assert diff_sets(con, t0, t0).changes == diff_sets(con, t1, t1).changes == []
    # Aliases resolve; the same two sets give the same digest.
    assert diff_sets(con, "fixtures", "latest").diff_digest == diff.diff_digest


def test_the_changed_graph_shows_a_stake_a_merge_and_a_lapsed_lei(fresh_con):
    con, (_, t1, t2) = fresh_con("t0_t1_t2")
    diff = diff_sets(con, t1, t2)
    owner, listed = "lei:FXS14OWNER0000000000", "fx_list:s14-listed"
    assert summary(diff) == {
        # SCEN-10: the designated party's stake in the intermediate, and the other owner's.
        ("edge", "CHANGED", "OWNERSHIP", ("lei:FXS10INTER0000000000",), ("pct",)),
        # SCEN-14: the list record states the owner's LEI, so the two are one entity.
        # The designation reached the owner by name; now it is the owner's own (LINK):
        # the record is in both sets, it reaches another entity.
        (
            "designation",
            "CHANGED",
            "LINK",
            (owner,),
            ("designated_node_id", "match_confidence", "match_type"),
        ),
        ("designation", "REMOVED", "LINK", (listed,), ()),
        ("entity", "REMOVED", "ENTITY", (listed,), ()),
        ("link", "REMOVED", "LINK", (listed, owner), ()),
        # SCEN-16: the parent's LEI lapsed.
        ("entity", "CHANGED", "ENTITY", ("lei:FXS16PARENT000000000",), ("lei_status",)),
    }
    stakes = sorted((c.before["pct"], c.after["pct"]) for c in diff.changes if c.domain == "edge")
    assert stakes == [("20.0000", "50.0000"), ("80.0000", "50.0000")]
    # The order and the ids are the data's, so a second run is byte for byte the same.
    assert canonical_json(diff) == canonical_json(diff_sets(con, t1, t2))


def test_two_databases_give_the_same_diff(fresh_con, ontology, tmp_path, monkeypatch):
    con, (t0, t1) = fresh_con("t0_t1")
    first = diff_sets(con, t0, t1)
    con.close()
    monkeypatch.setenv("UBO_DB", str(tmp_path / "other.duckdb"))
    other = connect()
    assert build_graph(other, ontology) == t0
    assert build_graph(other, ontology, "fixtures/snapshot_t1") == t1
    assert diff_sets(other, t0, t1) == first
    other.close()


def test_sets_built_by_other_code_are_not_compared(fresh_con):
    con, (t0, t1) = fresh_con("t0_t1")
    con.execute(
        "UPDATE silver_builds SET params = json_object('LINK_FLOOR', 0.5)"
        " WHERE snapshot_set_id = ?",
        [t0],
    )
    with pytest.raises(DiffRefused, match="normalize --rebuild --snapshot"):
        diff_sets(con, t0, t1)


def test_two_families_are_not_compared(fresh_con):
    con, (t0, t1) = fresh_con("t0_t1")
    con.execute("UPDATE snapshot_set_info SET family = 'real' WHERE snapshot_set_id = ?", [t1])
    with pytest.raises(DiffRefused, match="family"):
        diff_sets(con, t0, t1)


def test_a_set_not_built_names_the_command_to_run(fresh_con):
    con, (t0, t1) = fresh_con("t0_t1")
    con.execute("DELETE FROM gold_builds WHERE snapshot_set_id = ?", [t0])
    with pytest.raises(SetNotBuilt, match=f"build-graph --snapshot {t0}"):
        diff_sets(con, t0, t1)
    con.execute("DELETE FROM silver_builds WHERE snapshot_set_id = ?", [t0])
    with pytest.raises(SetNotBuilt, match=f"normalize --snapshot {t0}"):
        diff_sets(con, t0, t1)
    with pytest.raises(UnknownSnapshotSet):
        diff_sets(con, "nosuchset", t1)


def _build(path, source):
    for command in (
        ["ingest", "--source", source, "--path", path],
        ["normalize", "--snapshot", "latest"],
        ["build-graph", "--snapshot", "latest"],
    ):
        result = cli(*command)
        assert result.exit_code == 0, result.output


def test_a_designation_that_lapses_by_date_is_seen_though_its_row_is_unchanged(tmp_path):
    """OpenSanctions' `is_active` is the end date against the publish date: a later
    publish can end a designation whose published record has not changed by a byte."""
    first = shutil.copytree(SAMPLES, tmp_path / "first")
    ftm = first / "opensanctions/entities.ftm.json"
    # In force on the first publish date (2026-10-01): it ends on 2026-11-15.
    ftm.write_bytes(
        ftm.read_bytes().replace(b'"endDate":["2026-03-31"]', b'"endDate":["2026-11-15"]')
    )
    restamp(first)
    _build(first, "real")

    later = shutil.copytree(first, tmp_path / "later")
    manifest = json.loads((later / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["opensanctions_entities"]["published_at"] = "2026-12-01T06:00:00"
    (later / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # Another record changes, as one always does on a daily list; Teverin's does not.
    ftm = later / "opensanctions/entities.ftm.json"
    ftm.write_bytes(
        ftm.read_bytes().replace(b'"endDate":["2027-12-31"]', b'"endDate":["2028-12-31"]')
    )
    restamp(later)
    _build(later, "opensanctions")

    con = connect(read_only=True)
    sets = [
        resolve_set(con, set_id)
        for (set_id,) in con.execute(
            "SELECT snapshot_set_id FROM snapshot_set_info ORDER BY seq"
        ).fetchall()
    ]
    a, b = sets[0], sets[-1]
    hashes = con.execute(
        "SELECT _snapshot_id, _row_hash FROM bronze_sanctions"
        " WHERE _source_record_id = 'os-fx-teverin-uk' ORDER BY _snapshot_id"
    ).fetchall()
    diff = diff_sets(con, a.snapshot_set_id, b.snapshot_set_id)
    con.close()
    # Two snapshots, one row hash: a diff of Bronze would see nothing.
    assert len(hashes) == 2 and hashes[0][1] == hashes[1][1]
    lapsed = [c for c in diff.changes if c.fields == ["is_active"]]
    assert [(c.cause, c.before["is_active"], c.after["is_active"]) for c in lapsed] == [
        ("LIST", True, False)
    ]
    assert lapsed[0].anchor_ids == ["opensanctions:NK-fxTeverin"]
