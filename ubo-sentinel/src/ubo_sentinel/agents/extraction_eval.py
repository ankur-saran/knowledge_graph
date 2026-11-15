"""`ubo eval --suite extraction` (Step 15): proposals scored against labelled documents.

Each document of `eval/extraction/labels.yaml` is read against a fixtures set and
extracted as `ubo extract` would, writing nothing. Offline, the first pass is held
to the holdings marked `first_pass` and nothing else. With a model (live or
replaying recordings), its proposals are scored too: precision, recall, what it
adds over the first pass, and whether text written to steer it changed anything.
A document the model could not be asked about is "not run", never a pass.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.llm.client import LLMClient
from ubo_sentinel.llm.config import ExtractBudget
from ubo_sentinel.models.extraction import DocRelType, Proposal
from ubo_sentinel.models.relationship import Pct
from ubo_sentinel.pipeline.extraction.document import find_quote, read_document
from ubo_sentinel.pipeline.extraction.llm_extract import UNAVAILABLE
from ubo_sentinel.pipeline.extraction.run import extract
from ubo_sentinel.pipeline.name_match import normalise_name

LABELS_PATH = Path("eval/extraction/labels.yaml")


class ExpectedHolding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str
    asset: str
    rel_type: DocRelType
    pct: Pct | None = None
    kind: Literal["edge", "information"] = "edge"
    first_pass: bool = False


class LabelledDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    file: str
    subject_id: str
    expected: list[ExpectedHolding] = []
    injection: bool = False
    rationale: str = Field(min_length=1)


def load_labels(path: Path = LABELS_PATH) -> list[LabelledDocument]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    documents = [LabelledDocument.model_validate(entry) for entry in entries]
    ids = [document.id for document in documents]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: an id is used twice.")
    return documents


def _key(owner: str, asset: str, rel_type: str, pct: Decimal | None, information: bool) -> tuple:
    return (normalise_name(owner), normalise_name(asset), rel_type, pct, information)


def _expected_key(item: ExpectedHolding) -> tuple:
    return _key(item.owner, item.asset, item.rel_type, item.pct, item.kind == "information")


def _proposal_key(proposal: Proposal) -> tuple:
    edge = proposal.edge
    return _key(edge.owner_name, edge.asset_name, edge.rel_type, edge.pct, proposal.is_information)


@dataclass
class DocumentResult:
    document_id: str
    status: str
    first_pass_expected: int = 0
    first_pass_found: int = 0
    first_pass_proposals: int = 0
    first_pass_correct: int = 0
    # Proposals whose span, read again from the text, is the quote.
    spans: int = 0
    spans_valid: int = 0
    model_ran: bool = False
    expected: int = 0
    found: int = 0
    found_by_first_pass: int = 0
    proposals: int = 0
    correct: int = 0
    injection_ok: bool | None = None
    tokens: int = 0
    problems: list[str] = field(default_factory=list)


def _span_valid(proposal: Proposal, pages: list[str]) -> bool:
    return find_quote(pages, proposal.span.page, proposal.edge.quote) == proposal.span


def score(
    labelled: LabelledDocument,
    proposals: list[Proposal],
    pages: list[str],
    model_ran: bool,
    tokens: int = 0,
) -> DocumentResult:
    wanted = [_expected_key(item) for item in labelled.expected]
    first_wanted = [_expected_key(item) for item in labelled.expected if item.first_pass]
    first = [p for p in proposals if p.origin == "first_pass"]
    first_keys = [_proposal_key(p) for p in first]
    keys = [_proposal_key(p) for p in proposals]
    result = DocumentResult(labelled.id, "PASS")
    result.first_pass_expected = len(first_wanted)
    result.first_pass_found = sum(key in first_keys for key in first_wanted)
    result.first_pass_proposals = len(first)
    result.first_pass_correct = sum(key in first_wanted for key in first_keys)
    result.spans = len(proposals)
    result.spans_valid = sum(_span_valid(p, pages) for p in proposals)
    for key in first_wanted:
        if key not in first_keys:
            result.problems.append(f"first pass missed {key[0]} {key[2]} {key[1]} {key[3]}")
    for key in first_keys:
        if key not in first_wanted:
            result.problems.append(f"first pass proposed {key[0]} {key[2]} {key[1]} {key[3]}")
    result.model_ran = model_ran
    result.tokens = tokens
    if model_ran:
        result.expected = len(wanted)
        result.found = sum(key in keys for key in wanted)
        result.found_by_first_pass = sum(key in first_keys for key in wanted)
        result.proposals = len(proposals)
        result.correct = sum(key in wanted for key in keys)
        if labelled.injection:
            result.injection_ok = all(key in wanted for key in keys)
    if result.problems:
        result.status = "FAIL"
    elif not model_ran:
        result.status = "PASS (first pass)"
    return result


def run_extraction_suite(
    store: GraphStore,
    documents: list[LabelledDocument],
    directory: Path,
    client: LLMClient | None = None,
    budget: ExtractBudget | None = None,
    max_tokens: int = 4000,
) -> list[DocumentResult]:
    """Extract each labelled document against the store's set and score it. Writes nothing."""
    results = []
    for labelled in documents:
        if queries.get_entity_by_id(store, labelled.subject_id) is None:
            missing = DocumentResult(labelled.id, "FAIL")
            missing.problems.append(f"set {store.set_id} has no entity {labelled.subject_id}")
            results.append(missing)
            continue
        _, document = read_document(directory / labelled.file)
        extraction = extract(
            store, document, labelled.subject_id, client, budget, consent=client is not None,
            max_tokens=max_tokens,
        )  # fmt: skip
        answered = [call for call in extraction.calls if call.fault != UNAVAILABLE]
        model_ran = client is not None and bool(answered)
        tokens = sum(call.input_tokens + call.output_tokens for call in extraction.calls)
        result = score(labelled, extraction.proposals, document.pages, model_ran, tokens)
        if client is not None and not model_ran and result.status != "FAIL":
            result.status = "NOT RUN"
        results.append(result)
    return results
