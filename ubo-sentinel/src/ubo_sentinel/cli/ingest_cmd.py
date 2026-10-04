"""`ubo ingest`: load a source's files into Bronze."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ubo_sentinel.cli.app import app, require_role
from ubo_sentinel.pipeline.bronze import IngestError
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.loaders import LOADERS, IngestSummary


class Source(StrEnum):
    FIXTURES = "fixtures"
    GLEIF = "gleif"
    OPENSANCTIONS = "opensanctions"


def _print_summary(summary: IngestSummary) -> None:
    typer.echo(f"Source: {summary.source} ({summary.path.as_posix()})")
    for dataset in summary.datasets:
        typer.echo(
            f"  {dataset.table:<21} {dataset.snapshot_id}  {dataset.row_count:>6} rows"
            f"  {dataset.status}"
        )
    snapshot_set = summary.snapshot_set
    state = "new" if snapshot_set.is_new else "already present"
    typer.echo(f"Snapshot set: {snapshot_set.snapshot_set_id} ({state})")
    for alias, set_id in summary.aliases.items():
        typer.echo(f"  {alias:<8} -> {set_id or 'not set'}")


@app.command()
def ingest(
    source: Annotated[Source, typer.Option("--source", help="Which source to load.")],
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Directory to load (fixtures: fixtures/snapshot_t0)."),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", help="Snapshot set that supplies the datasets --path lacks."),
    ] = None,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Load a source's files into Bronze as immutable snapshots."""
    require_role(role, "ingest")
    loader = LOADERS.get(source.value)
    if loader is None:
        typer.echo(f"Source '{source.value}' arrives in Step 10.", err=True)
        raise typer.Exit(code=2)

    con = connect()
    try:
        summary = loader(con, path, base)
    except IngestError as exc:
        typer.echo(f"Ingest failed, nothing was written: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        con.close()
    _print_summary(summary)
