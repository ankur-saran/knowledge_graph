"""`ubo export`: copy a built graph to another store, as the role may see it."""

import os
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.audit.log import var_dir
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.graph.export import read_graph
from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
from ubo_sentinel.models.ontology import load_ontology
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.pipeline.exporters import neo4j_export

export_app = typer.Typer(help="Export a built graph to another store.", no_args_is_help=True)
app.add_typer(export_app, name="export")


@export_app.command(name="neo4j")
def neo4j(
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, real, latest or a set id.")
    ] = bronze.ALIAS_LATEST,
    role: Annotated[
        str, typer.Option("--role", help="An analyst's export masks persons.")
    ] = "analyst",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Directory for the files (default <UBO_VAR>/export/<set id>)."),
    ] = None,
    uri: Annotated[
        str | None,
        typer.Option("--uri", help="Load into this database instead (bolt://host:7687)."),
    ] = None,
    user: Annotated[str, typer.Option("--user")] = "neo4j",
    password_env: Annotated[
        str,
        typer.Option("--password-env", help="The environment variable holding the password."),
    ] = "NEO4J_PASSWORD",
    replace: Annotated[
        bool, typer.Option("--replace", help="Delete what the database holds of this set first.")
    ] = False,
) -> None:
    """Export one snapshot set's graph for Neo4j: files for LOAD CSV, or a live load.

    One-way: nothing reads the export back into screening."""
    require_role(role, "export-neo4j")
    try:
        ontology = load_ontology(ONTOLOGY_PATH)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read the ontology: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if uri is None and replace:
        typer.echo("--replace needs --uri.", err=True)
        raise typer.Exit(code=2)
    password = None
    if uri is not None:
        password = os.environ.get(password_env)
        if not password:
            typer.echo(f"Set the database password in ${password_env}.", err=True)
            raise typer.Exit(code=2)
    try:
        with GraphStore.open(snapshot) as store:
            rows = read_graph(store, role, ontology)
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo ingest' first.", err=True)
        raise typer.Exit(code=2) from exc
    except GraphNotBuilt as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except PermissionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    counts = ", ".join(f"{count} {name}" for name, count in rows.counts().items())
    masked = " (persons masked)" if rows.masked else ""
    if uri is None:
        target = out or var_dir() / "export" / rows.snapshot_set_id
        manifest = neo4j_export.write(rows, target)
        typer.echo(f"Exported snapshot set {rows.snapshot_set_id}{masked}: {counts}.")
        typer.echo(f"Files: {target.as_posix()} (load with: cypher-shell -f import.cypher)")
        typer.echo(f"Export digest: {manifest['export_digest']}")
        return
    try:
        neo4j_export.push(rows, uri, (user, password), replace=replace)
    except ImportError as exc:
        typer.echo("The Neo4j driver is not installed: uv sync --extra optional.", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # the driver's errors: unreachable, refused, auth
        typer.echo(f"Neo4j refused the export: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Loaded snapshot set {rows.snapshot_set_id}{masked} into {uri}: {counts}.")
