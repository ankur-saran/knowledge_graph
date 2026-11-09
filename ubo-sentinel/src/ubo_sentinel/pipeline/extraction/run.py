"""One extraction: the document kept, the parser's and the model's proposals checked,
each labelled against the graph, and the run recorded once as an `EXTRACTED` event.

Nothing here writes to the graph. A proposal reaches it only when a reviewer
accepts it (`audit/extraction_review.py`) and an engineer publishes
(`pipeline/loaders/document_loader.py`).
"""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.llm.client import LLMClient
from ubo_sentinel.llm.config import ExtractBudget
from ubo_sentinel.llm.egress import DOCUMENT_EGRESS, DocumentEgress
from ubo_sentinel.models.audit_event import RUN_PREFIX
from ubo_sentinel.models.canonical import short_id
from ubo_sentinel.models.extraction import (
    LABEL_ORDER,
    Extraction,
    Label,
    ModelCall,
    Origin,
    Proposal,
    ProposedEdge,
    Suggestion,
)
from ubo_sentinel.models.ontology import OntologyConfig, require_command, require_write
from ubo_sentinel.models.relationship import Pct
from ubo_sentinel.pipeline.extraction import llm_extract
from ubo_sentinel.pipeline.extraction.document import DocumentText, store_document
from ubo_sentinel.pipeline.extraction.first_pass import first_pass
from ubo_sentinel.pipeline.extraction.guardrail import check_edges
from ubo_sentinel.pipeline.extraction.llm_extract import UNAVAILABLE

# A candidate at or above this names an entity for a label; a reviewer still picks.
SURE_MATCH = 0.92
MAX_CANDIDATES = 5


def persons_of(store: GraphStore) -> dict[str, list[str]]:
    """Each person of the set and every name the set holds for them."""
    rows = store.rows(
        "SELECT id, legal_name, aliases FROM graph_nodes"
        " WHERE snapshot_set_id = ? AND label = 'Person' ORDER BY id",
        [store.set_id],
    )
    return {row["id"]: [row["legal_name"], *(row["aliases"] or [])] for row in rows}


def _candidates(store: GraphStore, name: str, first: str | None = None) -> list[Suggestion]:
    found = [
        Suggestion(entity_id=m.entity_id, confidence=m.confidence, match_type=m.match_type)
        for m in queries.resolve_entity(store, name, max_candidates=MAX_CANDIDATES)
    ]
    # The document's subject leads when the name resolves to it.
    return sorted(found, key=lambda s: (s.entity_id != first, -s.confidence, s.entity_id))


def _sure(candidates: list[Suggestion]) -> str | None:
    return (
        candidates[0].entity_id if candidates and candidates[0].confidence >= SURE_MATCH else None
    )


def label_for(
    store: GraphStore,
    edge: ProposedEdge,
    owners: list[Suggestion],
    assets: list[Suggestion],
) -> tuple[Label, Pct | None]:
    """What the proposal would do to the graph, and the stake on record for its pair."""
    if edge.holding == "indirect":
        return "INDIRECT_INFO", None
    if edge.rel_type == "OWNS" and edge.interest != "equity":
        return "INTEREST_INFO", None
    owner, asset = _sure(owners), _sure(assets)
    if owner is None or asset is None:
        return "NEW", None
    for item in queries.get_direct_owners(store, asset):
        relationship = item.relationship
        if (
            item.owner.id != owner
            or relationship.rel_type != edge.rel_type
            or relationship.is_ultimate
        ):
            continue
        if edge.rel_type == "CONTROLS":
            return "CONFIRMS", None
        if relationship.pct is None:
            return "FILLS_NULL_PCT", None
        return ("CONFIRMS" if relationship.pct == edge.pct else "CONFLICTS"), relationship.pct
    return "NEW", None


def _same(edge: ProposedEdge) -> tuple[Any, ...]:
    return (
        edge.owner_name.casefold(),
        edge.asset_name.casefold(),
        edge.rel_type,
        edge.pct,
        edge.page,
    )


def proposals_for(
    store: GraphStore,
    document: DocumentText,
    subject_id: str,
    found: list[tuple[Origin, ProposedEdge]],
) -> tuple[list[Proposal], list[str]]:
    """The proposals that pass the guardrail, labelled; the kinds of fault of the rest.

    A holding both proposers found is kept once, as the parser's.
    """
    proposals: list[Proposal] = []
    faults: list[str] = []
    seen: set[tuple[Any, ...]] = set()
    for origin in ("first_pass", "model"):
        edges = [edge for kind, edge in found if kind == origin]
        kept, dropped = check_edges(edges, document.pages)
        faults += dropped
        for edge, span in kept:
            if _same(edge) in seen:
                continue
            seen.add(_same(edge))
            owners = _candidates(store, edge.owner_name)
            assets = _candidates(store, edge.asset_name, first=subject_id)
            label, existing = label_for(store, edge, owners, assets)
            proposal_id = short_id(
                [document.sha256, document.text_digest, origin, edge.model_dump(mode="json")]
            )
            proposals.append(
                Proposal(
                    proposal_id=proposal_id,
                    origin=origin,
                    edge=edge,
                    span=span,
                    label=label,
                    owner_candidates=owners,
                    asset_candidates=assets,
                    existing_pct=existing,
                )
            )
    proposals.sort(key=lambda p: (LABEL_ORDER[p.label], p.span.page, p.span.start, p.proposal_id))
    return proposals, faults


def extract(
    store: GraphStore,
    document: DocumentText,
    subject_id: str,
    client: LLMClient | None = None,
    budget: ExtractBudget | None = None,
    consent: bool = False,
    max_tokens: int = 4000,
    investigation_id: str | None = None,
) -> Extraction:
    """Read one stored document about `subject_id` on the store's set. Writes nothing.

    The parser always runs. A model is asked only with a client, a budget and the
    analyst's consent for this document. Raises `LookupError` for a subject the
    set does not hold.
    """
    subject = queries.get_entity_by_id(store, subject_id)
    if subject is None:
        raise LookupError(f"No entity {subject_id} in snapshot set {store.set_id}.")
    found: list[tuple[Origin, ProposedEdge]] = [
        ("first_pass", edge) for edge in first_pass(document.pages, subject.legal_name)
    ]
    calls: list[ModelCall] = []
    faults: list[str] = []
    not_read: list[int] = []
    asked = client is not None and budget is not None and consent
    if asked:
        assert client is not None and budget is not None
        egress = DocumentEgress(document.pages, persons_of(store), consent)
        calls, edges, faults, not_read = llm_extract.ask_model(
            client, egress, len(document.pages), subject.legal_name, budget, max_tokens
        )
        found += [("model", edge) for edge in edges]
    proposals, dropped = proposals_for(store, document, subject_id, found)
    faults = sorted([*faults, *dropped])
    model = budget.model if asked and budget is not None else None
    egress_kind = DOCUMENT_EGRESS if asked else "none"
    extraction_id = short_id(
        [
            document.sha256,
            document.text_digest,
            subject_id,
            store.set_id,
            egress_kind,
            model,
            llm_extract.PROMPT_VERSION,
            [call.model_dump(mode="json") for call in calls],
            [proposal.proposal_id for proposal in proposals],
        ]
    )
    return Extraction(
        extraction_id=extraction_id,
        document_sha256=document.sha256,
        file_name=document.file_name,
        text_digest=document.text_digest,
        extractor=document.extractor,
        page_count=len(document.pages),
        subject_id=subject_id,
        snapshot_set_id=store.set_id,
        graph_digest=store.gold_digest,
        investigation_id=investigation_id,
        egress=egress_kind,
        model=model,
        prompt_version=llm_extract.PROMPT_VERSION,
        prompt_sha256=llm_extract.prompt_digest(),
        budgets=asdict(budget) | {"request_max_tokens": max_tokens} if asked and budget else {},
        calls=calls,
        pages_not_read=not_read,
        proposals=proposals,
        faults=faults,
        dropped=len(dropped),
        complete=not not_read and UNAVAILABLE not in faults,
    )


class _OnRecord(Exception):
    pass


def record(
    log: AuditLog,
    ontology: OntologyConfig,
    extraction: Extraction,
    actor: str,
    role: str,
    created_at: datetime,
) -> bool:
    """Append the one `EXTRACTED` event. False when the same run is on record."""

    def not_twice(records: list[dict[str, Any]]) -> None:
        if any(event["decision_id"] == extraction.event_id for event in records):
            raise _OnRecord

    try:
        log.append(
            ontology=ontology,
            decision_id=extraction.event_id,
            event_type="EXTRACTED",
            actor=actor,
            role=role,
            created_at=created_at,
            payload={"extraction": extraction.model_dump(mode="json")},
            guard=not_twice,
        )
    except _OnRecord:
        return False
    return True


def run_extraction(
    log: AuditLog,
    ontology: OntologyConfig,
    path: Path,
    subject_id: str,
    snapshot: str,
    actor: str,
    role: str,
    created_at: datetime,
    root: Path,
    con: duckdb.DuckDBPyConnection | None = None,
    client: LLMClient | None = None,
    budget: ExtractBudget | None = None,
    consent: bool = False,
    max_tokens: int = 4000,
    investigation_id: str | None = None,
) -> tuple[Extraction, bool]:
    """(the extraction, whether this call recorded it). A role that may not extract or
    write is refused before the file is read or a model asked.

    Raises `PermissionError`, `LookupError` (an unknown subject or investigation),
    `DocumentRefused` and what opening the set raises.
    """
    require_command(ontology, role, "extract")
    require_write(ontology, role)
    if investigation_id is not None:
        wanted = f"{RUN_PREFIX['INVESTIGATED']}{investigation_id}"
        if not any(event["decision_id"] == wanted for event in log.records()):
            raise LookupError(f"No investigation {investigation_id} in the audit log.")
    document = store_document(path, root)
    with GraphStore.open(snapshot, con) as store:
        extraction = extract(
            store, document, subject_id, client, budget, consent, max_tokens, investigation_id
        )
    return extraction, record(log, ontology, extraction, actor, role, created_at)


def extractions_of(records: list[dict[str, Any]]) -> list[Extraction]:
    """Every extraction on record, in the order recorded."""
    return [
        Extraction.model_validate(record["payload"]["extraction"])
        for record in records
        if record["event_type"] == "EXTRACTED"
    ]
