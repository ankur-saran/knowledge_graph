"""Pack scenarios: small graphs written inline, each judged under every rule pack.

`eval/pack_scenarios.yaml` shows where the packs differ (a stake of exactly half,
holdings added together, control). The rule engine is pure, so a scenario needs
no fixtures and no database: the graph is built here, as `tests/graphs.py` does.
A designation names its list by role: `own` is the pack's own list (the first of
its `designation_lists`), `other` a list no pack applies. One graph is therefore
read the same way under every pack.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ubo_sentinel.models import (
    Entity,
    Provenance,
    Relationship,
    Sanction,
    SanctionMatch,
    SubGraph,
    relationship_id,
    sanction_id,
)
from ubo_sentinel.models.decision import Recommendation
from ubo_sentinel.models.evidence import GapCode, ReasonCode
from ubo_sentinel.models.graph import upper_lower
from ubo_sentinel.models.provenance import Confidence, NonEmptyStr
from ubo_sentinel.models.relationship import RelType
from ubo_sentinel.models.rule_result import RuleResult
from ubo_sentinel.rules.engine import RuleEngine
from ubo_sentinel.rules.pack import RULES_DIR, RulePack, load_rule_pack

# Commands are run from the repository root.
PACK_SCENARIOS_PATH = Path("eval/pack_scenarios.yaml")
# A list no pack applies.
OTHER_LIST = "FX-OTHER-LIST"

_PROVENANCE = Provenance(
    source="pack_scenario",
    source_record_id="inline",
    snapshot_id="inline",
    as_of=date(2026, 1, 1),
    confidence=1.0,
)
_BASIS = {
    "OWNS": "shareholding",
    "CONSOLIDATED_BY": "accounting_consolidation",
    "CONTROLS": "directorship",
}


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SpecEdge(_Spec):
    """An edge named by its upper end (owner, parent or controller) and lower end."""

    upper: NonEmptyStr
    lower: NonEmptyStr
    rel: RelType = "OWNS"
    pct: Decimal | None = Field(default=None, gt=0, le=100)


class SpecDesignation(_Spec):
    """A designation that reaches `node`: its own, or one of `by` across a name link."""

    node: NonEmptyStr
    by: NonEmptyStr | None = None
    confidence: Confidence = 1.0
    list: Literal["own", "other"] = "own"


class Expectation(_Spec):
    recommendation: Recommendation
    reasons: list[ReasonCode] = []
    blocked: list[NonEmptyStr] = []
    possibly_blocked: list[NonEmptyStr] = []
    # Gap codes only: the graphs are small enough that the code names the gap.
    gaps: list[GapCode] = []

    @field_validator("reasons", "blocked", "possibly_blocked", "gaps")
    @classmethod
    def _sort_unique[T: str](cls, value: list[T]) -> list[T]:
        return sorted(set(value))


class PackScenario(_Spec):
    id: str = Field(pattern=r"^PACK-\d{2}$")
    title: NonEmptyStr
    target: NonEmptyStr
    edges: list[SpecEdge] = []
    designations: list[SpecDesignation] = []
    # Pack file stem (`rules/<stem>.yaml`) -> what it must conclude.
    expected: dict[str, Expectation] = Field(min_length=1)
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def _check_nodes(self) -> Self:
        nodes = {self.target} | {end for e in self.edges for end in (e.upper, e.lower)}
        unknown = sorted({d.node for d in self.designations} - nodes)
        if unknown:
            raise ValueError(f"designations reach nodes not in the graph: {unknown}")
        return self

    def subgraph(self) -> SubGraph:
        edges = [_relationship(edge) for edge in self.edges]
        depths = _upward_depths(self.target, edges)
        ids = {self.target} | {end for e in edges for end in (e.subject_id, e.object_id)}
        nodes = [
            Entity(
                id=node_id,
                legal_name=node_id,
                status="ACTIVE",
                entity_type="LegalEntity",
                provenance=_PROVENANCE,
            )
            for node_id in sorted(ids)
        ]
        return SubGraph(
            target_id=self.target,
            snapshot_set_id="inline",
            max_depth_up=len(ids),
            max_depth_down=1,
            nodes=nodes,
            edges=edges,
            depth_up=depths,
            down_ids=sorted(ids - set(depths)),
        )

    def matches(self, pack: RulePack) -> list[SanctionMatch]:
        return [_match(spec, pack) for spec in self.designations]


def _relationship(spec: SpecEdge) -> Relationship:
    subject, obj = (
        (spec.lower, spec.upper) if spec.rel == "CONSOLIDATED_BY" else (spec.upper, spec.lower)
    )
    return Relationship(
        id=relationship_id(subject, obj, spec.rel, False),
        subject_id=subject,
        object_id=obj,
        rel_type=spec.rel,
        pct=spec.pct,
        basis=_BASIS[spec.rel],
        is_ultimate=False,
        provenance=_PROVENANCE,
    )


def _upward_depths(target: str, edges: list[Relationship]) -> dict[str, int]:
    owners: dict[str, set[str]] = {}
    for item in edges:
        upper, lower = upper_lower(item)
        owners.setdefault(lower, set()).add(upper)
    depths, frontier, hops = {target: 0}, [target], 0
    while frontier:
        hops += 1
        found = {u for node in frontier for u in owners.get(node, ()) if u not in depths}
        depths.update(dict.fromkeys(found, hops))
        frontier = sorted(found)
    return depths


def _match(spec: SpecDesignation, pack: RulePack) -> SanctionMatch:
    by = spec.by or spec.node
    match_type = "DIRECT" if by == spec.node else "FUZZY_NAME"
    record = Sanction(
        id=sanction_id("pack_scenario", f"des-{by}", spec.node),
        entity_id=spec.node,
        designated_entity_id=by,
        program="P",
        list_source=pack.designation_lists[0] if spec.list == "own" else OTHER_LIST,
        is_active=True,
        match_type=match_type,
        match_confidence=spec.confidence,
        provenance=_PROVENANCE.model_copy(update={"source_record_id": f"des-{by}"}),
    )
    return SanctionMatch(
        entity_id=spec.node,
        designated_entity_id=by,
        match_type=match_type,
        confidence=spec.confidence,
        is_active=True,
        jurisdictions_agree=True,
        sanction_records=[record],
    )


def load_pack_scenarios(path: Path = PACK_SCENARIOS_PATH) -> list[PackScenario]:
    """Raises `OSError`, `yaml.YAMLError`, `pydantic.ValidationError`, or `ValueError`."""
    with path.open(encoding="utf-8") as f:
        scenarios = TypeAdapter(list[PackScenario]).validate_python(yaml.safe_load(f))
    ids = [scenario.id for scenario in scenarios]
    duplicates = sorted({id_ for id_ in ids if ids.count(id_) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate scenario ids: {', '.join(duplicates)}")
    return scenarios


def shipped_packs(rules_dir: Path = RULES_DIR) -> dict[str, RulePack]:
    """Every pack in `rules/`, by file stem."""
    return {path.stem: load_rule_pack(path) for path in sorted(rules_dir.glob("*.yaml"))}


@dataclass(frozen=True)
class PackOutcome:
    scenario_id: str
    pack: str
    result: RuleResult
    # Field -> (expected, actual), for every field that differs.
    diffs: dict[str, tuple[object, object]]

    @property
    def passed(self) -> bool:
        return not self.diffs


def judge(scenario: PackScenario, stem: str, pack: RulePack) -> PackOutcome:
    result = RuleEngine(pack).evaluate(scenario.subgraph(), scenario.matches(pack))
    expected = scenario.expected[stem]
    actual = {
        "recommendation": result.recommendation,
        "reasons": sorted(set(result.reasons)),
        "blocked": sorted(result.blocked_set),
        "possibly_blocked": sorted(result.possibly_blocked_set),
        "gaps": sorted({gap.code for gap in result.data_gaps}),
    }
    diffs = {
        name: (getattr(expected, name), value)
        for name, value in actual.items()
        if getattr(expected, name) != value
    }
    return PackOutcome(scenario.id, stem, result, diffs)


@dataclass(frozen=True)
class PackReport:
    packs: list[str]
    outcomes: list[PackOutcome]
    # Scenario -> packs in `rules/` it states no expectation for.
    unstated: dict[str, list[str]]

    @property
    def agreement(self) -> float:
        total = len(self.outcomes) + sum(len(v) for v in self.unstated.values())
        return sum(o.passed for o in self.outcomes) / total if total else 1.0


def run_pack_suite(scenarios: list[PackScenario], packs: dict[str, RulePack]) -> PackReport:
    """Each scenario under each pack. A pack a scenario does not state counts as a failure,
    so a new pack cannot be added without saying what it concludes on every graph."""
    outcomes, unstated = [], {}
    for scenario in scenarios:
        unknown = sorted(set(scenario.expected) - set(packs))
        if unknown:
            raise ValueError(f"{scenario.id} states packs that are not in rules/: {unknown}")
        missing = sorted(set(packs) - set(scenario.expected))
        if missing:
            unstated[scenario.id] = missing
        for stem in sorted(set(packs) & set(scenario.expected)):
            outcomes.append(judge(scenario, stem, packs[stem]))
    return PackReport(sorted(packs), outcomes, unstated)
