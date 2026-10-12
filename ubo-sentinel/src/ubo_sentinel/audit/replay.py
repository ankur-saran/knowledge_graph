"""Replay: screen a recorded decision's request again and compare.

The audit log shows that a decision was not altered. A replay shows that it
still follows from its inputs: the same request, the rule pack as the event
recorded it, the same snapshot set. The checks run from the cheapest to the
dearest and stop at the first that fails, so the verdict says which layer
moved and not only that something did. Nothing is written.
"""

import json
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, ValidationError

from ubo_sentinel import __version__
from ubo_sentinel.agents.audit import differing_fields
from ubo_sentinel.agents.base import PIPELINE_VERSION, ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.supervisor import Recommended, ScreenFailed, Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.store import GraphNotBuilt, evidence_info
from ubo_sentinel.models.canonical import canonical_json, sha256_hex
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.pipeline import bronze, gold, silver
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.pipeline.db import connect, db_path
from ubo_sentinel.rules.pack import RULES_VERSION, RulePack

Verdict = Literal[
    "IDENTICAL",
    # This installation cannot run the replay: other code, or the data is not here.
    "NOT_REPLAYABLE",
    # The Bronze rows the decision was made from are not the rows found.
    "SOURCE_DATA_CHANGED",
    # Bronze is as it was; Silver or Gold is not: a rebuild, another transform, a changed row.
    "GRAPH_CHANGED",
    "RESOLUTION_DIFFERS",
    "PAYLOAD_DIFFERS",
    "REPLAY_FAILED",
]


class UnknownDecision(LookupError):
    pass


class ReplayReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    verdict: Verdict
    detail: str = ""
    # Fields of the canonical payload in which the replay differs from the record.
    differing_fields: list[str] = []
    recorded_payload_sha256: str
    replayed_payload_sha256: str | None = None
    # Where the digests the database was compared with came from: the audit
    # event, or (for an event written before it held them) the database itself.
    evidence_source: Literal["event", "registry"] = "event"
    # True when the digests were computed from the rows, not read from the registries.
    recomputed: bool = False

    @property
    def identical(self) -> bool:
        return self.verdict == "IDENTICAL"


def replay_decision(
    log: AuditLog,
    ontology: OntologyConfig,
    decision_id: str,
    con: duckdb.DuckDBPyConnection | None = None,
    deep: bool = False,
) -> ReplayReport:
    """Replay one recorded decision.

    `deep` computes every digest from the rows; without it the registries are
    read, and the rows only when the replay differs.

    Raises `UnknownDecision`, and `AuditChainError` for a log that does not verify.
    """
    event = next(
        (
            record
            for record in log.records()
            if record["decision_id"] == decision_id and record["event_type"] == "RECOMMENDED"
        ),
        None,
    )
    if event is None:
        raise UnknownDecision(f"No decision '{decision_id}' is on record.")
    payload = event["payload"]
    decision = Decision.model_validate(payload["decision"])
    fields: dict[str, Any] = {
        "decision_id": decision_id,
        "recorded_payload_sha256": sha256_hex(decision.canonical_payload()),
        "evidence_source": "event" if payload.get("evidence") else "registry",
    }

    def report(verdict: Verdict, detail: str = "", **more: Any) -> ReplayReport:
        return ReplayReport(verdict=verdict, detail=detail, **(fields | more))

    refusal = _not_replayable_here(decision, payload)
    if refusal:
        return report("NOT_REPLAYABLE", refusal)
    pack = RulePack.model_validate(payload["rule_pack"])
    request = ScreenRequest.model_validate(
        payload["request"] | {"snapshot": decision.snapshot_set_id}
    )

    own = con is None
    if own:
        if not db_path().exists():
            return report("NOT_REPLAYABLE", "There is no database here.")
        con = connect(read_only=True)
    try:
        set_id = decision.snapshot_set_id
        try:
            members = bronze.resolve_set(con, set_id).snapshot_ids
        except (UnknownSnapshotSet, duckdb.CatalogException):
            return report("NOT_REPLAYABLE", f"Snapshot set {set_id} is not in this database.")
        if dict(members) != decision.snapshot_ids:
            return report("SOURCE_DATA_CHANGED", f"Snapshot set {set_id} has other members.")

        changed = _evidence_changed(con, decision, payload.get("evidence"), deep)
        if changed:
            return report(changed[0], changed[1], recomputed=deep)

        try:
            outcome = Supervisor(ontology).run(request, con, pack)
        except GraphNotBuilt as exc:
            return report("NOT_REPLAYABLE", str(exc))
        except ScreenUsageError as exc:
            return report("RESOLUTION_DIFFERS", str(exc))

        if isinstance(outcome, ScreenFailed):
            return report("REPLAY_FAILED", f"{outcome.agent}: {outcome.error}")
        resolution = outcome.resolution.model_dump(mode="json")
        if json.loads(canonical_json(resolution)) != payload["resolution"]:
            verdict, detail, differing = "RESOLUTION_DIFFERS", "The query resolves differently.", []
        elif not isinstance(outcome, Recommended):
            verdict, detail, differing = "RESOLUTION_DIFFERS", "The query now needs a pick.", []
        else:
            differing = differing_fields(outcome.decision, decision)
            fields["replayed_payload_sha256"] = outcome.payload_sha256
            if not differing:
                return report("IDENTICAL", recomputed=deep)
            verdict, detail = "PAYLOAD_DIFFERS", "The replay gives another decision."
        if not deep:
            # Something moved. The rows say whether it was the data or the code.
            changed = _evidence_changed(con, decision, payload.get("evidence"), deep=True)
            if changed:
                return report(changed[0], changed[1], differing_fields=differing, recomputed=True)
        return report(verdict, detail, differing_fields=differing, recomputed=True)
    finally:
        if own:
            con.close()


def replay_all(
    log: AuditLog, ontology: OntologyConfig, con: duckdb.DuckDBPyConnection | None = None
) -> list[ReplayReport]:
    """Replay every decision on record, in the order they were recommended."""
    return [
        replay_decision(log, ontology, decision.decision_id, con) for decision in log.decisions()
    ]


def _not_replayable_here(decision: Decision, payload: dict[str, Any]) -> str | None:
    """Why this code cannot replay the decision, from the event alone."""
    missing = sorted({"request", "resolution", "rule_pack"} - set(payload))
    if missing:
        return f"The event does not hold what a replay needs: {', '.join(missing)}."
    if decision.engine_version != __version__:
        return (
            f"The decision was made by engine {decision.engine_version}; this is {__version__}."
            " Replay it with that version."
        )
    if decision.pipeline_version != PIPELINE_VERSION:
        return (
            f"The decision was made by pipeline version {decision.pipeline_version};"
            f" this is {PIPELINE_VERSION}."
        )
    try:
        pack = RulePack.model_validate(payload["rule_pack"])
        ScreenRequest.model_validate(payload["request"])
    except ValidationError:
        return "The recorded rule pack or request is not one this version reads."
    if pack.rule_pack_hash != decision.rule_pack_hash:
        return (
            "The recorded rule pack no longer gives the recorded hash:"
            f" the rule engine has changed (RULES_VERSION is {RULES_VERSION})."
        )
    return None


def _evidence_changed(
    con: duckdb.DuckDBPyConnection,
    decision: Decision,
    recorded: dict[str, Any] | None,
    deep: bool,
) -> tuple[Verdict, str] | None:
    """The first layer, from Bronze up, that is not what the decision was made from.

    The database's registries are compared with the digests in the audit
    event. With `deep` the digests are computed from the rows as well, which
    also finds a row changed behind a registry.
    """
    set_id = decision.snapshot_set_id
    try:
        current = evidence_info(con, set_id)
    except duckdb.CatalogException:
        return "NOT_REPLAYABLE", f"Snapshot set {set_id} is not normalised in this database."
    expected = recorded or current

    for dataset, was in sorted(expected["snapshots"].items()):
        now = current["snapshots"].get(dataset)
        if now is None or now["rows_digest"] != was["rows_digest"]:
            return "SOURCE_DATA_CHANGED", f"The registry's digest of '{dataset}' has changed."
        if deep:
            table = con.execute(
                "SELECT table_name FROM bronze_snapshots WHERE snapshot_id = ?",
                [was["snapshot_id"]],
            ).fetchone()[0]
            if bronze.rows_digest(con, table, was["snapshot_id"]) != was["rows_digest"]:
                return "SOURCE_DATA_CHANGED", f"The Bronze rows of '{dataset}' have changed."

    # Bronze is as it was, so a difference from here on is the transform's or a rebuild's.
    if current["silver_digest"] != expected["silver_digest"]:
        return "GRAPH_CHANGED", "Silver was rebuilt and is not the one the decision read."
    if deep and current["silver_digest"] is not None:
        if silver.silver_digest(con, set_id) != expected["silver_digest"]:
            return "GRAPH_CHANGED", "The Silver rows have changed."

    try:
        built = gold.load_build(con, set_id)
    except duckdb.CatalogException:
        built = None
    if built is None:
        # Not a change: there is no graph to compare. Opening the store says so.
        return None
    if built["gold_digest"] != decision.graph_digest:
        return "GRAPH_CHANGED", "The graph was rebuilt and is not the one the decision read."
    if deep and gold.gold_digest(con, set_id, built["silver_digest"]) != decision.graph_digest:
        return "GRAPH_CHANGED", "The Gold rows have changed."
    return None
