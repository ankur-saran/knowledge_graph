"""`ubo audit`: verify the audit log's chain, and replay a recorded decision."""

import json
from collections import Counter
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.audit.replay import UnknownDecision, replay_decision
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.models.audit_event import RUN_EVENTS
from ubo_sentinel.models.ontology import OntologyConfig, load_ontology

# Exit code when this installation cannot run the replay: neither identical nor different.
EXIT_NOT_REPLAYABLE = 4

audit_app = typer.Typer(help="Verify the audit log and replay decisions.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


def _ontology() -> OntologyConfig:
    try:
        return load_ontology(ONTOLOGY_PATH)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read the ontology: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@audit_app.command()
def verify(
    role: Annotated[str, typer.Option("--role")] = "auditor",
    expect_head: Annotated[
        str | None, typer.Option("--expect-head", help="The head hash the log must end in.")
    ] = None,
    expect_count: Annotated[
        int | None, typer.Option("--expect-count", min=0, help="At least this many records.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check the hash chain and what the events say. A chain cannot show a removed tail:
    --expect-head and --expect-count, from a receipt kept elsewhere, can."""
    require_role(role, "audit-verify")
    log = AuditLog()
    report = log.verify(_ontology())
    problems = []
    if not report.intact:
        problems.append(f"line {report.broken_line}: {report.problem}")
    else:
        hashes = [record["hash"] for record in log.records()]
        # A receipt names a head the log had once; the log may have grown since.
        if expect_head is not None and expect_head not in hashes:
            problems.append(f"the log does not hold the expected head {expect_head}")
        if expect_count is not None and report.count < expect_count:
            problems.append(f"{report.count} records where at least {expect_count} are expected")
    statuses = Counter(decision.status for decision in log.decisions()) if report.intact else {}
    records = log.records() if report.intact else []
    runs = sum(record["event_type"] in RUN_EVENTS for record in records)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "intact": not problems,
                    "count": report.count,
                    "head": report.head,
                    "problems": problems,
                    "decisions": dict(sorted(statuses.items())),
                    "monitor_runs": runs,
                },
                indent=2,
            )
        )
    elif problems:
        typer.echo(f"Chain BROKEN ({report.count} records verify): {'; '.join(problems)}")
        typer.echo(f"Head: {report.head}")
    else:
        typer.echo(f"Chain intact ({report.count} records)")
        typer.echo(f"Head: {report.head}")
        if statuses:
            typer.echo(
                "Decisions: " + ", ".join(f"{n} {status}" for status, n in sorted(statuses.items()))
            )
        if runs:
            typer.echo(f"Monitor runs: {runs}")
    if problems:
        raise typer.Exit(code=1)


@audit_app.command()
def replay(
    decision_id: Annotated[str, typer.Argument(help="The decision to replay.")],
    role: Annotated[str, typer.Option("--role")] = "auditor",
    deep: Annotated[
        bool, typer.Option("--deep", help="Compute every digest from the rows (slow on real data).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Screen a recorded decision's request again and compare. Writes nothing."""
    require_role(role, "audit-replay")
    try:
        report = replay_decision(AuditLog(), _ontology(), decision_id, deep=deep)
    except UnknownDecision as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except AuditChainError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    elif report.identical:
        typer.echo(f"Replay identical: decision {decision_id}")
        typer.echo(f"Payload SHA-256: {report.recorded_payload_sha256}")
    else:
        typer.echo(f"Replay {report.verdict}: decision {decision_id}")
        typer.echo(report.detail)
        if report.differing_fields:
            # Names only: a field's values may be ones the role does not see.
            typer.echo(f"Differs in: {', '.join(report.differing_fields)}")
    if not as_json and report.evidence_source == "registry":
        typer.echo(
            "Note: this event holds no evidence digests; the database was compared with itself."
        )
    if report.verdict == "NOT_REPLAYABLE":
        raise typer.Exit(code=EXIT_NOT_REPLAYABLE)
    if not report.identical:
        raise typer.Exit(code=1)
