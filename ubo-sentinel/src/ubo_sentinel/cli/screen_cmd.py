"""`ubo screen`: screen one entity and print the recommendation and its memo."""

import sys
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents import intake_assist
from ubo_sentinel.agents.audit import DeterminismViolation
from ubo_sentinel.agents.base import Resolution, ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.supervisor import (
    NeedsDisambiguation,
    ScreenFailed,
    Supervisor,
    utc_now,
)
from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.show import RenderWithheld, audit_head, decision_text, utf8_stdout
from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
from ubo_sentinel.graph.views import mask_candidates
from ubo_sentinel.llm.client import LLMUnavailable, client_for
from ubo_sentinel.llm.config import load_config
from ubo_sentinel.models.graph import EntityMatch
from ubo_sentinel.models.ontology import OntologyConfig, load_ontology
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.rules.pack import DEFAULT_PACK, UnknownRulePack

# Exit code when the query has several candidates and no pick was given.
EXIT_NEEDS_PICK = 3


def _candidate_lines(resolution: Resolution, role: str, ontology: OntologyConfig) -> list[str]:
    lines = []
    for number, match in enumerate(mask_candidates(resolution.candidates, role, ontology), 1):
        lei = f"  LEI {match.lei}" if match.lei else ""
        lines.append(
            f"  {number}. {match.legal_name}  [{match.jurisdiction or '--'}]  {match.entity_type}"
            f"  {match.status}  {match.match_type} {match.confidence:.2f}{lei}  ({match.entity_id})"
        )
    return lines


def _assist_lines(
    request: ScreenRequest,
    known: list[EntityMatch],
    log: AuditLog,
    role: str,
    ontology: OntologyConfig,
) -> list[str]:
    """Search variants from a model (`UBO_LLM_INTAKE`), resolved; [] when off or not asked."""
    try:
        config = load_config()
    except ValueError as exc:
        return [f"Intake assist: not run ({exc})"]
    identifiers = bool(request.identifiers())
    if not config.enabled("intake") or not intake_assist.may_ask(known, ontology, identifiers):
        return []
    try:
        client = client_for(config)
        with GraphStore.open(request.snapshot) as store:
            run = intake_assist.assist(
                store, request.query, known, client, config.model, config.max_tokens,
                jurisdiction=request.jurisdiction,
            )  # fmt: skip
    except LLMUnavailable as exc:
        return [f"Intake assist: no model ({exc})."]
    intake_assist.record(log, ontology, run, request.actor, role, utc_now())
    if run.status != "ACCEPTED":
        return [f"Intake assist: {run.status} ({'; '.join(run.faults) or '-'}); nothing shown."]
    if not run.candidates:
        return ["Intake assist (LLM-assisted): its search variants found no other candidate."]
    lines = [
        "Intake assist (LLM-assisted, advisory): search variants found other candidates.",
        f"These are never picked for you: screen one with --target-id ID --assist-run {run.run_id}",
    ]
    shown = mask_candidates([c.match for c in run.candidates], role, ontology)
    for match in shown:
        lines.append(
            f"  - {match.legal_name}  [{match.jurisdiction or '--'}]  {match.entity_type}"
            f"  {match.match_type} {match.confidence:.2f}  ({match.entity_id})"
        )
    return lines


def _check_assist_run(log: AuditLog, run_id: str, target_id: str | None) -> None:
    """A target screened from an assist run must be one that run found."""
    event_id = f"intake:{run_id}"
    event = next((r for r in log.records() if r["decision_id"] == event_id), None)
    if event is None:
        raise ScreenUsageError(f"No intake assist run {run_id} is on record.")
    found = {c["match"]["entity_id"] for c in event["payload"]["candidates"]}
    if target_id not in found:
        raise ScreenUsageError(f"Run {run_id} did not find {target_id}; give one of its ids.")


@app.command()
def screen(
    query: Annotated[str, typer.Argument(help="An entity name or an LEI.")] = "",
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, real, latest or a set id.")
    ] = bronze.ALIAS_FIXTURES,
    role: Annotated[str, typer.Option("--role", help="analyst or reviewer.")] = "analyst",
    rule_pack: Annotated[
        str, typer.Option("--rule-pack", help="Rule pack name: a file in rules/.")
    ] = DEFAULT_PACK,
    max_depth: Annotated[
        int | None, typer.Option("--max-depth", min=1, help="Hops up; default: the rule pack's.")
    ] = None,
    lei: Annotated[str | None, typer.Option("--lei")] = None,
    registration_number: Annotated[str | None, typer.Option("--registration-number")] = None,
    jurisdiction: Annotated[
        str | None, typer.Option("--jurisdiction", help="Ranks candidates; never filters.")
    ] = None,
    pick: Annotated[
        int | None, typer.Option("--pick", min=1, help="Choose candidate N of an ambiguous query.")
    ] = None,
    target_id: Annotated[
        str | None, typer.Option("--target-id", help="Screen this entity id; no resolution.")
    ] = None,
    actor: Annotated[
        str | None, typer.Option("--actor", help="Who is screening; default: the role.")
    ] = None,
    assist_run: Annotated[
        str | None,
        typer.Option("--assist-run", help="The intake assist run --target-id came from."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the decision as JSON.")] = False,
) -> None:
    """Screen an entity: resolve it, walk its owners, apply the rule pack, write the memo."""
    require_role(role, "screen")
    utf8_stdout()
    if not query and not (lei or registration_number or target_id):
        typer.echo("Give a name or an LEI to screen, or --target-id.", err=True)
        raise typer.Exit(code=2)

    try:
        ontology = load_ontology(ONTOLOGY_PATH)
        request = ScreenRequest(
            query=query,
            lei=lei,
            registration_number=registration_number,
            jurisdiction=jurisdiction,
            target_id=target_id,
            pick=pick,
            snapshot=snapshot,
            rule_pack=rule_pack,
            max_depth=max_depth,
            role=role,
            actor=actor or role,
            assist_run_id=assist_run,
        )
        log = AuditLog()
        if assist_run is not None:
            if target_id is None:
                raise ScreenUsageError("--assist-run needs --target-id.")
            _check_assist_run(log, assist_run, target_id)
        supervisor = Supervisor(ontology, log)
        outcome = supervisor.run(request)
        if isinstance(outcome, NeedsDisambiguation):
            typer.echo(f"'{query}' has {len(outcome.candidates)} candidates:")
            for line in _candidate_lines(outcome.resolution, role, ontology):
                typer.echo(line)
            for line in _assist_lines(request, outcome.candidates, log, role, ontology):
                typer.echo(line)
            if as_json or not sys.stdin.isatty():
                typer.echo("Run again with --pick N.", err=True)
                raise typer.Exit(code=EXIT_NEEDS_PICK)
            chosen = typer.prompt("Screen which one", type=int)
            outcome = supervisor.run(request.model_copy(update={"pick": chosen}))
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo ingest' first.", err=True)
        raise typer.Exit(code=2) from exc
    except (UnknownRulePack, ScreenUsageError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read the rule pack or the ontology: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (GraphNotBuilt, AuditChainError, PermissionError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except DeterminismViolation as exc:
        typer.echo(f"DETERMINISM VIOLATION: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if isinstance(outcome, ScreenFailed):
        # The exception's text may quote what it failed on, and no view masks
        # it: the role is shown its kind, and the log holds the rest.
        typer.echo(
            f"{outcome.recommendation} ({outcome.reason}): the screen failed in the"
            f" {outcome.agent} agent ({outcome.error_type}).\n"
            "No decision was recorded. Treat the entity as not screened."
            " The audit log holds the error.",
            err=True,
        )
        raise typer.Exit(code=1)
    if isinstance(outcome, NeedsDisambiguation):
        typer.echo("Run again with --pick N.", err=True)
        raise typer.Exit(code=EXIT_NEEDS_PICK)

    try:
        text = decision_text(
            outcome.decision,
            role,
            ontology,
            as_json=as_json,
            recorded="recorded" if outcome.is_new else "already on record, unchanged",
            resolved_by=outcome.resolution.method,
        )
    except RenderWithheld as exc:
        # Fail closed: nothing of the memo is shown when the render guardrail trips.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(text)
    if not as_json:
        if outcome.resolution.method == "NOT_FOUND" and query and target_id is None:
            for line in _assist_lines(request, [], log, role, ontology):
                typer.echo(line)
        # A receipt: `ubo audit verify --expect-head` later shows a log cut short.
        typer.echo(audit_head(log))
