"""`ubo normalize`: build Silver for one snapshot set."""

from typing import Annotated

import typer

from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role
from ubo_sentinel.models.ontology import load_ontology
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.silver import SilverError
from ubo_sentinel.pipeline.transformer import SilverSummary, build_silver


def _print_summary(summary: SilverSummary) -> None:
    counts = summary.counts
    typer.echo(f"Snapshot set: {summary.snapshot_set_id} ({summary.status})")
    lines = [
        ("silver_entities", counts["entities"], f"from {counts['entity_records']} records"),
        ("silver_entity_xref", counts["entity_records"], f"{counts['merged_records']} merged"),
        ("silver_entity_names", counts["names"], ""),
        ("silver_entity_links", counts["links"], ""),
        (
            "silver_relationships",
            counts["relationships"],
            f"from {counts['relationship_records']} records",
        ),
        ("silver_sanctions", counts["sanctions"], f"from {counts['designations']} designations"),
        ("silver_reporting_exceptions", counts["reporting_exceptions"], ""),
        ("silver_gaps", counts["gaps"], ""),
        ("silver_rejects", sum(counts["rejects"].values()), ""),
    ]
    for table, rows, note in lines:
        typer.echo(f"  {table:<28} {rows:>6} rows  {note}".rstrip())
    for reason, rows in counts["rejects"].items():
        typer.echo(f"    {reason:<26} {rows:>6}")
    typer.echo(f"Digest: {summary.silver_digest}")


@app.command()
def normalize(
    snapshot: Annotated[
        str, typer.Option("--snapshot", help="Snapshot set: fixtures, latest or a set id.")
    ] = bronze.ALIAS_LATEST,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Replace a set that is already built.")
    ] = False,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Build the canonical Silver layer for one snapshot set."""
    require_role(role, "normalize")
    con = connect()
    try:
        bronze.create_schema(con)
        snapshot_set = resolve_set(con, snapshot)
        summary = build_silver(con, snapshot_set, load_ontology(ONTOLOGY_PATH), rebuild=rebuild)
    except UnknownSnapshotSet as exc:
        typer.echo(f"{exc} Run 'ubo ingest' first.", err=True)
        raise typer.Exit(code=2) from exc
    except SilverError as exc:
        typer.echo(f"Normalize failed, nothing was written: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        con.close()
    _print_summary(summary)
