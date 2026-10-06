"""Typer CLI entry point.

Each build step registers its own command on `app` in the step that first needs it.
"""

from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel import __version__
from ubo_sentinel.models.ontology import load_ontology

# Commands are run from the repository root.
ONTOLOGY_PATH = Path("ontology/ontology.yaml")

app = typer.Typer(
    name="ubo",
    help="Beneficial ownership and sanctions exposure screening.",
    no_args_is_help=True,
    add_completion=False,
)


def load_permissions(ontology_path: Path = ONTOLOGY_PATH) -> dict[str, set[str]]:
    """Return the role -> permitted commands matrix from the ontology."""
    ontology = load_ontology(ontology_path)
    return {role: set(spec.commands) for role, spec in ontology.roles.items()}


def require_role(role: str, command: str, ontology_path: Path = ONTOLOGY_PATH) -> None:
    """Exit with an error unless `role` may run `command`. Fails closed."""
    try:
        permissions = load_permissions(ontology_path)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read permission matrix from {ontology_path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if role not in permissions:
        known = ", ".join(sorted(permissions)) or "none defined"
        typer.echo(f"Unknown role '{role}' (known roles: {known}).", err=True)
        raise typer.Exit(code=2)
    if command not in permissions[role]:
        typer.echo(f"Role '{role}' is not permitted to run '{command}'.", err=True)
        raise typer.Exit(code=1)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"ubo-sentinel {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the engine version and exit.",
        ),
    ] = False,
) -> None:
    """Beneficial ownership and sanctions exposure screening."""


# Command modules register themselves on `app` when imported.
from ubo_sentinel.cli import (  # noqa: E402, F401
    build_graph_cmd,
    eval_cmd,
    ingest_cmd,
    normalize_cmd,
    screen_cmd,
)
