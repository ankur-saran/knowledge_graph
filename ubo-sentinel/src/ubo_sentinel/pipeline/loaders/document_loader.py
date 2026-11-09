"""Publish what reviewers accepted from documents (Step 15): `ubo ingest --source documents`.

The audit log is the record. A publish folds it and writes every accepted,
unrevoked item, in proposal-id order, into two files of the fixture shape:
`doc_entities.csv` (holders a reviewer created) and `doc_relationships.csv`
(holdings), each row with the document, page, span, quote and the review event
that accepted it. The files' bytes give the snapshot ids, so the same rulings
give the same set on any machine; a set holds one snapshot of each, so a publish
always carries every ruling, and a revoked one is simply left out.

The set is made on a base of its own family (default: the newest set of
`latest`'s family), which supplies every other dataset. For the real family the
set is built and put to the quality check (10.5) here, as `ubo refresh` does, so
`real` moves only when the check accepts it.
"""

import csv
from pathlib import Path

import duckdb

from ubo_sentinel.audit.extraction_review import ProposalState, accepted
from ubo_sentinel.audit.log import AuditLog, var_dir
from ubo_sentinel.models.extraction import EndChoice
from ubo_sentinel.models.fixture_rows import DocEntityRow, DocRelationshipRow, _Row
from ubo_sentinel.models.ontology import load_ontology, ontology_path
from ubo_sentinel.pipeline.bronze import (
    ALIAS_LATEST,
    FAMILY_FIXTURES,
    FAMILY_REAL,
    IngestError,
    SnapshotManager,
    UnknownSnapshotSet,
    resolve_set,
    transaction,
)
from ubo_sentinel.pipeline.loaders.base import DatasetSummary, IngestSummary, resolve_alias
from ubo_sentinel.pipeline.loaders.fixture_loader import DATASETS as FIXTURE_DATASETS
from ubo_sentinel.pipeline.loaders.fixture_loader import _read_records, check_stable_bytes
from ubo_sentinel.pipeline.loaders.real_loader import REAL_DATASETS, REAL_OPTIONAL_DATASETS
from ubo_sentinel.pipeline.sources import DOCUMENT_DATASETS

ENTITIES, RELATIONSHIPS = DOCUMENT_DATASETS
FILES = {ENTITIES: "doc_entities.csv", RELATIONSHIPS: "doc_relationships.csv"}
TABLES = {ENTITIES: "bronze_entities", RELATIONSHIPS: "bronze_relationships"}
MODELS: dict[str, type[_Row]] = {ENTITIES: DocEntityRow, RELATIONSHIPS: DocRelationshipRow}
SOURCE = "document"


def published_dir() -> Path:
    return var_dir() / "documents" / "published"


def _family_datasets(family: str) -> tuple[list[str], list[str]]:
    """(the datasets a set of the family must hold, those it may)."""
    if family == FAMILY_FIXTURES:
        required = [d.name for d in FIXTURE_DATASETS.values() if not d.optional]
        optional = [d.name for d in FIXTURE_DATASETS.values() if d.optional]
    else:
        required, optional = list(REAL_DATASETS), list(REAL_OPTIONAL_DATASETS)
    return required, [*optional, *DOCUMENT_DATASETS]


def _new_entity_row(state: ProposalState, end: EndChoice) -> dict[str, str | None]:
    assert state.acceptance is not None and end.new_entity is not None
    new = end.new_entity
    return {
        "source": SOURCE,
        "source_record_id": end.ref.removeprefix(f"{SOURCE}:"),
        "as_of": state.acceptance.as_of.isoformat(),
        "confidence": "1.0",
        "lei": None,
        "registration_authority_id": None,
        "registration_number": None,
        "legal_name": new.name,
        "aliases": None,
        "jurisdiction": new.jurisdiction,
        "status": "UNKNOWN",
        "lei_status": None,
        "entity_type": new.entity_type,
        "document_sha256": state.extraction.document_sha256,
        "page": str(state.proposal.span.page),
        "proposal_id": state.proposal.proposal_id,
        "review_event": state.accepted_event,
    }


def _relationship_row(state: ProposalState) -> dict[str, str | None]:
    acceptance, proposal = state.acceptance, state.proposal
    assert acceptance is not None and state.reviewed_by and state.accepted_event
    return {
        "source": SOURCE,
        "source_record_id": f"{state.extraction.document_sha256[:12]}:p{proposal.span.page}"
        f":{proposal.proposal_id}",
        "as_of": acceptance.as_of.isoformat(),
        "confidence": "1.0",
        "subject_ref": acceptance.owner.ref,
        "object_ref": acceptance.asset.ref,
        "rel_type": acceptance.rel_type,
        "pct": None if acceptance.pct is None else str(acceptance.pct),
        "basis": acceptance.basis,
        "is_ultimate": "false",
        "document_sha256": state.extraction.document_sha256,
        "page": str(proposal.span.page),
        "span_start": str(proposal.span.start),
        "span_end": str(proposal.span.end),
        "quote": proposal.edge.quote,
        "proposal_id": proposal.proposal_id,
        "reviewed_by": state.reviewed_by,
        "review_event": state.accepted_event,
    }


def rows_from_log(log: AuditLog) -> dict[str, list[dict[str, str | None]]]:
    """Dataset -> the rows a publish writes, from the rulings on record."""
    states = accepted(log.records())
    entities: dict[str, dict[str, str | None]] = {}
    relationships = []
    for state in states:
        assert state.acceptance is not None
        for end in (state.acceptance.owner, state.acceptance.asset):
            if end.new_entity is not None:
                entities[end.ref] = _new_entity_row(state, end)
        relationships.append(_relationship_row(state))
    return {
        ENTITIES: [entities[ref] for ref in sorted(entities)],
        RELATIONSHIPS: sorted(relationships, key=lambda row: row["source_record_id"] or ""),
    }


def write_files(rows: dict[str, list[dict[str, str | None]]], directory: Path) -> dict[str, Path]:
    """The two files, in bytes that depend only on the rows: UTF-8, LF, one header."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for dataset, file_name in FILES.items():
        path = directory / file_name
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=list(MODELS[dataset].model_fields), lineterminator="\n"
            )
            writer.writeheader()
            for row in rows[dataset]:
                writer.writerow({key: "" if value is None else value for key, value in row.items()})
        paths[dataset] = path
    return paths


def _accept_real(con: duckdb.DuckDBPyConnection, summary: IngestSummary) -> None:
    """Build the new real set and put it to the quality check, as `ubo refresh` does."""
    from ubo_sentinel.pipeline.context import build_context
    from ubo_sentinel.pipeline.gold import build_gold
    from ubo_sentinel.pipeline.quality import check
    from ubo_sentinel.pipeline.transformer import build_silver

    ontology = load_ontology(ontology_path())
    build_silver(con, summary.snapshot_set, ontology)
    build_gold(con, summary.snapshot_set)
    build_context(con, summary.snapshot_set)
    check(con, summary.snapshot_set)


def ingest_documents(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    """Publish the accepted items into a new set on `base` (default: `latest`).

    `path` is where the two files are written (default `<UBO_VAR>/documents/published/`).
    Refused: a log with nothing ever accepted.
    """
    log = AuditLog()
    rows = rows_from_log(log)
    if not rows[RELATIONSHIPS] and not any(
        record["event_type"] == "EXTRACTION_REVIEWED" and record["payload"]["action"] == "REVOKED"
        for record in log.records()
    ):
        raise IngestError("Nothing to publish: no holding from a document has been accepted.")
    try:
        base_set = resolve_set(con, base or ALIAS_LATEST)
    except UnknownSnapshotSet as exc:
        raise IngestError(str(exc)) from exc
    directory = path or published_dir()
    paths = write_files(rows, directory)
    records = {}
    for dataset, file_path in paths.items():
        check_stable_bytes(file_path)
        records[dataset] = _read_records(file_path, _DocDataset(dataset))

    required, optional = _family_datasets(base_set.family)
    manager = SnapshotManager(con)
    status: dict[str, str] = {}
    supplied: dict[str, str] = {}
    with transaction(con):
        for dataset, file_path in paths.items():
            snapshot = manager.open_snapshot(dataset, base_set.family, TABLES[dataset], file_path)
            if snapshot.is_new:
                manager.insert_rows(snapshot, records[dataset])
                manager.register_snapshot(snapshot)
            supplied[dataset] = snapshot.snapshot_id
            status[dataset] = "new" if snapshot.is_new else "already present"
        snapshot_set = manager.commit_set(
            base_set.family, supplied, required, base=base_set.snapshot_set_id, optional=optional
        )
    counts = dict(con.execute("SELECT snapshot_id, row_count FROM bronze_snapshots").fetchall())
    summary = IngestSummary(
        source=SOURCE,
        path=directory,
        datasets=[
            DatasetSummary(
                dataset,
                TABLES[dataset],
                supplied[dataset],
                counts[supplied[dataset]],
                status[dataset],
            )
            for dataset in DOCUMENT_DATASETS
        ],
        snapshot_set=snapshot_set,
        aliases={},
    )
    if base_set.family == FAMILY_REAL and snapshot_set.is_new:
        _accept_real(con, summary)
    aliases = (
        ("fixtures", ALIAS_LATEST) if base_set.family == FAMILY_FIXTURES else (ALIAS_LATEST, "real")
    )
    return IngestSummary(
        source=summary.source,
        path=summary.path,
        datasets=summary.datasets,
        snapshot_set=snapshot_set,
        aliases={alias: resolve_alias(con, alias) for alias in aliases},
    )


class _DocDataset:
    """What `fixture_loader._read_records` reads of a dataset: its row model."""

    def __init__(self, dataset: str) -> None:
        self.model = MODELS[dataset]
