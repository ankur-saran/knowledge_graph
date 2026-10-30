"""Replay of recorded LLM output: rebuild what the model read and check it again.

The model is never called. A replay rebuilds the model's input from the
recorded decision as this code builds it, compares its digest with the one the
record holds, and runs the guardrail again on the stored output. The checks run
from the cheapest and stop at the first that fails, so the verdict names what
moved: the prompt (`NOT_REPLAYABLE`), the input (`INPUT_DIFFERS`) or the check
(`GUARDRAIL_DIFFERS`). Step 14's investigations are replayed here too.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.agents import llm_prose, match_adjudicator
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm.annotation import Annotation, prompt_sha256
from ubo_sentinel.llm.egress import EgressRefused
from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.ontology import OntologyConfig

Verdict = Literal["IDENTICAL", "NOT_REPLAYABLE", "INPUT_DIFFERS", "GUARDRAIL_DIFFERS"]


class UnknownRecord(LookupError):
    """No such annotation (or investigation) in the audit log."""


class LLMReplayReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["annotation"]
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
    for record in log.events_of(decision_id):
        if record["event_type"] == "RECOMMENDED":
            return Decision.model_validate(record["payload"]["decision"])
    raise UnknownRecord(f"No decision {decision_id} in the audit log.")


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
