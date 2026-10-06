"""Compare what the rule engine concludes with what a gold scenario expects."""

from dataclasses import dataclass
from typing import Any

from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.gold_scenario import GoldScenario
from ubo_sentinel.models.rule_result import RuleResult
from ubo_sentinel.rules.engine import RuleEngine


@dataclass(frozen=True)
class FieldDiff:
    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ScenarioReport:
    scenario_id: str
    result: RuleResult
    diffs: list[FieldDiff]
    # Evidence paths the scenario expects, and how many of them were reported.
    paths_expected: int
    paths_found: int

    @property
    def passed(self) -> bool:
        return not self.diffs


def screen(
    store: GraphStore, engine: RuleEngine, target_id: str, max_depth: int | None = None
) -> RuleResult:
    """Run the rules for one entity of the store's set. The depths are the pack's unless given."""
    pack = engine.pack
    depth = pack.max_depth if max_depth is None else max_depth
    subgraph = queries.get_ownership_subgraph(store, target_id, depth, pack.max_depth_down)
    return engine.evaluate(subgraph, queries.get_sanctioned_nodes(store, subgraph))


def compare(result: RuleResult, scenario: GoldScenario) -> ScenarioReport:
    """Every field in which the result differs from the scenario.

    Reasons, the two sets and the gaps must be equal, so a scenario cannot pass
    for the wrong reason. Paths are a minimum: the engine may report more.
    Exposure is compared where the scenario gives it.
    """
    diffs: list[FieldDiff] = []

    def check(field: str, expected: Any, actual: Any) -> None:
        if expected != actual:
            diffs.append(FieldDiff(field, expected, actual))

    check("recommendation", scenario.expected_recommendation, result.recommendation)
    check("reasons", scenario.expected_reasons, result.reasons)
    check("blocked", scenario.expected_blocked, result.blocked_set)
    check("possibly_blocked", scenario.expected_possibly_blocked, result.possibly_blocked_set)
    if scenario.expected_exposure is not None:
        check("exposure", scenario.expected_exposure, result.effective_exposure)

    expected_paths = [path.to_evidence_path() for path in scenario.expected_paths]
    missing = [path for path in expected_paths if path not in result.paths]
    if missing:
        diffs.append(FieldDiff("paths", missing, result.paths))

    expected_gaps = sorted(
        {(gap.code, gap.subject_id(), gap.decision_relevant) for gap in scenario.expected_gaps}
    )
    actual_gaps = sorted(
        {(gap.code, gap.subject_id, gap.decision_relevant) for gap in result.data_gaps}
    )
    check("gaps", expected_gaps, actual_gaps)

    return ScenarioReport(
        scenario_id=scenario.id,
        result=result,
        diffs=diffs,
        paths_expected=len(expected_paths),
        paths_found=len(expected_paths) - len(missing),
    )


def run_rules_suite(
    store: GraphStore, engine: RuleEngine, scenarios: list[GoldScenario]
) -> list[ScenarioReport]:
    """Screen the target of every `rules` scenario and compare."""
    return [
        compare(screen(store, engine, scenario.target_id), scenario)
        for scenario in scenarios
        if scenario.suite == "rules"
    ]
