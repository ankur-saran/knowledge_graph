"""`ubo eval`: run the gold scenarios and the labelled entity-resolution set."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import BaseModel, ValidationError

from ubo_sentinel.cli.app import app, require_role
from ubo_sentinel.graph.entity_resolution import ErReport, evaluate_labelled
from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
from ubo_sentinel.models.gold_scenario import load_gold_scenarios
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.rules.engine import RuleEngine
from ubo_sentinel.rules.evaluator import ScenarioReport, run_rules_suite
from ubo_sentinel.rules.pack import DEFAULT_PACK, UnknownRulePack, load_rule_pack, pack_path

# Commands are run from the repository root.
GOLD_PATH = Path("eval/gold_scenarios.yaml")
ER_PATH = Path("eval/er_labelled.csv")

# PRD section 10.
DECISION_ACCURACY_TARGET = 1.0
PATH_RECALL_TARGET = 0.98
ER_PRECISION_TARGET = 0.95


class Suite(StrEnum):
    rules = "rules"
    er = "er"
    all = "all"


def _show(value: object) -> str:
    if isinstance(value, BaseModel):
        return str(value.model_dump(mode="json"))
    if isinstance(value, list):
        return "[" + ", ".join(_show(item) for item in value) + "]"
    return str(value)


def _metric(name: str, value: float, target: float) -> bool:
    met = value >= target
    typer.echo(f"  {name:<22} {value:>7.1%}  (target {target:.0%})  {'ok' if met else 'BELOW'}")
    return met


def _print_rules(reports: list[ScenarioReport], skipped: list[str]) -> bool:
    typer.echo("Gold scenarios (rules suite)")
    for report in reports:
        status = "PASS" if report.passed else "FAIL"
        result = report.result
        line = f"  {report.scenario_id}  {status}  {result.recommendation:<8} "
        typer.echo((line + ", ".join(result.reasons)).rstrip())
        for diff in report.diffs:
            typer.echo(f"      {diff.field}: expected {_show(diff.expected)}")
            typer.echo(f"      {' ' * len(diff.field)}  actual   {_show(diff.actual)}")
        if not report.passed:
            for why in report.result.derivations:
                typer.echo(f"      derived: {_show(why)}")
    if skipped:
        typer.echo(f"  Not run here (pipeline suite, Step 7): {', '.join(skipped)}")

    passed = sum(report.passed for report in reports)
    expected = sum(report.paths_expected for report in reports)
    found = sum(report.paths_found for report in reports)
    typer.echo(f"  {passed}/{len(reports)} scenarios passed")
    accuracy = _metric(
        "Decision accuracy", passed / len(reports) if reports else 1.0, DECISION_ACCURACY_TARGET
    )
    recall = _metric(
        "Evidence-path recall", found / expected if expected else 1.0, PATH_RECALL_TARGET
    )
    return accuracy and recall


def _print_er(report: ErReport) -> bool:
    typer.echo("Entity resolution (labelled set)")
    for query, expected, first in report.misses:
        typer.echo(f"  MISS  '{query}': expected {expected}, got {first}")
    for query, entity_id, score in report.false_candidates:
        typer.echo(f"  FALSE CANDIDATE  '{query}': {entity_id} at {score:.2f}")
    met = _metric("ER precision@1", report.precision_at_1, ER_PRECISION_TARGET)
    return met and not report.false_candidates


@app.command(name="eval")
def evaluate(
    suite: Annotated[Suite, typer.Option("--suite", help="What to evaluate.")] = Suite.all,
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, latest or a set id.")
    ] = bronze.ALIAS_FIXTURES,
    rule_pack: Annotated[
        str, typer.Option("--rule-pack", help="Rule pack name: a file in rules/.")
    ] = DEFAULT_PACK,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Run the gold scenarios through the rule engine and score entity resolution."""
    require_role(role, "eval")
    try:
        engine = RuleEngine(load_rule_pack(pack_path(rule_pack)))
    except UnknownRulePack as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read rule pack '{rule_pack}': {exc}", err=True)
        raise typer.Exit(code=2) from exc

    ok = True
    try:
        with GraphStore.open(snapshot) as store:
            typer.echo(
                f"Snapshot set: {store.set_id}   Rule pack: {engine.pack.id}"
                f" ({engine.rule_pack_hash[:16]})"
            )
            if suite in (Suite.rules, Suite.all):
                scenarios = load_gold_scenarios(GOLD_PATH)
                skipped = [scenario.id for scenario in scenarios if scenario.suite != "rules"]
                ok = _print_rules(run_rules_suite(store, engine, scenarios), skipped) and ok
            if suite in (Suite.er, Suite.all):
                ok = _print_er(evaluate_labelled(store, ER_PATH)) and ok
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo ingest' first.", err=True)
        raise typer.Exit(code=2) from exc
    except GraphNotBuilt as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if not ok:
        raise typer.Exit(code=1)
