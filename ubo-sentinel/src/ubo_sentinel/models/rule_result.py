"""What the rule engine concludes for one target, and how it got there."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ubo_sentinel.models.decision import Recommendation
from ubo_sentinel.models.evidence import (
    DataGap,
    EffectiveExposure,
    EvidencePath,
    Exposure,
    ReasonCode,
)
from ubo_sentinel.models.provenance import NonEmptyStr

BlockSet = Literal["BLOCKED", "POSSIBLY_BLOCKED"]
# SEED: a designation put the node in the set. AGGREGATE: its owners in the set
# hold enough of it. PRESUMED_MAJORITY: an edge without a percentage to such an owner.
DerivationBasis = Literal["SEED", "AGGREGATE", "PRESUMED_MAJORITY"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _sorted_unique(value: list[str]) -> list[str]:
    return sorted(set(value))


class NodeDerivation(_Model):
    """Why one node is blocked or possibly blocked."""

    node_id: NonEmptyStr
    block_set: BlockSet
    basis: DerivationBasis
    # The round of the fix-point that added the node; 0 for a seed.
    round: int = Field(ge=0)
    # What the node's owners in the set hold of it. May exceed 100 on over-reported stakes.
    aggregate_pct: Exposure | None = None
    edge_ids: list[str] = []
    sanction_ids: list[str] = []

    _sort = field_validator("edge_ids", "sanction_ids")(_sorted_unique)


class ReasonFinding(_Model):
    """One rule that fired, with what it rests on, so a memo can cite it."""

    code: ReasonCode
    # The node or edge the finding is about.
    subject_id: NonEmptyStr
    pct: Exposure | None = None
    edge_ids: list[str] = []
    sanction_ids: list[str] = []

    _sort = field_validator("edge_ids", "sanction_ids")(_sorted_unique)

    def sort_key(self) -> tuple[str, str]:
        return (self.code, self.subject_id)


class RuleResult(_Model):
    target_id: NonEmptyStr
    rule_pack_id: NonEmptyStr
    rule_pack_hash: NonEmptyStr
    recommendation: Recommendation
    reasons: list[ReasonCode] = []
    blocked_set: list[str] = []
    possibly_blocked_set: list[str] = []
    paths: list[EvidencePath] = []
    effective_exposure: EffectiveExposure = EffectiveExposure()
    data_gaps: list[DataGap] = []
    # What blocked owners hold of the target directly: the figure the legal test reads.
    aggregate_pct: Exposure
    findings: list[ReasonFinding] = []
    derivations: list[NodeDerivation] = []
    # What was looked at, so a CLEAR still says how far the screen went.
    max_depth: int = Field(ge=0)
    nodes_checked: int = Field(ge=1)
    edges_checked: int = Field(ge=0)
    sources_checked: list[str] = []

    _sort = field_validator("reasons", "blocked_set", "possibly_blocked_set", "sources_checked")(
        _sorted_unique
    )

    @field_validator("paths")
    @classmethod
    def _sort_paths(cls, value: list[EvidencePath]) -> list[EvidencePath]:
        return sorted(value, key=EvidencePath.sort_key)

    @field_validator("data_gaps")
    @classmethod
    def _sort_gaps(cls, value: list[DataGap]) -> list[DataGap]:
        return sorted(value, key=DataGap.sort_key)

    @field_validator("findings")
    @classmethod
    def _sort_findings(cls, value: list[ReasonFinding]) -> list[ReasonFinding]:
        return sorted(value, key=ReasonFinding.sort_key)

    @field_validator("derivations")
    @classmethod
    def _sort_derivations(cls, value: list[NodeDerivation]) -> list[NodeDerivation]:
        return sorted(value, key=lambda item: item.node_id)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        overlap = set(self.blocked_set) & set(self.possibly_blocked_set)
        if overlap:
            raise ValueError(f"blocked and possibly-blocked sets overlap: {sorted(overlap)}")
        if (self.target_id in self.blocked_set) != (self.recommendation == "ESCALATE"):
            raise ValueError("ESCALATE is recommended exactly when the target is blocked")
        if (self.recommendation == "CLEAR") != (not self.reasons):
            raise ValueError("a recommendation other than CLEAR needs a reason, and CLEAR has none")
        if {finding.code for finding in self.findings} != set(self.reasons):
            raise ValueError("every reason needs a finding, and every finding a reason")
        return self
