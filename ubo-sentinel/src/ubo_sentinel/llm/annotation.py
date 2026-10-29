"""An LLM annotation of a recorded decision, and how it is recorded (`ANNOTATED`).

Advisory text beside a decision, never part of it: it is folded into
`Decision.llm_annotations`, which is outside the canonical payload. A rejected
or unavailable answer is recorded too, so the log shows every call made.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm.client import LLMClient, LLMRequest, LLMUnavailable
from ubo_sentinel.models.canonical import sha256_hex, short_id
from ubo_sentinel.models.ontology import OntologyConfig

Status = Literal["ACCEPTED", "REJECTED", "UNAVAILABLE"]


class Annotation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    annotation_id: str
    decision_id: str
    feature: str
    status: Status
    model: str
    prompt_version: int
    prompt_sha256: str
    # The model input is rebuilt from the decision; its digest shows it was this one.
    input_digest: str
    egress: str
    output: dict[str, Any] | None = None
    # Kinds of fault the guardrail found, never a value.
    faults: list[str] = []
    usage: dict[str, Any] = {}


def prompt_sha256(system: str, schema: dict[str, Any]) -> str:
    return sha256_hex([system, schema])


def make_annotation(
    decision_id: str,
    request: LLMRequest,
    prompt_version: int,
    egress: str,
    status: Status,
    output: dict[str, Any] | None,
    faults: list[str],
    usage: dict[str, Any],
) -> Annotation:
    input_digest = sha256_hex(request.data)
    output_digest = sha256_hex(output) if output is not None else None
    annotation_id = short_id(
        [
            decision_id,
            request.feature,
            input_digest,
            prompt_version,
            request.model,
            output_digest,
            status,
            sorted(faults),
        ]  # fmt: skip
    )
    return Annotation(
        annotation_id=annotation_id,
        decision_id=decision_id,
        feature=request.feature,
        status=status,
        model=request.model,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256(request.system, request.schema_),
        input_digest=input_digest,
        egress=egress,
        output=output,
        faults=sorted(set(faults)),
        usage=usage,
    )


def ask(client: LLMClient, request: LLMRequest) -> tuple[dict[str, Any] | None, dict, list[str]]:
    """(output, usage, faults): an unavailable model is a fault, never an exception."""
    try:
        response = client.complete(request)
    except LLMUnavailable as exc:
        return None, {}, [f"unavailable: {exc}"]
    usage = {
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "seconds": response.seconds,
    }
    return response.output, usage, []


def recorded_annotation(
    log: AuditLog,
    decision_id: str,
    feature: str,
    input_digest: str,
    prompt_version: int,
    model: str,
) -> Annotation | None:
    """An accepted annotation on record for the same input, prompt and model: asked once."""
    for record in reversed(log.events_of(decision_id)):
        if record["event_type"] != "ANNOTATED":
            continue
        found = Annotation.model_validate(record["payload"]["annotation"])
        wanted = (feature, input_digest, prompt_version, model, "ACCEPTED")
        if (found.feature, found.input_digest, found.prompt_version, found.model,
                found.status) == wanted:  # fmt: skip
            return found
    return None


def record(
    log: AuditLog,
    ontology: OntologyConfig,
    annotation: Annotation,
    actor: str,
    role: str,
    created_at: datetime,
) -> bool:
    """Append the `ANNOTATED` event. False when this annotation is already on record."""

    def not_twice(records: list[dict[str, Any]]) -> None:
        for event in records:
            if event["event_type"] == "ANNOTATED" and (
                event["payload"]["annotation_id"] == annotation.annotation_id
            ):
                raise _OnRecord

    try:
        log.append(
            ontology=ontology,
            decision_id=annotation.decision_id,
            event_type="ANNOTATED",
            actor=actor,
            role=role,
            created_at=created_at,
            payload={
                "annotation_id": annotation.annotation_id,
                "annotation": annotation.model_dump(mode="json"),
            },
            guard=not_twice,
        )
    except _OnRecord:
        return False
    return True


class _OnRecord(Exception):
    pass
