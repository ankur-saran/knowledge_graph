"""The data-quality report, and the guard that keeps a shrunken feed off `real`."""

import json
import shutil

import pytest
from real_format import SAMPLES, restamp

from ubo_sentinel.pipeline.bronze import (
    ALIAS_LATEST,
    ALIAS_REAL,
    UnknownSnapshotSet,
    resolve_set,
)
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.real_loader import ingest_all
from ubo_sentinel.pipeline.quality import QualityError, check, quality_report
from ubo_sentinel.pipeline.transformer import build_silver

OS_PATH = "opensanctions/entities.ftm.json"


def build(con, ontology, path=SAMPLES):
    snapshot_set = ingest_all(con, path).snapshot_set
    build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    return snapshot_set


def without(tmp_path, keep, name="refresh"):
    """A copy of the sample refresh whose list keeps only the records `keep` accepts."""
    refresh = shutil.copytree(SAMPLES, tmp_path / name)
    path = refresh / OS_PATH
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if keep(record):
            kept.append(line)
    path.write_text("".join(kept), encoding="utf-8", newline="")
    restamp(refresh)
    return refresh


def test_the_report_counts_what_the_sources_held(con, ontology):
    report = quality_report(con, build(con, ontology))
    assert {name: d["rows"] for name, d in report["datasets"].items()} == {
        "gleif_l1": 20,
        # The same records as gleif_l1 and os_entities, read for their addresses (Step 14).
        "gleif_l1_addresses": 20,
        "gleif_repex": 10,
        "gleif_rr": 13,
        "os_addresses": 15,
        "os_entities": 16,
        "os_relationships": 8,
        "os_sanctions": 9,
    }
    assert report["family"] == "real"
    assert (report["designations"], report["active_designations"]) == (9, 7)
    # The target with no designation record, counted from Bronze.
    assert report["targets_without_designation"] == 1
    # Orvasko LLC and its OFAC record join by LEI; Sarvanta and Mirovane by name.
    joined = report["list_companies_joined"]
    assert (joined["LEI"], joined["FUZZY_NAME"]) == (2, 2)
    assert report["links_by_match_type"] == {"FUZZY_NAME": 2}
    assert sum(report["name_link_scores"].values()) == 2
    assert report["filtered"]["os_entities"] == {"UNSUPPORTED_SCHEMA": 1}
    assert report["unreadable"]["pct"] == 1
    assert "changes" not in report


def test_a_refused_record_is_in_the_report(con, ontology, tmp_path, monkeypatch):
    monkeypatch.setenv("UBO_REJECT_LIMIT", "0.2")
    refresh = shutil.copytree(SAMPLES, tmp_path / "refresh")
    with (refresh / OS_PATH).open("a", encoding="utf-8", newline="") as f:
        f.write("not json\n")
    restamp(refresh)
    report = quality_report(con, build(con, ontology, refresh))
    assert report["datasets"]["os_entities"]["rejects"] == {"UNREADABLE": 1}


def test_a_first_set_is_accepted_and_becomes_real(con, ontology):
    with pytest.raises(UnknownSnapshotSet):
        resolve_set(con, ALIAS_REAL)
    snapshot_set = build(con, ontology)
    # Ingested and built, but not checked: not real yet.
    with pytest.raises(UnknownSnapshotSet):
        resolve_set(con, ALIAS_REAL)
    result = check(con, snapshot_set)
    assert (result.accepted, result.shrunk, result.compared_with) == (True, [], None)
    assert resolve_set(con, ALIAS_REAL).snapshot_set_id == snapshot_set.snapshot_set_id


def test_a_set_whose_designations_fell_is_refused_until_accepted(con, ontology, tmp_path):
    first = build(con, ontology)
    check(con, first)
    # A truncated list: most designation records are gone.
    refresh = without(tmp_path, lambda r: r["schema"] != "Sanction" or "orv" in r["id"])
    later = build(con, ontology, refresh)

    refused = check(con, later)
    assert refused.compared_with == first.snapshot_set_id
    assert refused.shrunk == ["active_designations"] and not refused.accepted
    change = refused.report["changes"]["active_designations"]
    assert change["previous"] == 7 and change["current"] < 7 and change["change"] < -0.2
    # `latest` moved; `real` did not.
    assert resolve_set(con, ALIAS_LATEST).snapshot_set_id == later.snapshot_set_id
    assert resolve_set(con, ALIAS_REAL).snapshot_set_id == first.snapshot_set_id

    with pytest.raises(QualityError, match="needs a reason"):
        check(con, later, accept_shrink=True, reason=" ")
    accepted = check(con, later, accept_shrink=True, reason="OFAC delisted a programme")
    assert accepted.accepted and accepted.reason == "OFAC delisted a programme"
    assert resolve_set(con, ALIAS_REAL).snapshot_set_id == later.snapshot_set_id


def test_a_small_fall_is_accepted(con, ontology, tmp_path):
    check(con, build(con, ontology))
    refresh = without(tmp_path, lambda r: r["id"] != "os-fx-teverin-uk")
    result = check(con, build(con, ontology, refresh))
    assert result.accepted and result.shrunk == []
    assert result.report["changes"]["rows.os_sanctions"]["change"] == pytest.approx(
        -1 / 9, abs=1e-4
    )


def test_the_shrink_limit_can_be_set(con, ontology, tmp_path, monkeypatch):
    check(con, build(con, ontology))
    refresh = without(tmp_path, lambda r: r["id"] != "os-fx-teverin-uk")
    monkeypatch.setenv("UBO_SHRINK_LIMIT", "0.01")
    later = build(con, ontology, refresh)
    # Teverin's designation had ended: neither guarded count moved.
    assert check(con, later).accepted
    refresh = without(tmp_path, lambda r: r["id"] != "os-fx-orv-sdn", name="again")
    assert check(con, build(con, ontology, refresh)).shrunk == ["active_designations"]


def test_an_unbuilt_set_cannot_be_checked(con):
    snapshot_set = ingest_all(con, SAMPLES).snapshot_set
    with pytest.raises(QualityError, match="no Silver build"):
        check(con, snapshot_set)
