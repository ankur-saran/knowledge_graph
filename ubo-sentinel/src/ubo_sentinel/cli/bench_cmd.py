"""`ubo bench`: screen a list of LEIs and report how long the screens take."""

import json
import tempfile
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents.bench import (
    DEFAULT_BUDGET_SECONDS,
    BenchInputError,
    read_leis,
    run_bench,
)
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
from ubo_sentinel.models.ontology import load_ontology
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.rules.pack import DEFAULT_PACK, UnknownRulePack, load_rule_pack, pack_path


def _seconds(stats: dict[str, float]) -> str:
    return f"p50 {stats['p50']:.3f}s  p95 {stats['p95']:.3f}s  max {stats['max']:.3f}s"


@app.command()
def bench(
    lei_file: Annotated[
        Path,
        typer.Option("--lei-file", help="LEIs to screen: one a line, or a YAML list of entries."),
    ],
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, real, latest or a set id.")
    ] = bronze.ALIAS_REAL,
    rounds: Annotated[int, typer.Option("--rounds", min=1, help="Times each LEI is screened.")] = 1,
    budget: Annotated[
        float, typer.Option("--budget", help="p95 seconds above which the bench fails.")
    ] = DEFAULT_BUDGET_SECONDS,
    rule_pack: Annotated[
        str, typer.Option("--rule-pack", help="Rule pack name: a file in rules/.")
    ] = DEFAULT_PACK,
    record: Annotated[
        bool,
        typer.Option(
            "--record", help="Record the screens in the audit log (default: a scratch log)."
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the summary as JSON.")] = False,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Screen each LEI by target id and report p50/p95/max, cold and warm, by agent."""
    require_role(role, "bench")
    try:
        leis = read_leis(lei_file)
        pack = load_rule_pack(pack_path(rule_pack))
    except (BenchInputError, UnknownRulePack) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read rule pack '{rule_pack}': {exc}", err=True)
        raise typer.Exit(code=2) from exc

    ontology = load_ontology(ONTOLOGY_PATH)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            log = AuditLog() if record else AuditLog(Path(scratch))
            with GraphStore.open(snapshot) as store:
                set_id = store.set_id
            con = connect(read_only=True)
            try:
                report = run_bench(
                    Supervisor(ontology, log),
                    leis,
                    set_id,
                    con,
                    set_id,
                    rounds=rounds,
                    budget=budget,
                    pack=pack,
                    rule_pack=rule_pack,
                )
            finally:
                con.close()
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo refresh' first.", err=True)
        raise typer.Exit(code=2) from exc
    except GraphNotBuilt as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    summary = report.summary()
    if as_json:
        summary["errors_detail"] = [
            {"lei": s.lei, "outcome": s.outcome, "detail": s.detail} for s in report.errors
        ]
        typer.echo(json.dumps(summary, indent=2))
    else:
        typer.echo(f"Snapshot set: {set_id}   {len(leis)} LEIs x {rounds} round(s)")
        for s in report.screens:
            typer.echo(
                f"  {s.lei}  round {s.round}  {s.outcome:<12} {s.seconds:>8.3f}s"
                f"  {s.rows:>7} rows  {s.paths:>3} paths  {s.detail}".rstrip()
            )
        typer.echo(f"All screens   {_seconds(summary['seconds'])}")
        typer.echo(f"Cold (first)  {summary['cold_seconds']:.3f}s")
        if len(report.screens) > 1:
            typer.echo(f"Warm          {_seconds(summary['warm_seconds'])}")
        typer.echo("By agent")
        for name, stats in summary["agents"].items():
            typer.echo(f"  {name:<16} {_seconds(stats)}")
        rows = summary["rows_read"]
        typer.echo(f"Rows read     mean {rows['mean']}  max {rows['max']}")
        if summary["peak_rss_mb"] is not None:
            typer.echo(f"Peak memory   {summary['peak_rss_mb']} MB")
        verdict = "within" if report.within_budget else "ABOVE"
        typer.echo(f"p95 {report.p95:.3f}s: {verdict} the {budget:.0f}s budget")
        if report.errors:
            typer.echo(f"{len(report.errors)} screen(s) did not end in a recommendation.")
    if report.errors or not report.within_budget:
        raise typer.Exit(code=1)
