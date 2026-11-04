"""`ubo investigate`: look further around a recorded decision (Step 14).

A first pass in code always; a model only with `UBO_LLM=on` and
`UBO_LLM_INVESTIGATE=on`, and never with `--no-llm`. The investigation is
recorded once, as an `INVESTIGATED` event, and shown as the role may see it.
It never changes the decision.
"""

from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents import investigator
from ubo_sentinel.agents.supervisor import utc_now
from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.show import audit_head, utf8_stdout
from ubo_sentinel.graph.store import GraphNotBuilt
from ubo_sentinel.llm.client import LLMUnavailable, client_for
from ubo_sentinel.llm.config import FEATURE_FLAGS, MASTER_FLAG, load_config
from ubo_sentinel.models.ontology import load_ontology
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.present import RenderWithheld, investigation_text

# A model that answered nothing, or a run that stopped: the record is made, but the
# question was not answered.
_NOT_ANSWERED = (investigator.UNAVAILABLE, investigator.INTERRUPTED)


@app.command()
def investigate(
    decision_id: Annotated[str, typer.Argument(help="A recorded decision.")],
    question: Annotated[str, typer.Argument(help="What to look for, in plain words.")],
    role: Annotated[str, typer.Option("--role")] = "analyst",
    actor: Annotated[str | None, typer.Option("--actor", help="Default: the role.")] = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="The first pass only: no model, no key.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the investigation as JSON.")
    ] = False,
) -> None:
    """Look beyond the graph a decision's screen walked: shared addresses, common
    controllers, siblings, paths, history. Advisory; never changes the decision."""
    require_role(role, "investigate")
    utf8_stdout()
    if not question.strip():
        typer.echo("Give a question.", err=True)
        raise typer.Exit(code=2)
    try:
        ontology = load_ontology(ONTOLOGY_PATH)
        config = load_config()
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        typer.echo(f"Cannot read the configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    log = AuditLog()
    client = budget = None
    note = None
    if no_llm:
        note = "First pass only (--no-llm)."
    elif not config.enabled("investigate"):
        flag = FEATURE_FLAGS["investigate"]
        note = f"First pass only: the model is off (set {MASTER_FLAG}=on and {flag}=on)."
    else:
        try:
            client, budget = client_for(config), config.investigate
        except LLMUnavailable as exc:
            typer.echo(f"No model: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    try:
        decision = log.recommended(decision_id)
        if decision is None:
            typer.echo(f"No decision {decision_id} in the audit log.", err=True)
            raise typer.Exit(code=2)
        found, recorded = investigator.run_investigation(
            log, ontology, decision_id, question, actor or role, role, utc_now(),
            client=client, budget=budget,
        )  # fmt: skip
        text = (
            found.model_dump_json(indent=2)
            if as_json
            else investigation_text(found, decision, role, ontology)
        )
    except (UnknownSnapshotSet, GraphNotBuilt, AuditChainError, PermissionError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except RenderWithheld as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if note and not as_json:
        typer.echo(note)
    typer.echo(text)
    if not as_json:
        typer.echo(
            f"Recorded as {found.event_id}." if recorded else "Already on record: the same run."
        )
        typer.echo(audit_head(log))
    if any(fault.startswith(_NOT_ANSWERED) for fault in found.faults):
        raise typer.Exit(code=1)
