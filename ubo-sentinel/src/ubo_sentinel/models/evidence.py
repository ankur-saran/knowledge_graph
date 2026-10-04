"""Vocabulary shared by the gold scenarios, the rule engine and the memo."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from ubo_sentinel.models.canonical import quantize
from ubo_sentinel.models.provenance import NonEmptyStr, Provenance

GapCode = Literal[
    "NULL_PCT_EDGE",
    "REPORTING_EXCEPTION",
    "LAPSED_LEI",
    "REVIEW_BAND_MATCH",
    "PCT_SUM_OVER_100",
    # The upward traversal stopped at max_depth with owners still unexplored.
    "DEPTH_LIMIT_REACHED",
]

# Why a recommendation was made. The last two come from the agent pipeline, not the rules.
ReasonCode = Literal[
    "DIRECT_DESIGNATION",
    "DERIVED_50PCT",
    "POSSIBLY_BLOCKED",
    "EFFECTIVE_EXPOSURE",
    "NEAR_MISS_AGGREGATE",
    "DECISION_RELEVANT_GAP",
    "CONTROL_LINK",
    "ENTITY_NOT_FOUND",
    "GUARDRAIL_FAILED",
    "PIPELINE_ERROR",
]

Exposure = Annotated[Decimal, AfterValidator(quantize), Field(ge=0)]


class EvidencePath(BaseModel):
    """A path from a blocked party to the target: n nodes joined by n-1 edges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_ids: list[NonEmptyStr] = Field(min_length=1)
    edge_ids: list[NonEmptyStr]

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError("a path needs exactly one fewer edge than nodes")
        return self

    def sort_key(self) -> tuple[list[str], list[str]]:
        return (self.node_ids, self.edge_ids)


class DataGap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: GapCode
    # A node id or an edge id.
    subject_id: NonEmptyStr
    decision_relevant: bool
    provenance: Provenance

    def sort_key(self) -> tuple[str, str, tuple[str, str, str, str]]:
        return (self.code, self.subject_id, self.provenance.sort_key())


class EffectiveExposure(BaseModel):
    """Multiplicative exposure. A risk signal only, not the legal test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pct: Exposure | None = None
    sum_pct: Exposure | None = None
    # True when a path contains a null-% edge, so the figures are a lower bound.
    has_unknown: bool = False
