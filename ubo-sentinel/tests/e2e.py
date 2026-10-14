"""Helpers for the integration tests: the commands, in this process or in another."""

import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import NeedsDisambiguation
from ubo_sentinel.cli.app import app

# The repository root: commands are run from it.
REPO = Path(__file__).resolve().parent.parent

runner = CliRunner()

# The `ubo` entry point, for a child process.
_CHILD = "import sys; from ubo_sentinel.cli.app import app; sys.argv[0] = 'ubo'; app()"


def cli(*args):
    """Run a command in this process, as `uv run ubo ...` would."""
    return runner.invoke(app, [str(arg) for arg in args])


def decision_id_of(output):
    """The id on the first line a command prints for a decision."""
    return output.split()[1].rstrip(":")


def head_of(output):
    """The hash of the `Audit head:` receipt a command printed."""
    line = next(line for line in output.splitlines() if line.startswith("Audit head: "))
    return line.split()[2]


def child_env(**extra):
    """This process's environment (which names the test's database and log), plus `extra`."""
    env = dict(os.environ) | {"PYTHONIOENCODING": "utf-8"}
    return env | {name: str(value) for name, value in extra.items()}


def start_cli(*args, **env):
    """Start a command in another process. The caller waits for it."""
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, *map(str, args)],
        cwd=REPO,
        env=child_env(**env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
    )


def cli_process(*args, **env):
    """Run a command in another process and wait. Returns (exit code, output)."""
    child = start_cli(*args, **env)
    output, _ = child.communicate(timeout=120)
    return child.returncode, output


def run_python(code, **env):
    """Run Python code in another process from the repository root. Returns its output."""
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=child_env(**env),
        capture_output=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def person_pii(entities):
    """Everything the fixtures say that identifies a person: names, aliases, ids, numbers."""
    values = set()
    for row in entities:
        if row.entity_type != "Person":
            continue
        values.update([row.legal_name, *row.aliases, row.source_record_id, row.canonical_id()])
        if row.registration_number:
            values.add(row.registration_number)
    return sorted(value for value in values if value)


def screen_gold(supervisor, scenario, snapshot, con=None, **request):
    """Screen a gold scenario's query as an analyst would, picking its target when asked."""
    first = ScreenRequest(query=scenario.query, snapshot=snapshot, **request)
    outcome = supervisor.run(first, con)
    if isinstance(outcome, NeedsDisambiguation):
        ids = [match.entity_id for match in outcome.candidates]
        pick = ids.index(scenario.target_id) + 1
        outcome = supervisor.run(first.model_copy(update={"pick": pick}), con)
    return outcome
