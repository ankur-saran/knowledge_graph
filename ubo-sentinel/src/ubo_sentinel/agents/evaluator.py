"""Run the gold scenarios through the whole pipeline and compare."""

import time
from typing import Any

import duckdb

from ubo_sentinel import __version__
from ubo_sentinel.agents.base import PIPELINE_VERSION, ScreenRequest
from ubo_sentinel.agents.supervisor import NeedsDisambiguation, Recommended, Supervisor
from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.decision import Decision
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
    started = time.perf_counter()
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
    seconds = time.perf_counter() - started
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
        seconds=seconds,
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


# --- golden decisions -------------------------------------------------------------------


def golden_of(
    reports: list[ScenarioReport], snapshot_set_id: str, rule_pack_hash: str
) -> dict[str, Any]:
    """What the scenarios' decisions are, as ids and payload hashes, to be kept in a file.

    The audit log catches a decision that changed under its id only when the
    old decision is in that log. Kept in the repository, these catch it across
    commits: a change to what a screen produces that did not raise a version.
    """
    return {
        "snapshot_set_id": snapshot_set_id,
        "rule_pack_hash": rule_pack_hash,
        "engine_version": __version__,
        "pipeline_version": PIPELINE_VERSION,
        "decisions": {
            report.scenario_id: {
                "decision_id": report.result.decision_id,
                "payload_sha256": sha256_hex(report.result.canonical_payload()),
            }
            for report in sorted(reports, key=lambda item: item.scenario_id)
            if isinstance(report.result, Decision)
        },
    }


def compare_golden(golden: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Why the decisions are not the golden ones. Empty when they are.

    One id with two payloads is the fault this exists for. An id that moved
    is a version, a rule pack or a fixture that moved: the file is then out of
    date and is written again, which is a change someone reviews.
    """
    problems = []
    for name in ("snapshot_set_id", "rule_pack_hash"):
        if golden.get(name) != current[name]:
            problems.append(
                f"{name} is {current[name]}, the golden file was written for"
                f" {golden.get(name)}: the fixtures or the rule pack changed."
                " Write it again with 'ubo eval --suite pipeline --update-golden'."
            )
    if problems:
        return problems
    was, now = golden.get("decisions", {}), current["decisions"]
    for scenario_id in sorted(set(was) | set(now)):
        old, new = was.get(scenario_id), now.get(scenario_id)
        if old is None or new is None:
            problems.append(f"{scenario_id}: {'not in the golden file' if new else 'no decision'}.")
        elif old["decision_id"] != new["decision_id"]:
            problems.append(
                f"{scenario_id}: the decision id is {new['decision_id']}, was"
                f" {old['decision_id']}: a version moved. Write the golden file again."
            )
        elif old["payload_sha256"] != new["payload_sha256"]:
            problems.append(
                f"{scenario_id}: decision {new['decision_id']} has another payload"
                " under the same id. Code that changes what a screen produces must raise"
                " PIPELINE_VERSION, RULES_VERSION or TRANSFORM_VERSION."
            )
    return problems
