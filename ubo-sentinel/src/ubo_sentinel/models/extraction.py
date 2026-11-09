"""Ownership read from a document (Step 15): what was proposed, where it is written, and
what a reviewer made of it.

Nothing here is in a decision. A proposal a reviewer accepts becomes a Bronze row
when an engineer publishes (`pipeline/loaders/document_loader.py`), and from there
it is graph data like any other.
"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ubo_sentinel.models.entity import EntityType
from ubo_sentinel.models.provenance import NonEmptyStr
from ubo_sentinel.models.relationship import Pct

# The relations a document may state. `CONSOLIDATED_BY` is the registry's statement.
DocRelType = Literal["OWNS", "CONTROLS"]
DocBasis = Literal["shareholding", "directorship", "other"]
Holding = Literal["direct", "indirect"]
Interest = Literal["equity", "voting", "economic"]
# Who proposed it: the parser (no key) or a model.
Origin = Literal["first_pass", "model"]
# What a proposal would do to the current graph, in the order a reviewer is shown them.
# `INDIRECT_INFO` and `INTEREST_INFO` are shown and never accepted: an indirect holding
# is already counted through the chain, and a voting or economic interest is not equity.
Label = Literal["CONFLICTS", "FILLS_NULL_PCT", "NEW", "CONFIRMS", "INDIRECT_INFO", "INTEREST_INFO"]
LABEL_ORDER: dict[str, int] = {
    "CONFLICTS": 0,
    "FILLS_NULL_PCT": 1,
    "NEW": 2,
    "CONFIRMS": 3,
    "INDIRECT_INFO": 4,
    "INTEREST_INFO": 5,
}
INFORMATION_LABELS = frozenset({"INDIRECT_INFO", "INTEREST_INFO"})
# How a run read the document's text out.
Egress = Literal["none", "document_consented"]
ReviewAction = Literal["ACCEPTED", "REJECTED", "REVOKED"]

# A name or a quote longer than this is not a name or a quote.
MAX_NAME = 200
MAX_QUOTE = 500


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProposedEdge(_Model):
    """One stated holding, as a proposer writes it: names as written, the quote verbatim."""

    owner_name: str = Field(min_length=1, max_length=MAX_NAME)
    asset_name: str = Field(min_length=1, max_length=MAX_NAME)
    rel_type: DocRelType
    basis: DocBasis
    pct: Pct | None = None
    holding: Holding = "direct"
    interest: Interest = "equity"
    share_class: str | None = Field(default=None, max_length=MAX_NAME)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE)
    page: int = Field(ge=1)
    as_of_hint: date | None = None


class Span(_Model):
    """Where the quote is, in the page's normalised text (`document.normalise_text`)."""

    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class Suggestion(_Model):
    """A candidate the resolver gives for a name; a reviewer picks, code never does."""

    entity_id: NonEmptyStr
    confidence: float
    match_type: NonEmptyStr


class Proposal(_Model):
    proposal_id: NonEmptyStr
    origin: Origin
    edge: ProposedEdge
    span: Span
    label: Label
    owner_candidates: list[Suggestion] = []
    asset_candidates: list[Suggestion] = []
    # The stake on record for the pair the best candidates name, for CONFIRMS and CONFLICTS.
    existing_pct: Pct | None = None

    @property
    def is_information(self) -> bool:
        return self.label in INFORMATION_LABELS


class ModelCall(_Model):
    """One request to the model: its pages, digest and what it answered, as it answered."""

    pages: list[int]
    request_digest: NonEmptyStr
    output: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    fault: str | None = None


class Extraction(_Model):
    """One run over one document, recorded once as an `EXTRACTED` event."""

    extraction_id: NonEmptyStr
    document_sha256: NonEmptyStr
    file_name: NonEmptyStr
    text_digest: NonEmptyStr
    extractor: NonEmptyStr
    page_count: int = Field(ge=1)
    subject_id: NonEmptyStr
    snapshot_set_id: NonEmptyStr
    # The graph the labels and the pseudonyms were read from.
    graph_digest: NonEmptyStr
    investigation_id: str | None = None
    egress: Egress
    model: str | None = None
    prompt_version: int
    prompt_sha256: NonEmptyStr
    # The budget the model was held to (`ExtractBudget`) and each request's token limit.
    budgets: dict[str, Any] = {}
    calls: list[ModelCall] = []
    # Pages the model was not asked about (a budget ran out): no proposal is not no owner.
    pages_not_read: list[int] = []
    proposals: list[Proposal]
    # Kinds of fault, never a value: what the guardrail dropped and why, a model that failed.
    faults: list[str] = []
    dropped: int = 0
    complete: bool

    @property
    def event_id(self) -> str:
        return f"extraction:{self.extraction_id}"


class NewEntity(_Model):
    """A holder the graph does not have, as the reviewer confirms it."""

    name: str = Field(min_length=1, max_length=MAX_NAME)
    entity_type: EntityType
    jurisdiction: str | None = None


class EndChoice(_Model):
    """One end of an accepted edge: an entity of the set, or a new one; and its ref."""

    entity_id: str | None = None
    new_entity: NewEntity | None = None
    # `<source>:<source_record_id>` of the record Silver resolves the end through.
    ref: NonEmptyStr


class Acceptance(_Model):
    """What an `ACCEPTED` review states, beside the proposal it accepts."""

    as_of: date
    owner: EndChoice
    asset: EndChoice
    rel_type: DocRelType
    basis: DocBasis
    pct: Pct | None = None
    # Field -> [before, after] for what the reviewer changed.
    edits: dict[str, list[Any]] = {}
