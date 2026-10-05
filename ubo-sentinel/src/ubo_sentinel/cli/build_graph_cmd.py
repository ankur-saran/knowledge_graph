"""`ubo build-graph`: build the Gold graph for one snapshot set."""

from typing import Annotated

import typer

from ubo_sentinel.cli.app import app, require_role
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.gold import GoldError, GoldSummary, build_gold


def _print_summary(summary: GoldSummary) -> None:
    typer.echo(f"Snapshot set: {summary.snapshot_set_id} ({summary.status})")
    for table, rows in summary.counts.items():
        typer.echo(f"  {table:<28} {rows:>6} rows")
    typer.echo(f"Digest: {summary.gold_digest}")


@app.command()
def build_graph(
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, latest or a set id.")
    ] = bronze.ALIAS_LATEST,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Build the Gold graph for one snapshot set from its Silver tables."""
    require_role(role, "build-graph")
    con = connect()
    try:
        bronze.create_schema(con)
        snapshot_set = resolve_set(con, snapshot)
        summary = build_gold(con, snapshot_set)
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo ingest' first.", err=True)
        raise typer.Exit(code=2) from exc
    except GoldError as exc:
        typer.echo(f"Build failed, nothing was written: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        con.close()
    _print_summary(summary)
