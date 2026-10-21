"""`ubo refresh`: download, ingest, normalize, build the graph, then check the data.

The data-quality report ends the run. The set becomes `real` only when the
check accepts it: when its entities or active designations fell by more than
the shrink limit from the last accepted set, `real` stays where it was and
the command exits 1, unless the fall is accepted with a reason.
"""

from pathlib import Path
from typing import Annotated, Any

import typer

from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.download_cmd import run_download
from ubo_sentinel.models.ontology import load_ontology
from ubo_sentinel.pipeline.bronze import ALIAS_LATEST, ALIAS_REAL, IngestError
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.gold import GoldError, build_gold
from ubo_sentinel.pipeline.loaders.base import resolve_alias
from ubo_sentinel.pipeline.loaders.real_loader import ingest_all
from ubo_sentinel.pipeline.quality import QualityCheck, QualityError, check, shrink_limit
from ubo_sentinel.pipeline.silver import SilverError
from ubo_sentinel.pipeline.transformer import build_silver


def _count(value: Any) -> str:
    return "-" if value is None else f"{value:,}"


def _change(c: dict[str, Any]) -> str:
    if c["change"] is None:
        return ""
    return f"  (was {_count(c['previous'])}, {c['change']:+.1%})"


def report_lines(result: QualityCheck) -> list[str]:
    """The data-quality report as `ubo refresh` prints it."""
    report = result.report
    changes = report.get("changes", {})
    lines = [f"Data quality: snapshot set {report['snapshot_set_id']} ({report['family']})"]
    lines.append(
        f"  compared with {result.compared_with}"
        if result.compared_with
        else "  first checked set of its family: nothing to compare with"
    )
    lines.append("  datasets")
    for name, d in report["datasets"].items():
        rejects = sum(d["rejects"].values())
        note = f"  {rejects:,} rejected {d['rejects']}" if rejects else ""
        lines.append(
            f"    {name:<18} {_count(d['rows']):>12} rows{note}"
            f"{_change(changes.get(f'rows.{name}', {'change': None}))}"
        )
    for name in (
        "entities",
        "relationships",
        "designations",
        "active_designations",
        "links",
        "link_blocks_skipped",
        "targets_without_designation",
    ):
        lines.append(
            f"  {name:<28} {_count(report[name]):>12}{_change(changes.get(name, {'change': None}))}"
        )
    for title, key in (
        ("links by kind", "links_by_match_type"),
        ("name-link scores", "name_link_scores"),
        ("list companies joined to the registry", "list_companies_joined"),
        ("silver rejects", "rejects"),
    ):
        if report[key]:
            lines.append(f"  {title}: " + ", ".join(f"{k} {v:,}" for k, v in report[key].items()))
    for dataset, reasons in report["filtered"].items():
        lines.append(
            f"  filtered {dataset}: " + ", ".join(f"{k} {v:,}" for k, v in reasons.items())
        )
    unreadable = {k: v for k, v in report["unreadable"].items() if v}
    if unreadable:
        lines.append(
            "  unreadable (kept empty): " + ", ".join(f"{k} {v:,}" for k, v in unreadable.items())
        )
    if result.shrunk:
        fell = ", ".join(result.shrunk)
        limit = f"{shrink_limit():.0%}"
        if result.accepted:
            lines.append(f"  {fell} fell by more than {limit}; accepted: {result.reason}")
        else:
            lines.append(
                f"  REFUSED: {fell} fell by more than {limit} since the last accepted set."
                " A truncated feed looks like mass delistings."
            )
    return lines


@app.command()
def refresh(
    path: Annotated[
        Path | None,
        typer.Option(
            "--path", help="Refresh an existing refresh directory instead of downloading."
        ),
    ] = None,
    accept_shrink: Annotated[
        bool,
        typer.Option(
            "--accept-shrink",
            help="Accept files or counts that fell by more than the shrink limit (needs --reason).",
        ),
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="Why a fall is accepted.")] = None,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Download, ingest, normalize and build the graph, then report on the data."""
    require_role(role, "refresh")
    if accept_shrink and not (reason and reason.strip()):
        typer.echo("--accept-shrink needs --reason: say why the fall is real.", err=True)
        raise typer.Exit(code=2)
    refresh_dir = path if path is not None else run_download(accept_shrink).refresh_dir

    ontology = load_ontology(ONTOLOGY_PATH)
    con = connect()
    try:
        ingested = ingest_all(con, refresh_dir)
        snapshot_set = ingested.snapshot_set
        state = "new" if snapshot_set.is_new else "already present"
        typer.echo(f"Ingested: snapshot set {snapshot_set.snapshot_set_id} ({state})")
        silver = build_silver(con, snapshot_set, ontology)
        typer.echo(f"Silver: {silver.status}, digest {silver.silver_digest}")
        gold = build_gold(con, snapshot_set)
        typer.echo(f"Gold: {gold.status}, digest {gold.gold_digest}")
        result = check(con, snapshot_set, accept_shrink=accept_shrink, reason=reason)
        aliases = {alias: resolve_alias(con, alias) for alias in (ALIAS_LATEST, ALIAS_REAL)}
    except IngestError as exc:
        typer.echo(f"Ingest failed, nothing was written: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except (SilverError, GoldError, QualityError) as exc:
        typer.echo(f"Refresh failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        con.close()

    for line in report_lines(result):
        typer.echo(line)
    for alias, set_id in aliases.items():
        typer.echo(f"  {alias:<8} -> {set_id or 'not set'}")
    if not result.accepted:
        typer.echo(
            f"Set {snapshot_set.snapshot_set_id} is built but not accepted; 'real' is unchanged."
            " If the fall is real, accept it: ubo refresh"
            f" --path {refresh_dir.as_posix()} --accept-shrink --reason '...'",
            err=True,
        )
        raise typer.Exit(code=1)
