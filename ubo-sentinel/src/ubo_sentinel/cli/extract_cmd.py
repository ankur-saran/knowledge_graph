"""`ubo extract` and `ubo extract-review`: holdings read from a document (Step 15).

`extract` keeps the file, parses it (no key), and asks a model only with
`UBO_LLM=on`, `UBO_LLM_EXTRACT=on` and `--allow-egress` for this document. The run
is recorded once, as an `EXTRACTED` event. `extract-review` lists the proposals
waiting for a ruling, or records one; a reviewer may not rule on what they
extracted. Nothing reaches the graph until `ubo ingest --source documents`.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError

from ubo_sentinel.agents.supervisor import utc_now
from ubo_sentinel.audit.extraction_review import (
    ExtractionReviewError,
    parse_new_entity,
    proposal_states,
    queue,
    review_proposal,
)
from ubo_sentinel.audit.log import AuditChainError, AuditLog, var_dir
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.cli.show import audit_head, utf8_stdout
from ubo_sentinel.graph.store import GraphNotBuilt
from ubo_sentinel.llm.client import LLMUnavailable, client_for
from ubo_sentinel.llm.config import FEATURE_FLAGS, MASTER_FLAG, load_config
from ubo_sentinel.models.ontology import OntologyConfig, load_ontology
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.pipeline.db import connect, db_path
from ubo_sentinel.pipeline.extraction.document import DocumentRefused
from ubo_sentinel.pipeline.extraction.llm_extract import UNAVAILABLE
from ubo_sentinel.pipeline.extraction.run import run_extraction
from ubo_sentinel.present import Masker, extraction_text, proposal_lines


def _ontology() -> OntologyConfig:
    try:
        return load_ontology(ONTOLOGY_PATH)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Cannot read the ontology: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _masker(set_ids: list[str], role: str, ontology: OntologyConfig) -> Masker:
    con = connect(read_only=True)
    try:
        return Masker(con, set_ids, role, ontology)
    finally:
        con.close()


@app.command()
def extract(
    file: Annotated[Path, typer.Argument(help="A .txt or .pdf document.")],
    subject: Annotated[str, typer.Option("--subject", help="The entity the document is about.")],
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="The set to read against.")
    ] = "latest",
    allow_egress: Annotated[
        bool,
        typer.Option(
            "--allow-egress",
            help="Consent to send this document's text to the model API (known persons"
            " pseudonymised first).",
        ),
    ] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="The parser only: no model.")] = False,
    investigation_id: Annotated[
        str | None,
        typer.Option("--investigation-id", help="The investigation that asked for this document."),
    ] = None,
    role: Annotated[str, typer.Option("--role")] = "analyst",
    actor: Annotated[str | None, typer.Option("--actor", help="Default: the role.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read the holdings a document states, for a reviewer to rule on. Writes no graph data."""
    require_role(role, "extract")
    utf8_stdout()
    ontology = _ontology()
    try:
        config = load_config()
    except ValueError as exc:
        typer.echo(f"Cannot read the configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    client = budget = None
    note = None
    if no_llm:
        note = "First pass only (--no-llm)."
    elif not config.enabled("extract"):
        note = (
            "First pass only: the model is off"
            f" (set {MASTER_FLAG}=on and {FEATURE_FLAGS['extract']}=on)."
        )
    elif not allow_egress:
        note = "First pass only: the document leaves the machine only with --allow-egress."
    else:
        try:
            client, budget = client_for(config), config.extract
        except LLMUnavailable as exc:
            typer.echo(f"No model: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if not db_path().exists():
        typer.echo("There is no database yet: ingest and build a graph first.", err=True)
        raise typer.Exit(code=2)
    log = AuditLog()
    try:
        found, recorded = run_extraction(
            log, ontology, file, subject, snapshot, actor or role, role, utc_now(), var_dir(),
            client=client, budget=budget, consent=allow_egress, max_tokens=config.max_tokens,
            investigation_id=investigation_id,
        )  # fmt: skip
    except (DocumentRefused, LookupError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (UnknownSnapshotSet, GraphNotBuilt, AuditChainError, PermissionError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(found.model_dump_json(indent=2))
    else:
        if note:
            typer.echo(note)
        typer.echo(extraction_text(found, _masker([found.snapshot_set_id], role, ontology), role))
        typer.echo(
            f"Recorded as {found.event_id}." if recorded else "Already on record: the same run."
        )
        typer.echo(audit_head(log))
    if UNAVAILABLE in found.faults:
        raise typer.Exit(code=1)


class Ruling(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    REVOKE = "revoke"


_ACTIONS = {Ruling.ACCEPT: "ACCEPTED", Ruling.REJECT: "REJECTED", Ruling.REVOKE: "REVOKED"}


def _queue_text(records: list[dict[str, Any]], role: str, ontology: OntologyConfig) -> str:
    waiting = queue(records)
    if not waiting:
        return "No proposal waits for a ruling."
    set_ids = sorted({state.extraction.snapshot_set_id for state in waiting})
    masker = _masker(set_ids, role, ontology)
    lines = [f"Proposals waiting for a ruling: {len(waiting)}"]
    for state in waiting:
        lines += proposal_lines(state.proposal, masker, role)
    return "\n".join(lines)


@app.command("extract-review")
def extract_review(
    proposal_id: Annotated[
        str | None, typer.Argument(help="A proposal; none lists the queue.")
    ] = None,
    role: Annotated[str, typer.Option("--role", help="Required: reviewer.")] = "",
    actor: Annotated[str | None, typer.Option("--actor")] = None,
    ruling: Annotated[Ruling | None, typer.Option("--action")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="The document's effective date, YYYY-MM-DD.")
    ] = None,
    owner: Annotated[str | None, typer.Option("--owner", help="The owner's entity id.")] = None,
    owner_new: Annotated[
        str | None, typer.Option("--owner-new", help="A new holder: 'name;type[;country]'.")
    ] = None,
    asset: Annotated[
        str | None, typer.Option("--asset", help="The asset's entity id (default: the subject).")
    ] = None,
    asset_new: Annotated[str | None, typer.Option("--asset-new")] = None,
    pct: Annotated[str | None, typer.Option("--pct", help="An edited stake.")] = None,
) -> None:
    """List the proposals waiting for a ruling, or accept, reject or revoke one."""
    if not role:
        typer.echo("Pass --role reviewer.", err=True)
        raise typer.Exit(code=2)
    require_role(role, "extract-review")
    utf8_stdout()
    ontology = _ontology()
    log = AuditLog()
    try:
        records = log.records()
    except AuditChainError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if proposal_id is None:
        typer.echo(_queue_text(records, role, ontology))
        return
    state = proposal_states(records).get(proposal_id)
    if state is None:
        typer.echo(f"No proposal {proposal_id} in the audit log.", err=True)
        raise typer.Exit(code=2)
    if ruling is None:
        masker = _masker([state.extraction.snapshot_set_id], role, ontology)
        for line in proposal_lines(state.proposal, masker, role, status=state.status):
            typer.echo(line)
        return
    if not actor:
        typer.echo("Pass --actor: who rules on this proposal.", err=True)
        raise typer.Exit(code=2)
    try:
        when = date.fromisoformat(as_of) if as_of else None
        stake = Decimal(pct) if pct else None
        event = review_proposal(
            log, ontology, proposal_id, _ACTIONS[ruling], actor, role, utc_now(), var_dir(),
            reason=reason, as_of=when, owner_id=owner,
            owner_new=parse_new_entity(owner_new) if owner_new else None,
            asset_id=asset, asset_new=parse_new_entity(asset_new) if asset_new else None,
            pct=stake,
        )  # fmt: skip
    except (ValueError, InvalidOperation, ExtractionReviewError, LookupError) as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except (UnknownSnapshotSet, GraphNotBuilt, AuditChainError, PermissionError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Proposal {proposal_id}: {_ACTIONS[ruling]} by {actor}.")
    if ruling is Ruling.ACCEPT:
        typer.echo("Publish with: ubo ingest --source documents (role engineer).")
    typer.echo(f"Audit head: {event.hash}")
