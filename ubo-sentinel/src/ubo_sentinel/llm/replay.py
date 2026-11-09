"""Replay of recorded LLM output: rebuild what the model read and check it again.

The model is never called. A replay rebuilds the model's input from the
recorded decision as this code builds it, compares its digest with the one the
record holds, and runs the guardrail again on the stored output. The checks run
from the cheapest and stop at the first that fails, so the verdict names what
moved: the prompt (`NOT_REPLAYABLE`), the input (`INPUT_DIFFERS`) or the check
(`GUARDRAIL_DIFFERS`).

An extraction (Step 15) is made again from the stored document text with its
stored answers in the model's place: a text that is not the one read is
`DOCUMENT_CHANGED`, a graph built otherwise `GRAPH_CHANGED`, a request that is not
the one recorded `INPUT_DIFFERS`, other proposals or faults `GUARDRAIL_DIFFERS`.

An investigation is replayed by making it again with its stored turns in the
model's place: the first pass, each tool call through the analyst's view, each
request's digest, the budgets' path and the guardrail all run as they ran. Only
the turns are stored answers, so what moved is named: the data under the
decision (`GRAPH_CHANGED`), the context layer (`CONTEXT_CHANGED`), a tool's
result (`TOOL_RESULT_DIFFERS`), a request (`INPUT_DIFFERS`), or the check.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.agents import investigator, llm_prose, match_adjudicator
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.replay import replay_decision
from ubo_sentinel.llm.annotation import Annotation, prompt_sha256
from ubo_sentinel.llm.client import LLMRequest, LLMResponse, LLMUnavailable
from ubo_sentinel.llm.config import ExtractBudget, InvestigateBudget
from ubo_sentinel.llm.egress import EgressRefused
from ubo_sentinel.llm.tools import ToolTurn, ToolTurnRequest
from ubo_sentinel.models.audit_event import RUN_PREFIX
from ubo_sentinel.models.canonical import canonical_json, sha256_hex
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.extraction import Extraction, ModelCall
from ubo_sentinel.models.investigation import Investigation, TurnRecord
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.pipeline.context import ContextNotBuilt, context_digest, context_rows_digest
from ubo_sentinel.pipeline.db import connect, db_path

Verdict = Literal[
    "IDENTICAL",
    "NOT_REPLAYABLE",
    "GRAPH_CHANGED",
    "CONTEXT_CHANGED",
    "TOOL_RESULT_DIFFERS",
    "INPUT_DIFFERS",
    "GUARDRAIL_DIFFERS",
    "DOCUMENT_CHANGED",
]


class UnknownRecord(LookupError):
    """No such annotation (or investigation) in the audit log."""


class LLMReplayReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["annotation", "investigation", "extraction"]
    record_id: str
    decision_id: str
    feature: str
    verdict: Verdict
    # What failed, as a kind; never a value of the record.
    detail: str
    recorded_status: str

    @property
    def identical(self) -> bool:
        return self.verdict == "IDENTICAL"


@dataclass(frozen=True)
class _Feature:
    prompt_version: int
    system: str
    schema: dict[str, Any]
    # The model's input, rebuilt from the decision as `ubo annotate` builds it.
    build: Callable[[Decision, OntologyConfig], dict[str, Any]]
    # The kinds of fault in a stored output, against that input.
    check: Callable[[dict[str, Any], Decision, dict[str, Any]], list[str]]


def _check_matches(output: dict[str, Any], _: Decision, data: dict[str, Any]) -> list[str]:
    # `not_sent` is added by code after the check, not written by the model.
    answer = {key: value for key, value in output.items() if key != "not_sent"}
    return match_adjudicator.check_assessments(answer, data)


FEATURES = {
    llm_prose.FEATURE: _Feature(
        llm_prose.PROMPT_VERSION,
        llm_prose.SYSTEM,
        llm_prose.SCHEMA,
        llm_prose.prose_input,
        llm_prose.check_slotted,
    ),
    match_adjudicator.FEATURE: _Feature(
        match_adjudicator.PROMPT_VERSION,
        match_adjudicator.SYSTEM,
        match_adjudicator.SCHEMA,
        match_adjudicator.adjudication_input,
        _check_matches,
    ),
}


def recorded_annotation_event(log: AuditLog, annotation_id: str) -> dict[str, Any]:
    for record in log.records():
        if record["event_type"] == "ANNOTATED" and (
            record["payload"].get("annotation_id") == annotation_id
        ):
            return record
    raise UnknownRecord(f"No annotation {annotation_id} in the audit log.")


def recommended_decision(log: AuditLog, decision_id: str) -> Decision:
    """The decision as it was recommended: what the model's input was built from."""
    decision = log.recommended(decision_id)
    if decision is None:
        raise UnknownRecord(f"No decision {decision_id} in the audit log.")
    return decision


def replay_annotation(
    log: AuditLog, ontology: OntologyConfig, annotation_id: str
) -> LLMReplayReport:
    """Rebuild an annotation's input and check its stored output again. Writes nothing.

    Raises `UnknownRecord`, and `AuditChainError` when the log does not verify.
    """
    record = recorded_annotation_event(log, annotation_id)
    annotation = Annotation.model_validate(record["payload"]["annotation"])
    decision = recommended_decision(log, annotation.decision_id)

    def report(verdict: Verdict, detail: str) -> LLMReplayReport:
        return LLMReplayReport(
            kind="annotation",
            record_id=annotation_id,
            decision_id=annotation.decision_id,
            feature=annotation.feature,
            verdict=verdict,
            detail=detail,
            recorded_status=annotation.status,
        )

    feature = FEATURES.get(annotation.feature)
    if feature is None:
        return report("NOT_REPLAYABLE", f"this code has no feature '{annotation.feature}'")
    if annotation.prompt_version != feature.prompt_version or (
        annotation.prompt_sha256 != prompt_sha256(feature.system, feature.schema)
    ):
        return report(
            "NOT_REPLAYABLE",
            f"the record was made with prompt version {annotation.prompt_version};"
            f" this code has version {feature.prompt_version} or another prompt",
        )
    try:
        data = feature.build(decision, ontology)
    except EgressRefused:
        return report("INPUT_DIFFERS", "the input this code builds would not be sent")
    if sha256_hex(data) != annotation.input_digest:
        return report(
            "INPUT_DIFFERS",
            "the input this code builds from the decision is not the one the model read",
        )
    if annotation.output is not None:
        faults = feature.check(annotation.output, decision, data)
        if sorted(set(faults)) != annotation.faults:
            return report(
                "GUARDRAIL_DIFFERS",
                f"the guardrail finds {len(faults)} kind(s) of fault where"
                f" {len(annotation.faults)} were recorded",
            )
    return report("IDENTICAL", "the input and the guardrail's findings are as recorded")


class _StoredTurns:
    """The model, played back: each turn answers only the request it was made for."""

    def __init__(self, turns: list[TurnRecord]) -> None:
        self.turns = turns
        self.used = 0
        # The 1-based turn whose request is not the one recorded.
        self.differs_at: int | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMUnavailable("An investigation has no single-turn request.")

    def converse(self, request: ToolTurnRequest) -> ToolTurn:
        if self.used == len(self.turns):
            raise LLMUnavailable("No stored turn is left.")
        stored = self.turns[self.used]
        if request.digest() != stored.request_digest:
            self.differs_at = self.used + 1
            raise LLMUnavailable("The request is not the one recorded.")
        self.used += 1
        return ToolTurn.model_validate(stored.model_dump(exclude={"request_digest", "call_ids"}))


def _plain(value: Any) -> Any:
    """As the log holds it: a stored record and a new one compare field by field."""
    return json.loads(canonical_json(value))


def _check_again(
    recorded: Investigation, decision: Decision, ontology: OntologyConfig, con: Any
) -> tuple[Verdict, str]:
    asked = recorded.model is not None
    client = _StoredTurns(recorded.turns) if asked else None
    budget = InvestigateBudget(**recorded.budgets) if asked else None
    again = investigator.investigate(decision, recorded.question, ontology, con, client, budget)

    for was, now in zip(recorded.calls, again.calls, strict=False):
        if _plain(was) != _plain(now):
            return "TOOL_RESULT_DIFFERS", f"call {was.call_id} ({was.tool}) gives another result"
    # A request that is not the one recorded ends the run there: it explains fewer calls.
    if client is not None and (client.differs_at or client.used != len(recorded.turns)):
        turn = client.differs_at or client.used + 1
        return "INPUT_DIFFERS", f"the request of turn {turn} is not the one the model read"
    if len(recorded.calls) != len(again.calls):
        return (
            "TOOL_RESULT_DIFFERS",
            f"{len(again.calls)} calls were made where {len(recorded.calls)} were recorded",
        )
    # A question that was not sent stopped the run, and is not asked again: it is the mask.
    stopped = {investigator.QUESTION_MASKED, investigator.OPENING_MASKED}
    fields = ("first_pass", "findings", "suggested_actions", "coverage")
    faults = [set(item.faults) - stopped for item in (recorded, again)]
    if (
        any(_plain(getattr(recorded, f)) != _plain(getattr(again, f)) for f in fields)
        or faults[0] != faults[1]
        or (asked and recorded.complete != again.complete)
    ):
        return "GUARDRAIL_DIFFERS", "the findings or their faults are not the ones recorded"
    return "IDENTICAL", "the data, each tool result, each request and the findings are as recorded"


def replay_investigation(
    log: AuditLog,
    ontology: OntologyConfig,
    investigation_id: str,
    con: Any = None,
    deep: bool = False,
) -> LLMReplayReport:
    """Make a recorded investigation again and compare. Never calls a model; writes nothing.

    `deep` computes every digest from the rows, as `replay_decision()` does.

    Raises `UnknownRecord`, and `AuditChainError` when the log does not verify.
    """
    event_id = f"{RUN_PREFIX['INVESTIGATED']}{investigation_id}"
    record = next(
        (r for r in log.records() if r["decision_id"] == event_id),
        None,
    )
    if record is None:
        raise UnknownRecord(f"No investigation {investigation_id} in the audit log.")
    recorded = Investigation.model_validate(record["payload"])
    decision = recommended_decision(log, recorded.decision_id)

    def report(verdict: Verdict, detail: str) -> LLMReplayReport:
        return LLMReplayReport(
            kind="investigation",
            record_id=investigation_id,
            decision_id=recorded.decision_id,
            feature=investigator.FEATURE,
            verdict=verdict,
            detail=detail,
            recorded_status="COMPLETE" if recorded.complete else "INCOMPLETE",
        )

    if (
        recorded.prompt_version != investigator.PROMPT_VERSION
        or recorded.prompt_sha256 != investigator.prompt_digest()
    ):
        return report(
            "NOT_REPLAYABLE",
            f"the record was made with prompt version {recorded.prompt_version};"
            f" this code has version {investigator.PROMPT_VERSION} or another prompt or tools",
        )
    if any(fault.startswith(investigator.INTERRUPTED) for fault in recorded.faults):
        return report("NOT_REPLAYABLE", "the run was interrupted, so it ended at no recorded turn")
    own = con is None
    if own:
        if not db_path().exists():
            return report("NOT_REPLAYABLE", "There is no database here.")
        con = connect(read_only=True)
    try:
        checked = replay_decision(log, ontology, recorded.decision_id, con, deep)
        if checked.verdict == "NOT_REPLAYABLE":
            return report("NOT_REPLAYABLE", checked.detail)
        if not checked.identical:
            return report("GRAPH_CHANGED", f"the decision now replays as {checked.verdict}")
        try:
            digest = context_digest(con, recorded.snapshot_set_id)
            if deep:
                digest = context_rows_digest(con, recorded.snapshot_set_id)
        except ContextNotBuilt:
            digest = None
        if digest != recorded.context_digest:
            return report("CONTEXT_CHANGED", "the context layer is not the one the tools read")
        return report(*_check_again(recorded, decision, ontology, con))
    finally:
        if own:
            con.close()


# --- extractions (Step 15) ------------------------------------------------------------------


class _StoredCalls:
    """The model, played back for an extraction: each call answers only its own request."""

    def __init__(self, calls: list[ModelCall]) -> None:
        self.calls = calls
        self.used = 0
        self.differs_at: int | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self.used == len(self.calls):
            raise LLMUnavailable("No stored call is left.")
        stored = self.calls[self.used]
        if request.digest() != stored.request_digest:
            self.differs_at = self.differs_at or self.used + 1
        self.used += 1
        if stored.output is None:
            raise LLMUnavailable("The recorded call had no answer.")
        return LLMResponse(
            output=stored.output,
            model=request.model,
            input_tokens=stored.input_tokens,
            output_tokens=stored.output_tokens,
        )

    def converse(self, request: ToolTurnRequest) -> ToolTurn:
        raise LLMUnavailable("An extraction has no tool turns.")


def replay_extraction(
    log: AuditLog, extraction_id: str, root: Any, con: Any = None
) -> LLMReplayReport:
    """Make a recorded extraction again from the stored text. Never calls a model; writes
    nothing. `root` is where documents are kept (`UBO_VAR`).

    Raises `UnknownRecord`, and `AuditChainError` when the log does not verify.
    """
    from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
    from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
    from ubo_sentinel.pipeline.extraction import llm_extract
    from ubo_sentinel.pipeline.extraction.document import load_document
    from ubo_sentinel.pipeline.extraction.run import extract

    event_id = f"{RUN_PREFIX['EXTRACTED']}{extraction_id}"
    record = next((r for r in log.records() if r["decision_id"] == event_id), None)
    if record is None:
        raise UnknownRecord(f"No extraction {extraction_id} in the audit log.")
    recorded = Extraction.model_validate(record["payload"]["extraction"])

    def report(verdict: Verdict, detail: str) -> LLMReplayReport:
        return LLMReplayReport(
            kind="extraction",
            record_id=extraction_id,
            decision_id=recorded.subject_id,
            feature=llm_extract.FEATURE,
            verdict=verdict,
            detail=detail,
            recorded_status="COMPLETE" if recorded.complete else "INCOMPLETE",
        )

    if (
        recorded.prompt_version != llm_extract.PROMPT_VERSION
        or recorded.prompt_sha256 != llm_extract.prompt_digest()
    ):
        return report(
            "NOT_REPLAYABLE",
            f"the record was made with prompt version {recorded.prompt_version};"
            f" this code has version {llm_extract.PROMPT_VERSION} or another prompt",
        )
    document = load_document(recorded.document_sha256, root)
    if document is None or document.text_digest != recorded.text_digest:
        return report("DOCUMENT_CHANGED", "the document's text is not kept here as it was read")
    own = con is None
    if own:
        if not db_path().exists():
            return report("NOT_REPLAYABLE", "There is no database here.")
        con = connect(read_only=True)
    try:
        try:
            with GraphStore.open(recorded.snapshot_set_id, con) as store:
                if store.gold_digest != recorded.graph_digest:
                    return report(
                        "GRAPH_CHANGED", "the graph is not the one the labels were read from"
                    )
                asked = recorded.model is not None
                client = _StoredCalls(recorded.calls) if asked else None
                budgets = dict(recorded.budgets)
                limit = budgets.pop("request_max_tokens", 4000)
                budget = ExtractBudget(**budgets) if asked else None
                again = extract(
                    store,
                    document,
                    recorded.subject_id,
                    client,
                    budget,
                    consent=asked,
                    max_tokens=limit,
                    investigation_id=recorded.investigation_id,
                )
        except (UnknownSnapshotSet, GraphNotBuilt) as exc:
            return report("NOT_REPLAYABLE", f"the set is not here ({type(exc).__name__})")
    finally:
        if own:
            con.close()
    if client is not None and (client.differs_at or client.used != len(recorded.calls)):
        call = client.differs_at or client.used + 1
        return report("INPUT_DIFFERS", f"the request of call {call} is not the one the model read")
    fields = ("proposals", "faults", "dropped", "pages_not_read", "complete")
    if any(_plain(getattr(recorded, f)) != _plain(getattr(again, f)) for f in fields):
        return report(
            "GUARDRAIL_DIFFERS", "the proposals or their faults are not the ones recorded"
        )
    return report("IDENTICAL", "the text, each request and the proposals are as recorded")
