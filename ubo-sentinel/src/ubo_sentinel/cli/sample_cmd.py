"""`ubo sample`: cut a smaller refresh directory from a full one."""

from pathlib import Path
from typing import Annotated

import typer

from ubo_sentinel.cli.app import app, require_role
from ubo_sentinel.cli.download_cmd import downloads_root
from ubo_sentinel.pipeline.sample import SampleError, sample


@app.command(name="sample")
def sample_command(
    path: Annotated[Path, typer.Option("--path", help="The refresh directory to sample.")],
    n: Annotated[int, typer.Option("--n", help="Registry records to start from.")] = 10_000,
    seed: Annotated[int, typer.Option("--seed", help="Chooses the records.")] = 0,
    list_n: Annotated[
        int | None,
        typer.Option("--list-n", help="List targets to start from (default: n / 10)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Where to write it (default: var/downloads)."),
    ] = None,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Write a sample of a refresh, closed under references, as a refresh directory."""
    require_role(role, "sample")
    try:
        result = sample(path, n, seed, out or downloads_root(), list_n=list_n)
    except SampleError as exc:
        typer.echo(f"Sample failed, nothing was written: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Sample: {result.refresh_dir.as_posix()} ({result.status})")
    for key, records in result.records.items():
        typer.echo(f"  {key:<24} {records:>10,} records")
    typer.echo(f"Ingest it with: ubo ingest --source real --path {result.refresh_dir.as_posix()}")
    typer.echo("Its sets are of the 'sample' family; 'real' never points to one.")
