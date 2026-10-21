"""`ubo eval`: run the gold scenarios and the labelled entity-resolution set."""

import json
import math
import tempfile
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import BaseModel, ValidationError

from ubo_sentinel.agents.evaluator import compare_golden, golden_of, run_pipeline_suite
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.replay import replay_all
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.graph.entity_resolution import ErReport, evaluate_labelled
from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
from ubo_sentinel.models.gold_scenario import load_gold_scenarios
from ubo_sentinel.models.ontology import load_ontology
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.rules.engine import RuleEngine
from ubo_sentinel.rules.evaluator import ScenarioReport, run_rules_suite
from ubo_sentinel.rules.pack import DEFAULT_PACK, UnknownRulePack, load_rule_pack, pack_path

# Commands are run from the repository root.
GOLD_PATH = Path("eval/gold_scenarios.yaml")
# Family -> the labelled names entity resolution is scored on. The gold
# scenarios are written for the fixtures; a real set is scored on real names.
ER_PATHS = {
    bronze.FAMILY_FIXTURES: Path("eval/er_labelled.csv"),
    bronze.FAMILY_REAL: Path("eval/er_labelled_real.csv"),
}
# The decisions of the gold scenarios on the fixtures: `golden_of()` in `agents/evaluator.py`.
GOLDEN_PATH = Path("eval/golden_decisions.json")

# PRD section 10.
DECISION_ACCURACY_TARGET = 1.0
PATH_RECALL_TARGET = 0.98
ER_PRECISION_TARGET = 0.95
REPLAY_FIDELITY_TARGET = 1.0
UNCITED_CLAIMS_TARGET = 0
# PRD section 7: a screen on the fixtures.
LATENCY_P95_TARGET_SECONDS = 5.0


class Suite(StrEnum):
    rules = "rules"
    pipeline = "pipeline"
    er = "er"
    all = "all"


@dataclass(frozen=True)
class Metric:
    """One measured value and the target it is held to."""

    suite: str
    name: str
    value: float
    target: float
    # "min": the value must be at least the target; "max": at most.
    bound: str
    unit: str
    met: bool


@dataclass
class _Output:
    """What the command says: lines as it goes, or one JSON document at the end."""

    as_json: bool
    metrics: list[Metric] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def echo(self, text: str) -> None:
        if not self.as_json:
            typer.echo(text)

    def metric(
        self,
        suite: str,
        name: str,
        value: float,
        target: float,
        bound: str = "min",
        unit: str = "ratio",
    ) -> bool:
        met = value >= target if bound == "min" else value <= target
        self.metrics.append(Metric(suite, name, value, target, bound, unit, met))
        if unit == "ratio":
            shown, wanted = f"{value:>7.1%}", f"(target {target:.0%})"
        elif unit == "seconds":
            shown, wanted = f"{value:>6.2f}s", f"(target < {target:.0f}s)"
        else:
            shown, wanted = f"{value:>7.0f} ", f"(target {target:.0f}) "
        word = "ok" if met else ("BELOW" if bound == "min" else "ABOVE")
        self.echo(f"  {name:<22} {shown}  {wanted}  {word}")
        return met


def p95(values: list[float]) -> float:
    """The value at or below which 95 % of the values lie (nearest rank)."""
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else 0.0


def _show(value: object) -> str:
    if isinstance(value, BaseModel):
        return str(value.model_dump(mode="json"))
    if isinstance(value, list):
        return "[" + ", ".join(_show(item) for item in value) + "]"
    return str(value)


def _print_scenarios(
    out: _Output, suite: str, title: str, reports: list[ScenarioReport], skipped: list[str]
) -> bool:
    out.echo(title)
    for report in reports:
        status = "PASS" if report.passed else "FAIL"
        result = report.result
        recommendation = result.recommendation if result else "-"
        line = f"  {report.scenario_id}  {status}  {recommendation:<8} "
        out.echo((line + ", ".join(result.reasons if result else [])).rstrip())
        for diff in report.diffs:
            out.echo(f"      {diff.field}: expected {_show(diff.expected)}")
            out.echo(f"      {' ' * len(diff.field)}  actual   {_show(diff.actual)}")
        if not report.passed:
            out.problems.append(f"{suite}: {report.scenario_id} failed")
            # The rule engine's trace; a decision carries it in its memo.
            for why in getattr(result, "derivations", []):
                out.echo(f"      derived: {_show(why)}")
    if skipped:
        out.echo(f"  Not run here (pipeline suite): {', '.join(skipped)}")

    passed = sum(report.passed for report in reports)
    expected = sum(report.paths_expected for report in reports)
    found = sum(report.paths_found for report in reports)
    out.echo(f"  {passed}/{len(reports)} scenarios passed")
    accuracy = out.metric(
        suite,
        "Decision accuracy",
        passed / len(reports) if reports else 1.0,
        DECISION_ACCURACY_TARGET,
    )
    recall = out.metric(
        suite, "Evidence-path recall", found / expected if expected else 1.0, PATH_RECALL_TARGET
    )
    return accuracy and recall


def _print_pipeline(out: _Output, reports: list[ScenarioReport]) -> bool:
    title = "Gold scenarios (pipeline suite: query to decision)"
    ok = _print_scenarios(out, "pipeline", title, reports, [])
    uncited = sum(report.uncited_claims for report in reports)
    cited = out.metric(
        "pipeline", "Uncited claims", uncited, UNCITED_CLAIMS_TARGET, bound="max", unit="count"
    )
    fast = out.metric(
        "pipeline",
        "Screen latency p95",
        p95([report.seconds for report in reports]),
        LATENCY_P95_TARGET_SECONDS,
        bound="max",
        unit="seconds",
    )
    return ok and cited and fast


def _print_er(out: _Output, report: ErReport) -> bool:
    out.echo("Entity resolution (labelled set)")
    for query, expected, first in report.misses:
        out.echo(f"  MISS  '{query}': expected {expected}, got {first}")
    for query, entity_id, score in report.false_candidates:
        out.echo(f"  FALSE CANDIDATE  '{query}': {entity_id} at {score:.2f}")
        out.problems.append(f"er: '{query}' returned a candidate")
    met = out.metric("er", "ER precision@1", report.precision_at_1, ER_PRECISION_TARGET)
    return met and not report.false_candidates


def _golden(out: _Output, current: dict, update: bool) -> bool:
    """Compare the suite's decisions with the golden file, or write the file."""
    if update:
        text = json.dumps(current, indent=2, sort_keys=True) + "\n"
        GOLDEN_PATH.write_text(text, encoding="utf-8", newline="\n")
        out.echo(f"  Golden decisions       written to {GOLDEN_PATH.as_posix()}")
        return True
    if not GOLDEN_PATH.exists():
        out.echo(f"  Golden decisions       not compared: there is no {GOLDEN_PATH.as_posix()}")
        return True
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if any(golden.get(name) != current[name] for name in ("snapshot_set_id", "rule_pack_hash")):
        # Another set or another pack: the file says nothing about these decisions.
        out.echo("  Golden decisions       not compared: written for another set or rule pack")
        return True
    problems = compare_golden(golden, current)
    for problem in problems:
        out.echo(f"  GOLDEN  {problem}")
    out.problems += [f"golden: {problem}" for problem in problems]
    out.echo(f"  {'Golden decisions':<22} {'unchanged' if not problems else 'CHANGED'}")
    return not problems


@app.command(name="eval")
def evaluate(
    suite: Annotated[Suite, typer.Option("--suite", help="What to evaluate.")] = Suite.all,
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, real, latest or a set id.")
    ] = bronze.ALIAS_FIXTURES,
    rule_pack: Annotated[
        str, typer.Option("--rule-pack", help="Rule pack name: a file in rules/.")
    ] = DEFAULT_PACK,
    role: Annotated[str, typer.Option("--role")] = "engineer",
    as_json: Annotated[
        bool, typer.Option("--json", help="Print every metric and its target as JSON.")
    ] = False,
    update_golden: Annotated[
        bool,
        typer.Option(
            "--update-golden",
            help=f"Write the pipeline suite's decisions to {GOLDEN_PATH.as_posix()}.",
        ),
    ] = False,
    labels: Annotated[
        Path | None,
        typer.Option("--labels", help="Labelled names for the er suite (default: by family)."),
    ] = None,
) -> None:
    """Run the gold scenarios through the rules and the pipeline, and score entity resolution."""
    require_role(role, "eval")
    if update_golden and suite not in (Suite.pipeline, Suite.all):
        typer.echo("--update-golden needs the pipeline suite.", err=True)
        raise typer.Exit(code=2)
    out = _Output(as_json)
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
            set_id = store.set_id
            family = store.snapshot_set.family
            if suite is not Suite.er and family != bronze.FAMILY_FIXTURES:
                typer.echo(
                    f"Snapshot set {set_id} is of family '{family}'. The gold scenarios are"
                    " written for the fixtures: run them on a fixtures set, or run"
                    " '--suite er' on this one.",
                    err=True,
                )
                raise typer.Exit(code=2)
            er_path = labels or ER_PATHS.get(family)
            if suite in (Suite.er, Suite.all) and (er_path is None or not er_path.is_file()):
                typer.echo(
                    f"No labelled names for family '{family}'"
                    f"{f' ({er_path.as_posix()})' if er_path else ''}; pass --labels.",
                    err=True,
                )
                raise typer.Exit(code=2)
            out.echo(
                f"Snapshot set: {store.set_id}   Rule pack: {engine.pack.id}"
                f" ({engine.rule_pack_hash[:16]})"
            )
            if suite in (Suite.rules, Suite.all):
                scenarios = load_gold_scenarios(GOLD_PATH)
                skipped = [
                    scenario.id
                    for scenario in scenarios
                    if scenario.suite != "rules" and suite is Suite.rules
                ]
                reports = run_rules_suite(store, engine, scenarios)
                title = "Gold scenarios (rules suite)"
                ok = _print_scenarios(out, "rules", title, reports, skipped) and ok
            if suite in (Suite.pipeline, Suite.all):
                # The suite records nothing: its log is a directory that is thrown away.
                with tempfile.TemporaryDirectory() as scratch:
                    ontology, log = load_ontology(ONTOLOGY_PATH), AuditLog(Path(scratch))
                    reports = run_pipeline_suite(
                        Supervisor(ontology, log),
                        load_gold_scenarios(GOLD_PATH),
                        store.set_id,
                        store.con,
                        engine.pack,
                    )
                    # Every decision the suite recorded, replayed from its event.
                    replays = replay_all(log, ontology, store.con)
                ok = _print_pipeline(out, reports) and ok
                for replay in replays:
                    if not replay.identical:
                        out.echo(
                            f"  REPLAY {replay.decision_id}  {replay.verdict}  {replay.detail}"
                        )
                        out.problems.append(f"replay: {replay.decision_id} {replay.verdict}")
                identical = sum(replay.identical for replay in replays)
                ok = (
                    out.metric(
                        "pipeline",
                        "Replay fidelity",
                        identical / len(replays) if replays else 1.0,
                        REPLAY_FIDELITY_TARGET,
                    )
                    and ok
                )
                current = golden_of(reports, store.set_id, engine.rule_pack_hash)
                ok = _golden(out, current, update_golden) and ok
            if suite in (Suite.er, Suite.all):
                ok = _print_er(out, evaluate_labelled(store, er_path)) and ok
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo ingest' first.", err=True)
        raise typer.Exit(code=2) from exc
    except GraphNotBuilt as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        document = {
            "ok": ok,
            "snapshot_set_id": set_id,
            "rule_pack": {"id": engine.pack.id, "hash": engine.rule_pack_hash},
            "metrics": [asdict(metric) for metric in out.metrics],
            "problems": out.problems,
        }
        typer.echo(json.dumps(document, indent=2))
    if not ok:
        raise typer.Exit(code=1)
