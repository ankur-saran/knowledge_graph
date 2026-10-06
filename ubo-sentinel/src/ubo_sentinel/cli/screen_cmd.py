"""`ubo screen`: screen one entity and print the recommendation and its memo."""

import sys
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents.audit import DeterminismViolation
from ubo_sentinel.agents.base import Resolution, ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.explainer import render_memo
from ubo_sentinel.agents.guardrail import check_render
from ubo_sentinel.agents.supervisor import (
    NeedsDisambiguation,
    Recommended,
    ScreenFailed,
    Supervisor,
)
from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.graph.store import GraphNotBuilt
from ubo_sentinel.graph.views import mask_candidates, mask_decision
from ubo_sentinel.models.memo import DecisionView
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


def _summary(view: DecisionView, outcome: Recommended) -> str:
    recorded = "recorded" if outcome.is_new else "already on record, unchanged"
    lines = [
        f"Decision {view.decision_id}: {view.recommendation}  (status {view.status}; {recorded})",
        f"Reasons: {', '.join(view.reasons) or 'none'}",
        f"Resolved by: {outcome.resolution.method}",
        f"Snapshot set: {view.snapshot_set_id}   Rule pack: {view.rule_pack_id}"
        f"   Depth: {view.max_depth}",
        f"Payload SHA-256: {view.payload_sha256}",
    ]
    lines += [f"GUARDRAIL WARNING: {warning}" for warning in view.guardrail_warnings]
    return "\n".join(lines)


@app.command()
def screen(
    query: Annotated[str, typer.Argument(help="An entity name or an LEI.")] = "",
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, latest or a set id.")
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
    as_json: Annotated[bool, typer.Option("--json", help="Print the decision as JSON.")] = False,
) -> None:
    """Screen an entity: resolve it, walk its owners, apply the rule pack, write the memo."""
    require_role(role, "screen")
    if hasattr(sys.stdout, "reconfigure"):
        # A non-Latin name must not fail a piped or redirected run on Windows.
        sys.stdout.reconfigure(encoding="utf-8")
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
        )
        supervisor = Supervisor(ontology, AuditLog())
        outcome = supervisor.run(request)
        if isinstance(outcome, NeedsDisambiguation):
            typer.echo(f"'{query}' has {len(outcome.candidates)} candidates:")
            for line in _candidate_lines(outcome.resolution, role, ontology):
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
        typer.echo(
            f"{outcome.recommendation} ({outcome.reason}): the screen failed in the"
            f" {outcome.agent} agent: {outcome.error}\n"
            "No decision was recorded. Treat the entity as not screened.",
            err=True,
        )
        raise typer.Exit(code=1)
    if isinstance(outcome, NeedsDisambiguation):
        typer.echo("Run again with --pick N.", err=True)
        raise typer.Exit(code=EXIT_NEEDS_PICK)

    view = mask_decision(outcome.decision, role, ontology)
    if as_json:
        text = view.model_dump_json(indent=2)
    else:
        text = f"{_summary(view, outcome)}\n\n{render_memo(view.memo)}"
    if check_render(text, outcome.decision, role, ontology):
        # Fail closed: nothing of the memo is shown when the render guardrail trips.
        typer.echo(
            f"Decision {view.decision_id} is recorded, but its memo is not shown: the output"
            f" held a value that is masked for role '{role}'.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(text)
