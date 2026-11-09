"""A person's ruling on a proposed holding (Step 15): accept, reject, or later revoke.

The queue is a fold of the log, never kept as state. Each ruling is one
`EXTRACTION_REVIEWED` event, and the log refuses one that cannot follow (a
second ruling, a revocation of what was not accepted, a reviewer who extracted
the proposal). An acceptance names both ends: an entity of the set, by the ref of
the record that describes it, or a holder the reviewer creates. Nothing here
writes to the graph: an engineer publishes the accepted items.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.audit_event import PROPOSAL_PREFIX, AuditEvent
from ubo_sentinel.models.entity import EntityType
from ubo_sentinel.models.extraction import (
    LABEL_ORDER,
    Acceptance,
    DocBasis,
    DocRelType,
    EndChoice,
    Extraction,
    NewEntity,
    Proposal,
    ReviewAction,
)
from ubo_sentinel.models.ontology import OntologyConfig, require_command
from ubo_sentinel.pipeline.extraction.document import load_document
from ubo_sentinel.pipeline.extraction.guardrail import check_edge

DOCUMENT_SOURCE = "document"


class ExtractionReviewError(ValueError):
    """The ruling cannot be made as asked: an end unknown, a stake its quote does not hold."""


@dataclass(frozen=True)
class ProposalState:
    proposal: Proposal
    extraction: Extraction
    extractor: str
    # PROPOSED, ACCEPTED, REJECTED or REVOKED.
    status: str
    acceptance: Acceptance | None = None
    reviewed_by: str | None = None
    # The hash of the event that accepted it: a published row names it.
    accepted_event: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "PROPOSED" and not self.proposal.is_information


def proposal_states(records: list[dict[str, Any]]) -> dict[str, ProposalState]:
    """Proposal id -> how the log leaves it, in the order each was first proposed."""
    states: dict[str, ProposalState] = {}
    for record in records:
        if record["event_type"] == "EXTRACTED":
            extraction = Extraction.model_validate(record["payload"]["extraction"])
            for proposal in extraction.proposals:
                if proposal.proposal_id not in states:
                    states[proposal.proposal_id] = ProposalState(
                        proposal, extraction, record["actor"], "PROPOSED"
                    )
        elif record["event_type"] == "EXTRACTION_REVIEWED":
            proposal_id = record["decision_id"].removeprefix(PROPOSAL_PREFIX)
            state = states[proposal_id]
            action = record["payload"]["action"]
            if action == "ACCEPTED":
                acceptance = Acceptance.model_validate(record["payload"]["acceptance"])
                states[proposal_id] = ProposalState(
                    state.proposal,
                    state.extraction,
                    state.extractor,
                    action,
                    acceptance,
                    record["actor"],
                    record["hash"],
                )
            else:
                states[proposal_id] = ProposalState(
                    state.proposal, state.extraction, state.extractor, action
                )
    return states


def queue(records: list[dict[str, Any]]) -> list[ProposalState]:
    """The proposals waiting for a ruling, the ones that would change the graph most first."""
    waiting = [state for state in proposal_states(records).values() if state.is_open]
    return sorted(
        waiting,
        key=lambda s: (
            LABEL_ORDER[s.proposal.label],
            s.extraction.document_sha256,
            s.proposal.span.page,
            s.proposal.proposal_id,
        ),
    )


def accepted(records: list[dict[str, Any]]) -> list[ProposalState]:
    """Every proposal accepted and not revoked, by proposal id: what a publish holds."""
    states = proposal_states(records).values()
    return sorted(
        (state for state in states if state.status == "ACCEPTED"),
        key=lambda s: s.proposal.proposal_id,
    )


def new_entity_ref(state_or_sha: str, page: int, proposal_id: str, end: str) -> str:
    return f"{DOCUMENT_SOURCE}:{state_or_sha[:12]}:p{page}:{proposal_id}:{end}"


def _end(
    store: GraphStore,
    entity_id: str | None,
    new: NewEntity | None,
    proposal: Proposal,
    extraction: Extraction,
    end: str,
) -> EndChoice:
    if (entity_id is None) == (new is None):
        raise ExtractionReviewError(f"Name the {end}: an entity id or a new entity, not both.")
    if new is not None:
        ref = new_entity_ref(
            extraction.document_sha256, proposal.span.page, proposal.proposal_id, end
        )
        return EndChoice(new_entity=new, ref=ref)
    entity = queries.get_entity_by_id(store, entity_id or "")
    if entity is None:
        raise ExtractionReviewError(f"No entity {entity_id} in snapshot set {store.set_id}.")
    # Silver resolves an end through a record, never a name: the one that describes it.
    ref = f"{entity.provenance.source}:{entity.provenance.source_record_id}"
    return EndChoice(entity_id=entity.id, ref=ref)


def _acceptance(
    state: ProposalState,
    store: GraphStore,
    root: Path,
    as_of: date | None,
    owner_id: str | None,
    owner_new: NewEntity | None,
    asset_id: str | None,
    asset_new: NewEntity | None,
    pct: Decimal | None,
    rel_type: DocRelType | None,
    basis: DocBasis | None,
) -> Acceptance:
    proposal, extraction = state.proposal, state.extraction
    if as_of is None:
        raise ExtractionReviewError("State the document's effective date (--as-of).")
    edge = proposal.edge
    changed = edge.model_copy(
        update={
            "pct": edge.pct if pct is None else pct,
            "rel_type": rel_type or edge.rel_type,
            "basis": basis or edge.basis,
        }
    )
    changed = type(edge).model_validate(changed.model_dump())
    edits = {
        field: [getattr(edge, field), getattr(changed, field)]
        for field in ("pct", "rel_type", "basis")
        if getattr(edge, field) != getattr(changed, field)
    }
    if edits:
        # An edited stake must still be one the quote writes.
        document = load_document(extraction.document_sha256, root)
        if document is None:
            raise ExtractionReviewError("The document is not kept here: an edit cannot be checked.")
        _, fault = check_edge(changed, document.pages)
        if fault is not None:
            raise ExtractionReviewError(f"The edited holding fails the guardrail ({fault}).")
    if asset_id is None and asset_new is None:
        asset_id = extraction.subject_id
    owner = _end(store, owner_id, owner_new, proposal, extraction, "owner")
    asset = _end(store, asset_id, asset_new, proposal, extraction, "asset")
    if owner.ref == asset.ref:
        raise ExtractionReviewError("The owner and the asset are one entity.")
    return Acceptance(
        as_of=as_of,
        owner=owner,
        asset=asset,
        rel_type=changed.rel_type,
        basis=changed.basis,
        pct=changed.pct,
        edits={
            field: [str(v) if isinstance(v, Decimal) else v for v in pair]
            for field, pair in edits.items()
        },
    )


def review_proposal(
    log: AuditLog,
    ontology: OntologyConfig,
    proposal_id: str,
    action: ReviewAction,
    actor: str,
    role: str,
    created_at: datetime,
    root: Path,
    reason: str | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    as_of: date | None = None,
    owner_id: str | None = None,
    owner_new: NewEntity | None = None,
    asset_id: str | None = None,
    asset_new: NewEntity | None = None,
    pct: Decimal | None = None,
    rel_type: DocRelType | None = None,
    basis: DocBasis | None = None,
) -> AuditEvent:
    """Record one ruling. Raises `PermissionError`, `LookupError` for an unknown proposal,
    `ExtractionReviewError`, and `ValueError` for a ruling the log refuses."""
    require_command(ontology, role, "extract-review")
    state = proposal_states(log.records()).get(proposal_id)
    if state is None:
        raise LookupError(f"No proposal {proposal_id} in the audit log.")
    payload: dict[str, Any] = {
        "action": action,
        "proposal_id": proposal_id,
        "extraction_id": state.extraction.extraction_id,
    }
    if action == "ACCEPTED":
        with GraphStore.open(state.extraction.snapshot_set_id, con) as store:
            acceptance = _acceptance(
                state,
                store,
                root,
                as_of,
                owner_id,
                owner_new,
                asset_id,
                asset_new,
                pct,
                rel_type,
                basis,
            )
        payload["acceptance"] = acceptance.model_dump(mode="json")
    return log.append(
        ontology=ontology,
        decision_id=f"{PROPOSAL_PREFIX}{proposal_id}",
        event_type="EXTRACTION_REVIEWED",
        actor=actor,
        role=role,
        created_at=created_at,
        payload=payload,
        note=reason,
    )


def parse_new_entity(text: str) -> NewEntity:
    """`name;type;jurisdiction` (the jurisdiction may be left out)."""
    parts = [part.strip() for part in text.split(";")]
    if len(parts) not in (2, 3) or not parts[0]:
        raise ExtractionReviewError("A new entity is 'name;LegalEntity|Person[;country]'.")
    entity_type: EntityType = parts[1]  # type: ignore[assignment]
    try:
        return NewEntity(
            name=parts[0],
            entity_type=entity_type,
            jurisdiction=(parts[2] or None) if len(parts) == 3 else None,
        )
    except ValueError as exc:
        raise ExtractionReviewError(f"Not a new entity: {exc}") from exc
