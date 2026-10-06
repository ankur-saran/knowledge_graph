"""Run the gold scenarios through the whole pipeline and compare."""

import duckdb

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import NeedsDisambiguation, Recommended, Supervisor
from ubo_sentinel.models.gold_scenario import GoldScenario
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.rules.evaluator import FieldDiff, ScenarioReport, compare
from ubo_sentinel.rules.pack import RulePack


def screen_scenario(
    supervisor: Supervisor,
    scenario: GoldScenario,
    snapshot: str,
    con: duckdb.DuckDBPyConnection | None = None,
    pack: RulePack | None = None,
) -> ScenarioReport:
    """Screen a scenario's query as an analyst would, and compare with what it expects.

    Where the query is ambiguous, the analyst's pick is the scenario's target.
    On top of the rule engine's comparison: the query resolves to the target, a
    pick is asked for exactly where the scenario says, the expected paths are
    in the memo, and the guardrail has nothing to say.
    """
    request = ScreenRequest(query=scenario.query, snapshot=snapshot)
    extra: list[FieldDiff] = []
    outcome = supervisor.run(request, con, pack)
    asked = isinstance(outcome, NeedsDisambiguation)
    if asked != scenario.expect_disambiguation:
        extra.append(FieldDiff("disambiguation", scenario.expect_disambiguation, asked))
    if asked:
        ids = [match.entity_id for match in outcome.candidates]
        if scenario.target_id not in ids:
            diff = FieldDiff("candidates", scenario.target_id, ids)
            return ScenarioReport(
                scenario.id, None, [*extra, diff], len(scenario.expected_paths), 0
            )
        pick = ids.index(scenario.target_id) + 1
        outcome = supervisor.run(request.model_copy(update={"pick": pick}), con, pack)
    if not isinstance(outcome, Recommended):
        diff = FieldDiff("screen", "a decision", outcome)
        return ScenarioReport(scenario.id, None, [*extra, diff], len(scenario.expected_paths), 0)

    decision = outcome.decision
    report = compare(decision, scenario)
    memo = Memo.model_validate(decision.memo_json)
    if decision.target_id != scenario.target_id:
        extra.append(FieldDiff("target", scenario.target_id, decision.target_id))
    if decision.guardrail_warnings:
        extra.append(FieldDiff("guardrail", [], decision.guardrail_warnings))
    # PRD section 10: the expected paths are present in the memo.
    expected = [path.to_evidence_path() for path in scenario.expected_paths]
    in_memo = [path for path in expected if path in memo.paths]
    if len(in_memo) < len(expected):
        extra.append(FieldDiff("memo paths", expected, memo.paths))
    return ScenarioReport(
        scenario_id=scenario.id,
        result=decision,
        diffs=[*report.diffs, *extra],
        paths_expected=len(expected),
        paths_found=len(in_memo),
        uncited_claims=len(memo.uncited()),
    )


def run_pipeline_suite(
    supervisor: Supervisor,
    scenarios: list[GoldScenario],
    snapshot: str,
    con: duckdb.DuckDBPyConnection | None = None,
    pack: RulePack | None = None,
) -> list[ScenarioReport]:
    """Every scenario, of either suite: the pipeline must agree with the rule engine."""
    return [screen_scenario(supervisor, scenario, snapshot, con, pack) for scenario in scenarios]
