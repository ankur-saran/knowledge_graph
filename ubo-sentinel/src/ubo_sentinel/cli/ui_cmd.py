"""`ubo ui`: start the review UI on this machine."""

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.models.ontology import ONTOLOGY_ENV_VAR

APP_PATH = Path(__file__).resolve().parent.parent / "ui" / "app.py"

# Local-first: served to this machine only, and no usage statistics sent anywhere.
# `.streamlit/config.toml` says the same for a plain `streamlit run`.
SERVER_FLAGS = [
    "--server.address",
    "localhost",
    "--server.headless",
    "true",
    "--browser.gatherUsageStats",
    "false",
]


def streamlit_command(role: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_PATH),
        "--server.port",
        str(port),
        *SERVER_FLAGS,
        "--",
        "--role",
        role,
    ]


@app.command()
def ui(
    role: Annotated[
        str, typer.Option("--role", help="The role the UI opens with; it can be changed there.")
    ] = "analyst",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8501,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the command that starts the UI, and stop.")
    ] = False,
) -> None:
    """Start the review UI (Streamlit) on localhost."""
    require_role(role, "ui")
    command = streamlit_command(role, port)
    if dry_run:
        typer.echo(shlex.join(command))
        return
    # The UI reads the ontology, the database and the log as the commands do:
    # relative to this directory, so it is run from here, with the ontology named.
    env = dict(os.environ) | {ONTOLOGY_ENV_VAR: str(ONTOLOGY_PATH.resolve())}
    try:
        raise typer.Exit(code=subprocess.call(command, cwd=Path.cwd(), env=env))
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
