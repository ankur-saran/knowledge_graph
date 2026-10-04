"""Schema for `eval/gold_scenarios.yaml` and its loader.

A gold scenario states what the rule engine must conclude for one fixture
target, and why. The evaluator and the integration tests load it through here.
"""

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ubo_sentinel.models.decision import Recommendation
from ubo_sentinel.models.evidence import EffectiveExposure, EvidencePath, GapCode, ReasonCode
from ubo_sentinel.models.provenance import NonEmptyStr
from ubo_sentinel.models.relationship import RelType, relationship_id

# `rules` scenarios run on the rule engine alone; `pipeline` ones need the agents.
Suite = Literal["rules", "pipeline"]
# How close two names are meant to be: in the review band, at or above the match
# threshold, or identical names of different entities.
NameBand = Literal["review", "blocked", "homonym"]

ScenarioId = Annotated[str, Field(pattern=r"^SCEN-\d{2}$")]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpectedEdge(_Spec):
    """An edge named by its ends, in the ontology's direction (subject -> object)."""

    subject_id: NonEmptyStr
    object_id: NonEmptyStr
    rel_type: RelType
    is_ultimate: bool = False

    def edge_id(self) -> str:
        return relationship_id(self.subject_id, self.object_id, self.rel_type, self.is_ultimate)


class ExpectedPath(_Spec):
    """A path from a blocked or possibly-blocked party to the target.

    `via[i]` is the relation between `nodes[i]` and `nodes[i + 1]`. Edge ids are
    derived, so the file never holds a hash. Ultimate-parent edges are not used.
    """

    nodes: list[NonEmptyStr] = Field(min_length=1)
    via: list[RelType] = []

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if len(self.via) != len(self.nodes) - 1:
            raise ValueError("a path needs exactly one fewer relation than nodes")
        return self

    def edges(self) -> list[ExpectedEdge]:
        edges = []
        for upper, lower, rel_type in zip(self.nodes[:-1], self.nodes[1:], self.via, strict=True):
            # A path runs owner -> asset; CONSOLIDATED_BY is stored child -> parent.
            if rel_type == "CONSOLIDATED_BY":
                upper, lower = lower, upper
            edges.append(ExpectedEdge(subject_id=upper, object_id=lower, rel_type=rel_type))
        return edges

    def to_evidence_path(self) -> EvidencePath:
        return EvidencePath(node_ids=self.nodes, edge_ids=[edge.edge_id() for edge in self.edges()])


class ExpectedGap(_Spec):
    code: GapCode
    # Exactly one of `node` and `edge` says what the gap is about.
    node: NonEmptyStr | None = None
    edge: ExpectedEdge | None = None
    decision_relevant: bool

    @model_validator(mode="after")
    def _check_subject(self) -> Self:
        if (self.node is None) == (self.edge is None):
            raise ValueError("a gap needs exactly one of 'node' and 'edge'")
        return self

    def subject_id(self) -> str:
        return self.node if self.node is not None else self.edge.edge_id()


class DeclaredNamePair(_Spec):
    """Two different entities whose names are meant to be similar."""

    a: NonEmptyStr
    b: NonEmptyStr
    band: NameBand


class GoldScenario(_Spec):
    id: ScenarioId
    suite: Suite = "rules"
    query: NonEmptyStr
    # The entity to screen; the analyst's choice when the query is ambiguous.
    target_id: NonEmptyStr | None
    expect_disambiguation: bool = False
    # A scenario that differs from this one by a single fact and has another outcome.
    twin_of: ScenarioId | None = None
    expected_recommendation: Recommendation
    # The outcome on the changed sanctions list, where it differs.
    expected_recommendation_t1: Recommendation | None = None
    expected_reasons: list[ReasonCode] = []
    expected_blocked: list[NonEmptyStr] = []
    expected_possibly_blocked: list[NonEmptyStr] = []
    # Left out where the exposure is not what the scenario is about.
    expected_exposure: EffectiveExposure | None = None
    # The paths that must be reported; the engine may report more.
    expected_paths: list[ExpectedPath] = []
    expected_gaps: list[ExpectedGap] = []
    declared_name_pairs: list[DeclaredNamePair] = []
    rationale: NonEmptyStr

    @field_validator("expected_reasons", "expected_blocked", "expected_possibly_blocked")
    @classmethod
    def _sort_unique[T: str](cls, value: list[T]) -> list[T]:
        return sorted(set(value))

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        overlap = set(self.expected_blocked) & set(self.expected_possibly_blocked)
        if overlap:
            raise ValueError(f"blocked and possibly-blocked sets overlap: {sorted(overlap)}")
        if self.target_id is None and self.suite != "pipeline":
            raise ValueError("a scenario without a target cannot run in the rules suite")
        blocked = self.target_id in self.expected_blocked
        if blocked != (self.expected_recommendation == "ESCALATE"):
            raise ValueError("ESCALATE is expected exactly when the target is blocked")
        if self.twin_of == self.id:
            raise ValueError("a scenario cannot be its own twin")
        return self


def load_gold_scenarios(path: Path) -> list[GoldScenario]:
    """Load and validate the gold scenarios.

    Raises `OSError`, `yaml.YAMLError`, `pydantic.ValidationError`, or `ValueError`
    on a duplicate id.
    """
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    scenarios = TypeAdapter(list[GoldScenario]).validate_python(data)
    ids = [scenario.id for scenario in scenarios]
    duplicates = sorted({id_ for id_ in ids if ids.count(id_) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate scenario ids: {', '.join(duplicates)}")
    return scenarios
