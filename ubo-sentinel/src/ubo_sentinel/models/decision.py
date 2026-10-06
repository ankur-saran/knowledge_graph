from datetime import UTC
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ubo_sentinel.models.canonical import short_id
from ubo_sentinel.models.evidence import DataGap, EffectiveExposure, EvidencePath, ReasonCode
from ubo_sentinel.models.provenance import NonEmptyStr

Recommendation = Literal["CLEAR", "REVIEW", "ESCALATE"]
DecisionStatus = Literal["RECOMMENDED", "APPROVED", "OVERRIDDEN", "ESCALATED"]

UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]

REVIEWER_FIELDS = frozenset({"reviewed_by", "review_reason", "reviewed_at"})
# `query` is what the caller typed: two spellings of one name reach one decision.
NON_CANONICAL_FIELDS = REVIEWER_FIELDS | {"created_at", "status", "llm_annotations", "query"}


def normalise_query(query: str) -> str:
    return " ".join(query.split()).casefold()


def compute_decision_id(
    target_id: str | None,
    query: str,
    snapshot_ids: dict[str, str],
    rule_pack_hash: str,
    engine_version: str,
    max_depth: int,
    graph_digest: str,
    pipeline_version: int,
) -> str:
    """Deterministic id over the resolved snapshot ids, never the set alias.

    When no target was resolved, the normalised query takes its place. The
    graph digest and the pipeline version are part of it, so a change to the
    transform or to the agents gives a new id even when the package version
    has not moved.
    """
    subject = target_id if target_id is not None else normalise_query(query)
    return short_id(
        [
            subject,
            snapshot_ids,
            rule_pack_hash,
            engine_version,
            max_depth,
            graph_digest,
            pipeline_version,
        ]
    )


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: NonEmptyStr
    query: str
    target_id: NonEmptyStr | None
    recommendation: Recommendation
    status: DecisionStatus
    reasons: list[ReasonCode] = []
    blocked_set: list[str] = []
    possibly_blocked_set: list[str] = []
    paths: list[EvidencePath] = []
    effective_exposure: EffectiveExposure = EffectiveExposure()
    data_gaps: list[DataGap] = []
    guardrail_warnings: list[str] = []
    memo_json: dict[str, Any] = {}
    llm_annotations: dict[str, Any] = {}
    # The resolved set id, never an alias: what the caller typed is in the audit event.
    snapshot_set_id: NonEmptyStr
    # Dataset -> resolved snapshot id.
    snapshot_ids: dict[str, str]
    rule_pack_id: NonEmptyStr
    rule_pack_hash: NonEmptyStr
    engine_version: NonEmptyStr
    # `PIPELINE_VERSION` of the agents that made the decision.
    pipeline_version: int = Field(ge=1)
    # The `gold_digest` of the graph the screen read.
    graph_digest: NonEmptyStr
    max_depth: int = Field(ge=0)
    created_at: UtcDatetime
    # Empty on a RECOMMENDED decision; filled when the view is rebuilt from the audit log.
    reviewed_by: str | None = None
    review_reason: str | None = None
    reviewed_at: UtcDatetime | None = None

    @field_validator("reasons", "blocked_set", "possibly_blocked_set", "guardrail_warnings")
    @classmethod
    def _sort_unique[T: str](cls, value: list[T]) -> list[T]:
        return sorted(set(value))

    @field_validator("paths")
    @classmethod
    def _sort_paths(cls, value: list[EvidencePath]) -> list[EvidencePath]:
        return sorted(value, key=EvidencePath.sort_key)

    @field_validator("data_gaps")
    @classmethod
    def _sort_gaps(cls, value: list[DataGap]) -> list[DataGap]:
        return sorted(value, key=DataGap.sort_key)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        expected = compute_decision_id(
            self.target_id,
            self.query,
            self.snapshot_ids,
            self.rule_pack_hash,
            self.engine_version,
            self.max_depth,
            self.graph_digest,
            self.pipeline_version,
        )
        if self.decision_id != expected:
            raise ValueError(f"decision_id does not match its inputs (expected {expected})")

        overlap = set(self.blocked_set) & set(self.possibly_blocked_set)
        if overlap:
            raise ValueError(f"blocked and possibly-blocked sets overlap: {sorted(overlap)}")

        if self.status == "RECOMMENDED":
            if self.reviewed_by or self.review_reason or self.reviewed_at:
                raise ValueError("a RECOMMENDED decision has no reviewer fields")
        else:
            if not self.reviewed_by or self.reviewed_at is None:
                raise ValueError(f"a {self.status} decision needs reviewed_by and reviewed_at")
            if self.status == "OVERRIDDEN" and not (self.review_reason or "").strip():
                raise ValueError("an OVERRIDDEN decision needs a review_reason")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """What determinism and replay compare: no times, status, LLM text, reviewer or query."""
        return self.model_dump(mode="json", exclude=set(NON_CANONICAL_FIELDS))
