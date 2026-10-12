"""`ubo review`: a reviewer approves, overrides or escalates a recommended decision."""

import sys
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

import duckdb
import typer
import yaml

from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.audit.review import pending, review_decision, unscreened
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.show import RenderWithheld, audit_head, decision_text, utf8_stdout
from ubo_sentinel.graph.views import mask_decision
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.ontology import OntologyConfig, load_ontology
from ubo_sentinel.pipeline.bronze import newest_set_of_family
from ubo_sentinel.pipeline.db import connect, db_path


class Action(StrEnum):
    approve = "approve"
    override = "override"
    escalate = "escalate"


class Outcome(StrEnum):
    clear = "CLEAR"
    escalate = "ESCALATE"


def is_superseded(decision: Decision) -> bool | None:
    """True when a newer snapshot set of the decision's family exists; None when unknown.

    A review needs no database: one that is missing, or held by a writer, is "unknown".
    """
    if not db_path().exists():
        return None
    try:
        con = connect(read_only=True)
    except duckdb.Error:
        return None
    try:
        newest = newest_set_of_family(con, decision.snapshot_set_id)
    except duckdb.Error:
        return None
    finally:
        con.close()
    return None if newest is None else newest != decision.snapshot_set_id


def _queue(log: AuditLog, role: str, ontology: OntologyConfig) -> None:
    waiting = pending(log)
    typer.echo(f"{len(waiting)} decisions wait for a review.")
    for decision in waiting:
        view = mask_decision(decision, role, ontology)
        newer = "  [newer data exists]" if is_superseded(decision) else ""
        typer.echo(
            f"  {view.decision_id}  {view.recommendation:<8}  {view.created_at:%Y-%m-%d %H:%M}"
            f"  {view.target_id or view.memo.query_norm}  {', '.join(view.reasons)}{newer}"
        )
    failed = unscreened(log)
    if failed:
        typer.echo(f"{len(failed)} screens failed and were not run again: not screened.")
        for record in failed:
            typer.echo(f"  {record['decision_id']}  {record['created_at']}  {record['note']}")


@app.command()
def review(
    role: Annotated[str, typer.Option("--role", help="Must be given: reviewer.")],
    decision_id: Annotated[
        str | None, typer.Argument(help="The decision to review; none lists those waiting.")
    ] = None,
    actor: Annotated[
        str | None, typer.Option("--actor", help="Who is reviewing. Needed to act.")
    ] = None,
    action: Annotated[Action | None, typer.Option("--action")] = None,
    outcome: Annotated[
        Outcome | None, typer.Option("--outcome", help="Needed to approve a REVIEW.")
    ] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confirm_matches: Annotated[
        bool, typer.Option("--confirm-matches", help="The unsettled matches are the listed party.")
    ] = False,
    reject_matches: Annotated[
        bool, typer.Option("--reject-matches", help="The unsettled matches are someone else.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the decision as JSON.")] = False,
) -> None:
    """Show a recommended decision and record the reviewer's decision on it."""
    require_role(role, "review")
    utf8_stdout()
    if confirm_matches and reject_matches:
        typer.echo("Give --confirm-matches or --reject-matches, not both.", err=True)
        raise typer.Exit(code=2)
    matches = "confirm" if confirm_matches else "reject" if reject_matches else None

    try:
        ontology = load_ontology(ONTOLOGY_PATH)
        log = AuditLog()
        if decision_id is None:
            _queue(log, role, ontology)
            return
        decision = log.decision(decision_id)
        if decision is None:
            typer.echo(f"No decision '{decision_id}' is on record.", err=True)
            raise typer.Exit(code=2)
        # Nothing is decided on a memo that could not be shown.
        typer.echo(decision_text(decision, role, ontology, as_json=as_json))

        if action is None and not as_json and sys.stdin.isatty():
            if decision.status != "RECOMMENDED":
                return
            letter = typer.prompt("[A]pprove / [O]verride / [E]scalate").strip().lower()[:1]
            chosen = {"a": Action.approve, "o": Action.override, "e": Action.escalate}.get(letter)
            if chosen is None:
                typer.echo("Nothing was recorded.", err=True)
                raise typer.Exit(code=2)
            action = chosen
            actor = actor or typer.prompt("Reviewer")
            if action is Action.approve and decision.recommendation == "REVIEW" and not matches:
                outcome = outcome or Outcome(typer.prompt("Outcome (CLEAR / ESCALATE)").upper())
            if reason is None and (
                action is Action.override or decision.recommendation == "REVIEW"
            ):
                reason = typer.prompt("Reason")
        if action is None:
            return
        if not actor:
            typer.echo("Give --actor: a review is made by someone.", err=True)
            raise typer.Exit(code=2)

        reviewed = review_decision(
            log,
            ontology,
            decision_id,
            action=action.value,
            actor=actor,
            role=role,
            now=lambda: datetime.now(UTC),
            outcome=outcome.value if outcome else None,
            reason=reason,
            matches=matches,
            superseded=is_superseded(decision),
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, yaml.YAMLError) as exc:
        typer.echo(f"Cannot read the ontology: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (AuditChainError, PermissionError, RenderWithheld) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"Decision {reviewed.decision_id}: {reviewed.status}, outcome {reviewed.final_outcome}"
        f"  (recommended {reviewed.recommendation}; reviewed by {reviewed.reviewed_by})"
    )
    typer.echo(audit_head(log))
