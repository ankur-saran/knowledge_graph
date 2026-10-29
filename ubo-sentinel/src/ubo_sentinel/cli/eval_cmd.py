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

from ubo_sentinel.agents import change_monitor
from ubo_sentinel.agents.evaluator import compare_golden, golden_of, run_pipeline_suite
from ubo_sentinel.agents.real_smoke import SmokeReport, load_smoke, run_smoke
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
from ubo_sentinel.rules.pack_scenarios import (
    PACK_SCENARIOS_PATH,
    PackReport,
    load_pack_scenarios,
    run_pack_suite,
    shipped_packs,
)

# Commands are run from the repository root.
GOLD_PATH = Path("eval/gold_scenarios.yaml")
# Family -> the labelled names entity resolution is scored on. The gold
# scenarios are written for the fixtures; a real set is scored on real names.
ER_PATHS = {
    bronze.FAMILY_FIXTURES: Path("eval/er_labelled.csv"),
    bronze.FAMILY_REAL: Path("eval/er_labelled_real.csv"),
}
# Invariants of real screens that hold on any refresh: `agents/real_smoke.py`.
SMOKE_PATH = Path("eval/real_smoke.yaml")
# The decisions of the gold scenarios on the fixtures: `golden_of()` in `agents/evaluator.py`.
GOLDEN_PATH = Path("eval/golden_decisions.json")

# PRD section 10.
DECISION_ACCURACY_TARGET = 1.0
PATH_RECALL_TARGET = 0.98
ER_PRECISION_TARGET = 0.95
REPLAY_FIDELITY_TARGET = 1.0
UNCITED_CLAIMS_TARGET = 0
REAL_SMOKE_TARGET = 1.0
# G6: every decision that newer data changes is on the re-screen queue.
RESCREEN_RECALL_TARGET = 1.0
# Every pack concludes what each pack scenario states for it.
PACK_AGREEMENT_TARGET = 1.0
# PRD section 7: a screen on the fixtures.
LATENCY_P95_TARGET_SECONDS = 5.0


class Suite(StrEnum):
    rules = "rules"
    pipeline = "pipeline"
    er = "er"
    real_smoke = "real-smoke"
    # The re-screen queue against screening everything again: the gold
    # scenarios on the set, then a later set of the family.
    monitor = "monitor"
    # Every pack in rules/ on the small inline graphs of eval/pack_scenarios.yaml.
    # Needs no database; part of `all` on any set.
    packs = "packs"
    # The fixtures: rules, pipeline, er, and monitor when a later fixtures set is
    # loaded. A real set: er, real-smoke.
    all = "all"


# Suites written for the fixtures, and suites run on published data.
FIXTURE_SUITES = {Suite.rules, Suite.pipeline, Suite.monitor}
REAL_SUITES = {Suite.real_smoke}


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


def _print_smoke(out: _Output, reports: list[SmokeReport]) -> bool:
    out.echo("Real smoke (invariants that hold on any refresh)")
    for report in reports:
        shown = report.recommendation or report.resolved_to or "-"
        status = "PASS" if report.passed else "FAIL"
        out.echo(f"  {report.entry_id}  {status}  {shown:<8} {', '.join(report.reasons)}".rstrip())
        for failure in report.failures:
            out.echo(f"      {failure}")
            out.problems.append(f"real-smoke: {report.entry_id} {failure}")
        if report.drift:
            out.echo(f"      DRIFT (not a failure): {report.drift}")
    passed = sum(report.passed for report in reports)
    return out.metric(
        "real-smoke",
        "Real smoke invariants",
        passed / len(reports) if reports else 1.0,
        REAL_SMOKE_TARGET,
    )


def _print_monitor(out: _Output, evaluation: change_monitor.MonitorEvaluation, curr: str) -> bool:
    out.echo(f"Change monitor (the gold scenarios' decisions against set {curr})")
    out.echo(
        f"  {len(evaluation.changed)} decisions change on the later set;"
        f" {len(evaluation.queued)} are queued"
    )
    for decision_id in evaluation.missed:
        out.echo(f"  MISSED  {decision_id} changes and is not queued")
        out.problems.append(f"monitor: {decision_id} changes and is not queued")
    met = out.metric("monitor", "Re-screen recall", evaluation.recall, RESCREEN_RECALL_TARGET)
    # Reported, not held to a target: a queued decision that does not change costs
    # a re-screen; a missed one is an entity dealt with on old data.
    out.metric("monitor", "Re-screen precision", evaluation.precision, 0.0)
    return met


def _print_packs(out: _Output, report: PackReport) -> bool:
    out.echo("Rule packs (eval/pack_scenarios.yaml: one graph, every pack)")
    out.echo(("  " + f"{'scenario':<9}" + "".join(f"{pack:<11}" for pack in report.packs)).rstrip())
    by_scenario: dict[str, dict[str, str]] = {}
    for outcome in report.outcomes:
        mark = "" if outcome.passed else "!"
        by_scenario.setdefault(outcome.scenario_id, {})[outcome.pack] = (
            outcome.result.recommendation + mark
        )
    for scenario_id, row in by_scenario.items():
        cells = "".join(f"{row.get(pack, '?'):<11}" for pack in report.packs)
        out.echo(f"  {scenario_id:<9}{cells}".rstrip())
    for outcome in report.outcomes:
        for name, (expected, actual) in outcome.diffs.items():
            out.echo(
                f"  FAIL {outcome.scenario_id} {outcome.pack} {name}: expected {_show(expected)}"
            )
            pad = " " * len(outcome.scenario_id + outcome.pack + name)
            out.echo(f"       {pad}  actual   {_show(actual)}")
        if outcome.diffs:
            out.problems.append(f"packs: {outcome.scenario_id} under {outcome.pack} failed")
    for scenario_id, packs in report.unstated.items():
        out.echo(f"  UNSTATED {scenario_id}: no expectation for {', '.join(packs)}")
        out.problems.append(f"packs: {scenario_id} states nothing for {', '.join(packs)}")
    return out.metric("packs", "Pack agreement", report.agreement, PACK_AGREEMENT_TARGET)


def _run_packs(out: _Output) -> bool:
    try:
        report = run_pack_suite(load_pack_scenarios(PACK_SCENARIOS_PATH), shipped_packs())
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        typer.echo(f"Cannot run the pack scenarios: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    return _print_packs(out, report)


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
    smoke: Annotated[
        Path,
        typer.Option("--smoke", help="Entries of the real-smoke suite."),
    ] = SMOKE_PATH,
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
    if suite is Suite.packs:
        # Inline graphs and the pure rule engine: no snapshot set is read.
        ok = _run_packs(out)
        if as_json:
            document = {
                "ok": ok,
                "snapshot_set_id": None,
                "rule_pack": None,
                "metrics": [asdict(metric) for metric in out.metrics],
                "problems": out.problems,
            }
            typer.echo(json.dumps(document, indent=2))
        if not ok:
            raise typer.Exit(code=1)
        return
    try:
        with GraphStore.open(snapshot) as store:
            set_id = store.set_id
            family = store.snapshot_set.family
            fixtures = family == bronze.FAMILY_FIXTURES
            if suite in (FIXTURE_SUITES if not fixtures else REAL_SUITES):
                typer.echo(
                    f"Snapshot set {set_id} is of family '{family}'. The gold scenarios are"
                    " written for the fixtures and the real-smoke entries for published"
                    " data: run 'rules' and 'pipeline' on a fixtures set, 'real-smoke' on a"
                    " real one ('er' and 'all' run on either).",
                    err=True,
                )
                raise typer.Exit(code=2)
            runs = {suite} if suite is not Suite.all else {Suite.er}
            if suite is Suite.all:
                runs |= FIXTURE_SUITES if fixtures else REAL_SUITES
            if Suite.real_smoke in runs and not smoke.is_file():
                typer.echo(f"No real-smoke entries ({smoke.as_posix()}); pass --smoke.", err=True)
                raise typer.Exit(code=2)
            er_path = labels or ER_PATHS.get(family)
            if Suite.er in runs and (er_path is None or not er_path.is_file()):
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
            if Suite.rules in runs:
                scenarios = load_gold_scenarios(GOLD_PATH)
                skipped = [
                    scenario.id
                    for scenario in scenarios
                    if scenario.suite != "rules" and Suite.pipeline not in runs
                ]
                reports = run_rules_suite(store, engine, scenarios)
                title = "Gold scenarios (rules suite)"
                ok = _print_scenarios(out, "rules", title, reports, skipped) and ok
            if Suite.pipeline in runs:
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
            if Suite.monitor in runs:
                curr = change_monitor.default_curr(store.con, family)
                if curr.seq <= store.snapshot_set.seq:
                    message = (
                        f"No set of the {family} family is later than {set_id}: the monitor suite"
                        " compares one (load fixtures/snapshot_t1, normalize and build-graph)."
                    )
                    if suite is Suite.monitor:
                        typer.echo(message, err=True)
                        raise typer.Exit(code=2)
                    out.echo(f"Change monitor: not run. {message}")
                else:
                    # Recorded nowhere: the log is a directory that is thrown away.
                    with tempfile.TemporaryDirectory() as scratch:
                        ontology, log = load_ontology(ONTOLOGY_PATH), AuditLog(Path(scratch))
                        run_pipeline_suite(
                            Supervisor(ontology, log),
                            load_gold_scenarios(GOLD_PATH),
                            store.set_id,
                            store.con,
                            engine.pack,
                        )
                        evaluation = change_monitor.evaluate_monitor(
                            log, Supervisor(ontology), store.con, curr, engine.pack
                        )
                    ok = _print_monitor(out, evaluation, curr.snapshot_set_id) and ok
            if suite is Suite.all:
                ok = _run_packs(out) and ok
            if Suite.er in runs:
                ok = _print_er(out, evaluate_labelled(store, er_path)) and ok
            if Suite.real_smoke in runs:
                try:
                    entries = load_smoke(smoke)
                except ValueError as exc:
                    typer.echo(str(exc), err=True)
                    raise typer.Exit(code=2) from exc
                # Recorded nowhere: the log is a directory that is thrown away.
                with tempfile.TemporaryDirectory() as scratch:
                    supervisor = Supervisor(load_ontology(ONTOLOGY_PATH), AuditLog(Path(scratch)))
                    reports = run_smoke(supervisor, entries, store, pack=engine.pack)
                ok = _print_smoke(out, reports) and ok
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
