"""Step 15: what reviewers accepted is published from the log as a set of its own,
the same rulings giving the same bytes, and from there it is graph data like any other."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ubo_sentinel.audit.extraction_review import review_proposal
from ubo_sentinel.audit.log import AuditLog, var_dir
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.extraction import NewEntity
from ubo_sentinel.pipeline.bronze import IngestError, resolve_set
from ubo_sentinel.pipeline.extraction.run import run_extraction
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders import document_loader
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.transformer import build_silver

AT = datetime(2026, 10, 1, tzinfo=UTC)
ACME = "lei:FXS01TARGET000000000"
QUILLON = "lei:FXS01PARENT000000000"
VOSTREK = "lei:FXS02TARGET000000000"
T1 = "fixtures/snapshot_t1"
TEXT = (
    "Ownership of Acme Trading FZE as at 30 September 2026.\n"
    "Quillon Harbour Group Ltd holds 45 % of the issued shares of Acme Trading FZE."
    " Ivo Marchetti holds 20 % of Acme Trading FZE."
)


@pytest.fixture
def world(fresh_con, ontology, tmp_path):
    con, (t0,) = fresh_con("t0")
    path = tmp_path / "ownership.txt"
    path.write_text(TEXT, encoding="utf-8")
    log = AuditLog()
    found, _ = run_extraction(
        log, ontology, path, ACME, t0, "ana", "analyst", AT, var_dir(), con=con
    )
    ids = {p.edge.owner_name: p.proposal_id for p in found.proposals}

    def rule(owner, action="ACCEPTED", **options):
        review_proposal(
            log, ontology, ids[owner], action, "rita", "reviewer", AT, var_dir(), con=con, **options
        )

    return con, t0, log, rule


def accept_both(rule):
    rule("Quillon Harbour Group Ltd", owner_id=QUILLON, as_of=date(2026, 9, 30))
    rule(
        "Ivo Marchetti", as_of=date(2026, 9, 30),
        owner_new=NewEntity(name="Ivo Marchetti", entity_type="Person", jurisdiction="IT"),
    )  # fmt: skip


def build(con, ontology, set_id):
    snapshot_set = resolve_set(con, set_id)
    build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    return snapshot_set


def test_nothing_accepted_is_nothing_to_publish(world):
    con, *_ = world
    with pytest.raises(IngestError, match="Nothing to publish"):
        document_loader.ingest_documents(con)


def test_the_same_rulings_give_the_same_files_and_the_same_set(world, tmp_path):
    con, t0, log, rule = world
    accept_both(rule)
    first = document_loader.write_files(document_loader.rows_from_log(log), tmp_path / "a")
    second = document_loader.write_files(document_loader.rows_from_log(log), tmp_path / "b")
    assert {d: p.read_bytes() for d, p in first.items()} == {
        d: p.read_bytes() for d, p in second.items()
    }
    assert b"\r" not in first["doc_relationships"].read_bytes()
    published = document_loader.ingest_documents(con)
    again = document_loader.ingest_documents(con, tmp_path / "elsewhere")
    assert published.snapshot_set.is_new and not again.snapshot_set.is_new
    assert again.snapshot_set.snapshot_set_id == published.snapshot_set.snapshot_set_id
    # A publish takes every other dataset from its base, and moves `latest` only.
    members = published.snapshot_set.snapshot_ids
    assert set(members) - set(resolve_set(con, t0).snapshot_ids) == {
        "doc_entities",
        "doc_relationships",
    }
    assert published.aliases == {"fixtures": t0, "latest": published.snapshot_set.snapshot_set_id}
    assert [(d.dataset, d.row_count) for d in published.datasets] == [
        ("doc_entities", 1), ("doc_relationships", 2),
    ]  # fmt: skip


def test_a_published_holding_is_graph_data_cited_to_its_document(world, ontology):
    con, _, log, rule = world
    accept_both(rule)
    published = document_loader.ingest_documents(con)
    snapshot_set = build(con, ontology, published.snapshot_set.snapshot_set_id)
    with GraphStore.open(snapshot_set.snapshot_set_id, con) as store:
        owners = {o.owner.legal_name: o for o in queries.get_direct_owners(store, ACME)}
        ivo = owners["Ivo Marchetti"]
        assert ivo.owner.entity_type == "Person" and ivo.relationship.pct == Decimal("20")
        assert ivo.relationship.provenance.source == "document"
        record = queries.get_source_record(store, ivo.relationship.provenance)
        assert record["quote"] == "Ivo Marchetti holds 20 % of Acme Trading FZE"
        assert record["reviewed_by"] == "rita" and record["review_event"]
        # The registry says 60 % and the document 45 %: the newer decides, and the
        # disagreement is a gap on the asset, naming the edge and both stakes.
        quillon = owners["Quillon Harbour Group Ltd"].relationship
        assert quillon.pct == Decimal("45")
        gaps = store.rows(
            "SELECT code, subject_id, detail FROM silver_gaps WHERE snapshot_set_id = ?",
            [store.set_id],
        )
    conflict = next(g for g in gaps if g["code"] == "CONFLICTING_PCT")
    assert conflict["subject_id"] == ACME and quillon.id in conflict["detail"]
    assert '"45.0000"' in conflict["detail"] and '"60.0000"' in conflict["detail"]


def test_a_later_ingest_on_a_published_set_keeps_its_documents(world, ontology):
    con, *_, rule = world
    accept_both(rule)
    published = document_loader.ingest_documents(con).snapshot_set
    later = ingest_fixtures(con, Path(T1), base=published.snapshot_set_id)
    kept = {d: later.snapshot_set.snapshot_ids[d] for d in ("doc_entities", "doc_relationships")}
    assert kept == {d: published.snapshot_ids[d] for d in kept}
    assert {d.dataset for d in later.datasets} >= {"doc_entities", "doc_relationships"}


def test_a_revoked_holding_leaves_the_next_publish(world, ontology):
    con, _, log, rule = world
    accept_both(rule)
    first = document_loader.ingest_documents(con).snapshot_set
    rule("Ivo Marchetti", "REVOKED", reason="the register was superseded")
    second = document_loader.ingest_documents(con).snapshot_set
    assert second.snapshot_set_id != first.snapshot_set_id
    rows = document_loader.rows_from_log(log)
    assert [row["subject_ref"] for row in rows["doc_relationships"]] == ["fx_registry:s01-parent"]
    assert rows["doc_entities"] == []
    # The base is the newest set of `latest`'s family, which is the first publish.
    assert second.base_set_id == first.snapshot_set_id
    rule("Quillon Harbour Group Ltd", "REVOKED", reason="no longer held")
    # Everything revoked: the publish carries empty files, so the edges leave too.
    third = document_loader.ingest_documents(con)
    assert [d.row_count for d in third.datasets] == [0, 0]


def test_a_real_publish_is_built_and_put_to_the_quality_check(world, monkeypatch):
    con, t0, *_, rule = world
    accept_both(rule)
    checked = []
    monkeypatch.setattr(document_loader, "FAMILY_REAL", "fixtures")
    monkeypatch.setattr(
        document_loader, "_accept_real", lambda con, summary: checked.append(summary)
    )
    document_loader.ingest_documents(con)
    assert len(checked) == 1
    required, optional = document_loader._family_datasets("real")
    assert "gleif_l1" in required and "doc_relationships" in optional
