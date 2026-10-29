"""`ubo annotate`: advisory LLM text beside a recorded decision (Step 13.1, 13.4).

Off unless `UBO_LLM=on` and the feature's flag are set. The match cards need no
flag: they are code, shown whatever the setting. An answer is recorded as an
`ANNOTATED` event, accepted or not; a recorded answer is never asked for twice.
"""

from enum import StrEnum
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents import llm_prose, match_adjudicator
from ubo_sentinel.agents.guardrail import check_render
from ubo_sentinel.agents.supervisor import utc_now
from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.show import audit_head, utf8_stdout
from ubo_sentinel.llm import annotation as annotations
from ubo_sentinel.llm.client import LLMUnavailable, client_for
from ubo_sentinel.llm.config import FEATURE_FLAGS, MASTER_FLAG, load_config
from ubo_sentinel.llm.egress import EgressRefused
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.ontology import OntologyConfig, load_ontology
from ubo_sentinel.present import advisory_text, card_lines


class Feature(StrEnum):
    prose = "prose"
    match = "match"


@app.command()
def annotate(
    decision_id: Annotated[str, typer.Argument(help="A recorded decision.")],
    feature: Annotated[Feature, typer.Option("--feature")] = Feature.prose,
    role: Annotated[str, typer.Option("--role")] = "reviewer",
    actor: Annotated[str | None, typer.Option("--actor", help="Default: the role.")] = None,
) -> None:
    """Ask a model for advisory text on a decision: a summary of its memo (prose), or a
    reading of its unsettled name matches (match). Never changes the decision."""
    require_role(role, "annotate")
    utf8_stdout()
    try:
        ontology = load_ontology(ONTOLOGY_PATH)
        config = load_config()
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        typer.echo(f"Cannot read the configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    log = AuditLog()
    try:
        decision = log.decision(decision_id)
    except AuditChainError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if decision is None:
        typer.echo(f"No decision {decision_id} in the audit log.", err=True)
        raise typer.Exit(code=2)

    lines: list[str] = [f"Decision {decision_id}: {decision.recommendation} ({decision.status})"]
    if feature is Feature.match:
        cards = match_adjudicator.match_cards(decision, role, ontology)
        if not cards:
            lines.append("No unsettled name match: nothing to compare.")
            _echo(lines, decision, role, ontology)
            return
        lines += card_lines(cards)
    if not config.enabled(feature.value):
        flag = FEATURE_FLAGS[feature.value]
        lines.append(f"LLM {feature.value}: off (set {MASTER_FLAG}=on and {flag}=on).")
        _echo(lines, decision, role, ontology)
        if feature is Feature.prose:
            raise typer.Exit(code=1)
        return

    try:
        client = client_for(config)
        annotate_ = (
            llm_prose.annotate_prose
            if feature is Feature.prose
            else match_adjudicator.annotate_matches
        )
        found, asked = annotate_(
            decision, ontology, client, config.model, config.max_tokens, log=log
        )
    except LLMUnavailable as exc:
        typer.echo(f"No model: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except EgressRefused as exc:
        typer.echo(f"Not sent: {exc}.", err=True)
        raise typer.Exit(code=1) from exc
    if found is None:
        lines.append("The unsettled matches do not change the outcome: no model was asked.")
        _echo(lines, decision, role, ontology)
        return
    if asked:
        recorded = annotations.record(log, ontology, found, actor or role, role, utc_now())
        lines.append(f"Annotation {found.annotation_id}: {found.status}"
                     f"{' (recorded)' if recorded else ' (already on record)'}.")  # fmt: skip
    else:
        lines.append(f"Annotation {found.annotation_id}: on record (the model was not asked).")
    lines += advisory_text(found.model_dump(mode="json"), decision, role, ontology)
    _echo(lines, decision, role, ontology)
    typer.echo(audit_head(log))
    if found.status != "ACCEPTED":
        raise typer.Exit(code=1)


def _echo(lines: list[str], decision: Decision, role: str, ontology: OntologyConfig) -> None:
    text = "\n".join(lines)
    # The render guardrail: what this role must not see is never printed.
    if check_render(text, decision, role, ontology):
        typer.echo("Withheld: the output shows a value that is masked for this role.", err=True)
        raise typer.Exit(code=1)
    typer.echo(text)
