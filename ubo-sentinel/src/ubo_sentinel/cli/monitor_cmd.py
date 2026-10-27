"""`ubo monitor`: what newer data may change among the decisions on record, and re-screening it."""

import json
from typing import Annotated

import duckdb
import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents import change_monitor as cm
from ubo_sentinel.agents.audit import DeterminismViolation
from ubo_sentinel.agents.base import ScreenUsageError
from ubo_sentinel.agents.supervisor import Supervisor, utc_now
from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.show import RenderWithheld, audit_head, utf8_stdout
from ubo_sentinel.graph.store import GraphNotBuilt
from ubo_sentinel.models.ontology import load_ontology, require_command
from ubo_sentinel.pipeline import gold
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.differ import DiffRefused
from ubo_sentinel.present import Masker, guarded, monitor_lines
from ubo_sentinel.rules.pack import UnknownRulePack


@app.command()
def monitor(
    curr: Annotated[
        str | None,
        typer.Option(
            "--curr",
            help="The newer set; default: the newest of the family (for real data, `real`).",
        ),
    ] = None,
    prev: Annotated[
        str | None,
        typer.Option(
            "--prev", help="Only the decisions made on this set; default: every older set."
        ),
    ] = None,
    family: Annotated[
        str | None, typer.Option("--family", help="fixtures, real or sample; default: of `latest`.")
    ] = None,
    role: Annotated[str, typer.Option("--role")] = "engineer",
    actor: Annotated[
        str, typer.Option("--actor", help="Who runs the monitor and makes the re-screens.")
    ] = cm.MONITOR_ACTOR,
    autorun: Annotated[
        bool,
        typer.Option("--autorun", help="Screen the queue again; needs a role that may screen."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the queue; record nothing.")
    ] = False,
    allow_unaccepted: Annotated[
        bool,
        typer.Option(
            "--allow-unaccepted", help="Compare with a real set the quality check did not accept."
        ),
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the run as JSON.")] = False,
) -> None:
    """Diff the newer data against the sets earlier decisions were made on, and queue re-screens."""
    require_role(role, "monitor")
    utf8_stdout()
    if autorun and dry_run:
        typer.echo("Give --autorun or --dry-run, not both.", err=True)
        raise typer.Exit(code=2)

    try:
        ontology = load_ontology(ONTOLOGY_PATH)
        spec = ontology.roles[role]
        if spec.read_only and not dry_run:
            typer.echo(f"Role '{role}' is read-only: use --dry-run.", err=True)
            raise typer.Exit(code=1)
        if autorun:
            require_command(ontology, role, "screen")
        log = AuditLog()
        con = connect(read_only=True)
    except (duckdb.Error, PermissionError) as exc:
        # Before OSError, of which PermissionError is one.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read the ontology or the database: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        try:
            current = resolve_set(con, curr) if curr else cm.default_curr(con, family)
            cm.check_curr(con, current, allow_unaccepted, reason)
            if not gold.is_built(con, current.snapshot_set_id):
                typer.echo(
                    f"Set {current.snapshot_set_id} has no current graph: run"
                    f" `ubo normalize --snapshot {current.snapshot_set_id}` and"
                    f" `ubo build-graph --snapshot {current.snapshot_set_id}`.",
                    err=True,
                )
                raise typer.Exit(code=1)
            run, diffs, unknown = cm.build_queue(log, con, current, prev)
            recorded = False
            if not dry_run:
                note = (reason or "").strip() or None
                recorded = cm.record_run(
                    log, ontology, run, actor=actor, role=role, now=utc_now, note=note
                )
                cm.write_queue(run)
            results = []
            if autorun:
                results = cm.rescreen(
                    run,
                    Supervisor(ontology, log),
                    con,
                    actor=actor,
                    role=role,
                    requests=cm.recorded_requests(log),
                )
                run = cm.refresh_statuses(run, log, con)
                cm.write_queue(run)
        except (UnknownSnapshotSet, DiffRefused, cm.MonitorError, UnknownRulePack) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except ScreenUsageError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except (GraphNotBuilt, AuditChainError, PermissionError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        except DeterminismViolation as exc:
            typer.echo(f"DETERMINISM VIOLATION: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        set_ids = [run.curr_set_id, *run.baselines]
        masker = Masker(con, set_ids, role, ontology)
    finally:
        con.close()

    if as_json:
        body = {
            "run": run.model_dump(mode="json"),
            "changes": [d.model_dump(mode="json") for d in diffs],
            "recorded": recorded,
            "rescreens": [_result_json(r) for r in results],
        }
        text = json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False)
    else:
        lines = monitor_lines(run, diffs, cm.changes_by_decision(run))
        if unknown:
            lines.append(
                f"{len(unknown)} snapshot set(s) of recorded decisions are not in this database:"
                f" {', '.join(unknown)}. Their decisions cannot be compared here."
            )
        if dry_run:
            lines.append("Dry run: nothing recorded.")
        else:
            lines.append("Run recorded." if recorded else "Run already on record, unchanged.")
        lines += _result_lines(results)
        text = "\n".join(lines)
    try:
        text = guarded(text, masker, "change digest", role)
    except RenderWithheld as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(text)
    if not dry_run and not as_json:
        typer.echo(audit_head(log))
    if any(result.outcome == "FAILED" for result in results):
        raise typer.Exit(code=1)


def _result_json(result: cm.RescreenResult) -> dict:
    return {
        "prior_decision_id": result.item.prior_decision_id,
        "outcome": result.outcome,
        "decision_id": result.decision.decision_id if result.decision else None,
        "recommendation": result.decision.recommendation if result.decision else None,
        "transition": result.transition,
        "error_type": result.error_type,
    }


def _result_lines(results: list[cm.RescreenResult]) -> list[str]:
    if not results:
        return []
    lines = ["Re-screens (each waits for a reviewer):"]
    for result in results:
        item = result.item
        subject = item.target_id or item.query_norm
        prior = item.prior_outcome or item.prior_recommendation
        if result.decision is not None:
            on_record = "recorded" if result.outcome == "RECORDED" else "already on record"
            lines.append(
                f"  {subject}  {prior} -> {result.decision.recommendation} ({result.transition})"
                f"  decision {result.decision.decision_id}, {on_record}"
            )
        elif result.outcome == "SKIPPED":
            lines.append(f"  {subject}  skipped: {item.status}")
        elif result.outcome == "NEEDS_PICK":
            lines.append(f"  {subject}  several candidates now: screen it with a pick")
        else:
            lines.append(
                f"  {subject}  the screen failed ({result.error_type}): not screened;"
                " the audit log holds the error"
            )
    return lines
