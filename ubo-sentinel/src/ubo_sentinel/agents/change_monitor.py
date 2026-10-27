"""Change monitor (G6): which recorded decisions newer data may move, and screening them again.

Not a step of a screen. For each entity screened before, the newest decision
made on an older set of the family is compared with the current set:

- the two sets are diffed (`pipeline/differ.py`), once per older set;
- a decision is reached by a change in two ways, and either queues it:
  *footprint*, a change to an entity or edge its own memo shows the rules
  read; *traversal*, a change from which the decision's target is reached
  down the graph within the decision's depth (crossing a link at every
  entity, so a duplicate record's new owner reaches the record screened);
- a screen that found nothing is queued when the resolver now finds something;
- a decision whose older set is not built is queued all the same: unknown is
  queued, never skipped.

The queue is derived: an item is DONE once a decision on the same entity, rule
pack and depth is on record for the current set. A run is recorded once, as a
`MONITOR_RUN` event; its id comes from what was compared and found.
Re-screens go through the Supervisor by target id, so each is an ordinary
decision that waits for a reviewer.
"""

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import (
    NeedsDisambiguation,
    Recommended,
    ScreenFailed,
    Supervisor,
)
from ubo_sentinel.audit.log import AuditLog, var_dir
from ubo_sentinel.audit.review import supersedes_of  # noqa: F401 - shown with the queue
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models import audit_event
from ubo_sentinel.models.canonical import canonical_json, sha256_hex, short_id
from ubo_sentinel.models.change import Change, MonitorRun, RescreenItem, SetDiff
from ubo_sentinel.models.decision import Decision, not_found_subject
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.pipeline.bronze import SnapshotSet, resolve_set
from ubo_sentinel.pipeline.differ import SetNotBuilt, diff_sets
from ubo_sentinel.rules.pack import DEFAULT_PACK

RUN_EVENT = "MONITOR_RUN"
RUN_PREFIX = audit_event.RUN_PREFIX[RUN_EVENT]
# Who the re-screens are made by when no one is named: maker and checker are
# two actors, so any person may then review them.
MONITOR_ACTOR = "system:monitor"

_SEVERITY = {"CLEAR": 0, "REVIEW": 1, "ESCALATE": 2}


class MonitorError(ValueError):
    """The monitor cannot run as asked: no current set, or one the quality check refused."""


@dataclass(frozen=True)
class _Baseline:
    """A recorded decision that newer data may move, with what the monitor needs of it."""

    decision: Decision
    set_seq: int
    # The request as recorded: the pack's name, and for a screen that found
    # nothing, the query and identifiers to look up again.
    request: dict[str, Any]


def item_key(decision: Decision) -> tuple[str, str, str, int]:
    """What a re-screen must match to replace a decision: the entity (or the query
    that found nothing), the rule pack and the depth."""
    if decision.target_id is not None:
        return ("target", decision.target_id, decision.rule_pack_id, decision.max_depth)
    subject = not_found_subject(decision.query, decision.query_identifiers)
    return ("query", subject, decision.rule_pack_id, decision.max_depth)


def default_curr(con: duckdb.DuckDBPyConnection, family: str | None) -> SnapshotSet:
    """The set to compare with: for the real family the accepted `real` set; else the
    newest set of the family (of `latest` when none is named)."""
    if family is None:
        family = resolve_set(con, bronze.ALIAS_LATEST).family
    if family == bronze.FAMILY_REAL:
        return resolve_set(con, bronze.ALIAS_REAL)
    row = con.execute(
        "SELECT snapshot_set_id FROM snapshot_set_info WHERE family = ? ORDER BY seq DESC LIMIT 1",
        [family],
    ).fetchone()
    if row is None:
        raise bronze.UnknownSnapshotSet(f"No snapshot set of the {family} family.")
    return resolve_set(con, row[0])


def is_accepted(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> bool:
    """True when the data-quality check accepted the set (`ubo refresh`)."""
    try:
        row = con.execute(
            f"SELECT 1 FROM {bronze.QUALITY_TABLE} WHERE snapshot_set_id = ? AND accepted",
            [snapshot_set_id],
        ).fetchone()
    except duckdb.CatalogException:
        return False
    return row is not None


def check_curr(
    con: duckdb.DuckDBPyConnection, curr: SnapshotSet, allow_unaccepted: bool, reason: str | None
) -> None:
    """A real set the quality check did not accept is compared only on purpose, with a reason.

    A truncated file looks like mass delistings.
    """
    if curr.family != bronze.FAMILY_REAL or is_accepted(con, curr.snapshot_set_id):
        return
    if not allow_unaccepted:
        raise MonitorError(
            f"Set {curr.snapshot_set_id} has not passed the data-quality check (`ubo refresh`):"
            " a shrunken file would look like delistings. Compare with it all the same with"
            " --allow-unaccepted and a --reason."
        )
    if not (reason or "").strip():
        raise MonitorError("Comparing with a set the quality check did not accept needs a reason.")


def _set_info(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, int]]:
    return {
        set_id: (family, seq)
        for set_id, family, seq in con.execute(
            "SELECT snapshot_set_id, family, seq FROM snapshot_set_info"
        ).fetchall()
    }


def baselines(
    log: AuditLog, con: duckdb.DuckDBPyConnection, curr: SnapshotSet
) -> tuple[list[_Baseline], set[tuple], list[str]]:
    """The decisions to compare with `curr`, the keys already screened on it, and the
    sets of recorded decisions this database does not hold.

    For each key, the newest decision made on an older set of the family: by
    the set's order, then the log's. Reviewed decisions count: an approved
    CLEAR is the decision a firm acts on.
    """
    info = _set_info(con)
    requests = {
        record["decision_id"]: record["payload"].get("request") or {}
        for record in log.records()
        if record["event_type"] == "RECOMMENDED"
    }
    newest: dict[tuple, _Baseline] = {}
    done: set[tuple] = set()
    unknown: set[str] = set()
    for decision in log.decisions():
        found = info.get(decision.snapshot_set_id)
        if found is None:
            unknown.add(decision.snapshot_set_id)
            continue
        family, seq = found
        if family != curr.family or seq > curr.seq:
            continue
        request = requests.get(decision.decision_id, {})
        keys = {item_key(decision)}
        if decision.snapshot_set_id == curr.snapshot_set_id:
            # A screen that now resolves replaces the one that found nothing.
            query = not_found_subject(decision.query, decision.query_identifiers)
            done |= keys | {("query", query, decision.rule_pack_id, decision.max_depth)}
            continue
        key = item_key(decision)
        held = newest.get(key)
        if held is None or seq >= held.set_seq:
            newest[key] = _Baseline(decision, seq, request)
    return list(newest.values()), done, sorted(unknown)


def footprint(decision: Decision) -> tuple[set[str], set[str]]:
    """(every entity the memo names, the entities above the target the rules read)."""
    memo = Memo.model_validate(decision.memo_json)
    named = {entity.id for entity in memo.entities}
    upward = {entity.id for entity in memo.entities if entity.depth is not None}
    if decision.target_id is not None:
        named.add(decision.target_id)
        upward.add(decision.target_id)
    return named, upward


def footprint_hits(decision: Decision, changes: list[Change]) -> list[Change]:
    """The changes to what the decision's rules read.

    A designation, an entity field or a reporting exception on any entity the
    memo names; an owner, a stake, a set gap or a link at an entity above the
    target, at any depth up to the limit (a new owner of an entity at the
    limit is a new `DEPTH_LIMIT_REACHED`).
    """
    named, upward = footprint(decision)
    hits = []
    for change in changes:
        scope = upward if change.domain in {"edge", "set_gap", "link"} else named
        if scope & set(change.anchor_ids):
            hits.append(change)
    return hits


def traversal_hits(
    con: duckdb.DuckDBPyConnection,
    set_ids: list[str],
    changes: list[Change],
    max_depth: int,
) -> dict[str, dict[str, int]]:
    """Entity -> change id -> fewest hops, walking down from each change's entities on
    each set's graph: the older graph for what was removed, the newer for what was added."""
    by_anchor: dict[str, list[str]] = defaultdict(list)
    for change in changes:
        for anchor in change.anchor_ids:
            by_anchor[anchor].append(change.change_id)
    reached: dict[str, dict[str, int]] = defaultdict(dict)
    if not by_anchor:
        return reached
    for set_id in set_ids:
        with GraphStore.open(set_id, con) as store:
            for item in queries.get_affected_by(store, sorted(by_anchor), max_depth):
                for change_id in by_anchor[item.origin_id]:
                    held = reached[item.entity_id].get(change_id)
                    if held is None or item.hops < held:
                        reached[item.entity_id][change_id] = item.hops
    return reached


def _priority(decision: Decision, reason: str, status: str) -> int:
    """0 a CLEAR (approved or recommended) that the change may turn; 1 a REVIEW;
    2 nothing to compare, a query that now resolves, or an entity gone; 3 an ESCALATE.

    A delisting never clears by itself: a blocked entity stays blocked even if
    delisted when its owners block it (6.2), and a re-screen waits for a reviewer.
    """
    if reason != "CHANGED" or status == "TARGET_GONE":
        return 2
    effective = decision.final_outcome or decision.recommendation
    return {"CLEAR": 0, "REVIEW": 1, "ESCALATE": 3}[effective]


def _item(
    baseline: _Baseline,
    reason: str,
    basis: str,
    changes: list[Change] = (),
    hops: int | None = None,
    status: str = "PENDING",
) -> RescreenItem:
    decision = baseline.decision
    return RescreenItem(
        prior_decision_id=decision.decision_id,
        target_id=decision.target_id,
        query_norm="" if decision.target_id else item_key(decision)[1],
        prior_set_id=decision.snapshot_set_id,
        prior_recommendation=decision.recommendation,
        prior_status=decision.status,
        prior_outcome=decision.final_outcome,
        rule_pack_id=decision.rule_pack_id,
        max_depth=decision.max_depth,
        reason=reason,
        causes=sorted({change.cause for change in changes}),
        change_ids=sorted({change.change_id for change in changes}),
        basis=basis,
        hops=hops,
        priority=_priority(decision, reason, status),
        status=status,
    )


def sort_items(items: list[RescreenItem]) -> list[RescreenItem]:
    """Gravest first; among equals a reviewed decision before one still waiting."""
    return sorted(
        items,
        key=lambda i: (
            i.priority,
            i.prior_status == "RECOMMENDED",
            i.target_id or i.query_norm,
            i.prior_decision_id,
        ),
    )


def build_queue(
    log: AuditLog,
    con: duckdb.DuckDBPyConnection,
    curr: SnapshotSet,
    prev: str | None = None,
) -> tuple[MonitorRun, list[SetDiff], list[str]]:
    """The run: every decision newer data may move, from each older set to `curr`.

    `prev` keeps only the decisions made on that set. Raises `DiffRefused` when
    two sets were built by other code.
    """
    held, done, unknown = baselines(log, con, curr)
    if prev is not None:
        prev_id = resolve_set(con, prev).snapshot_set_id
        held = [b for b in held if b.decision.snapshot_set_id == prev_id]
    groups: dict[str, list[_Baseline]] = defaultdict(list)
    for baseline in held:
        groups[baseline.decision.snapshot_set_id].append(baseline)

    present = _present(con, curr.snapshot_set_id, [b.decision.target_id for b in held])
    items: list[RescreenItem] = []
    diffs: list[SetDiff] = []
    digests: dict[str, str | None] = {}
    for set_id in sorted(groups):
        group = groups[set_id]
        try:
            diff = diff_sets(con, set_id, curr.snapshot_set_id)
        except SetNotBuilt:
            digests[set_id] = None
            items += [_item(b, "BASELINE_MISSING", "BASELINE") for b in group]
            continue
        diffs.append(diff)
        digests[set_id] = diff.diff_digest
        by_id = {change.change_id: change for change in diff.changes}
        deepest = max(b.decision.max_depth for b in group)
        reached = traversal_hits(
            con, [set_id, curr.snapshot_set_id], diff.changes, deepest
        )
        for baseline in group:
            decision = baseline.decision
            if decision.target_id is None:
                if _now_resolves(con, curr.snapshot_set_id, baseline.request):
                    items.append(_item(baseline, "NOW_RESOLVES", "RESOLVER"))
                continue
            by_footprint = footprint_hits(decision, diff.changes)
            walked = {
                change_id: hops
                for change_id, hops in reached.get(decision.target_id, {}).items()
                if hops <= decision.max_depth
            }
            if not by_footprint and not walked:
                continue
            basis = "BOTH" if by_footprint and walked else "FOOTPRINT" if by_footprint else "TRAVERSAL"
            hits = {change.change_id: change for change in by_footprint}
            hits |= {change_id: by_id[change_id] for change_id in walked}
            status = "PENDING" if decision.target_id in present else "TARGET_GONE"
            items.append(
                _item(
                    baseline,
                    "CHANGED",
                    basis,
                    list(hits.values()),
                    min(walked.values()) if walked else None,
                    status,
                )
            )
    items = [_with_status(item, done) for item in sort_items(items)]
    run_id = short_id(
        [
            curr.snapshot_set_id,
            sorted(digests.items(), key=lambda pair: pair[0]),
            sorted(item.prior_decision_id for item in items),
        ]
    )
    counts = {diff.prev_set_id: diff.counts() for diff in diffs}
    run = MonitorRun(
        run_id=run_id,
        family=curr.family,
        curr_set_id=curr.snapshot_set_id,
        baselines=digests,
        counts=counts,
        items=items,
        queue_digest=sha256_hex([item.model_dump(exclude={"status"}) for item in items]),
    )
    return run, diffs, unknown


def _with_status(item: RescreenItem, done: set[tuple]) -> RescreenItem:
    kind = "target" if item.target_id else "query"
    key = (kind, item.target_id or item.query_norm, item.rule_pack_id, item.max_depth)
    if key in done:
        return item.model_copy(update={"status": "DONE"})
    return item


def refresh_statuses(run: MonitorRun, log: AuditLog, con: duckdb.DuckDBPyConnection) -> MonitorRun:
    """The run with each item's status as the log stands now."""
    curr = resolve_set(con, run.curr_set_id)
    _, done, _ = baselines(log, con, curr)
    present = _present(con, curr.snapshot_set_id, [item.target_id for item in run.items])
    items = []
    for item in run.items:
        status = "TARGET_GONE" if item.target_id and item.target_id not in present else "PENDING"
        items.append(_with_status(item.model_copy(update={"status": status}), done))
    return run.model_copy(update={"items": items})


def _present(con: duckdb.DuckDBPyConnection, set_id: str, ids: list[str | None]) -> set[str]:
    wanted = sorted({entity for entity in ids if entity})
    if not wanted:
        return set()
    return {
        entity
        for (entity,) in con.execute(
            "SELECT id FROM graph_nodes WHERE snapshot_set_id = ?"
            " AND id IN (SELECT unnest(CAST(? AS VARCHAR[])))",
            [set_id, wanted],
        ).fetchall()
    }


def _now_resolves(con: duckdb.DuckDBPyConnection, set_id: str, request: dict[str, Any]) -> bool:
    """True when a screen that found nothing would find a candidate now."""
    with GraphStore.open(set_id, con) as store:
        return bool(
            queries.resolve_entity(
                store,
                request.get("query") or "",
                lei=request.get("lei"),
                registration_number=request.get("registration_number"),
                jurisdiction=request.get("jurisdiction"),
            )
        )


# --- the record ---------------------------------------------------------------------


def run_decision_id(run_id: str) -> str:
    return f"{RUN_PREFIX}{run_id}"


def recorded_run(log: AuditLog, run_id: str) -> MonitorRun | None:
    events = log.events_of(run_decision_id(run_id))
    return MonitorRun.model_validate(events[0]["payload"]["run"]) if events else None


def runs(log: AuditLog) -> list[dict[str, Any]]:
    """Every monitor run on record, oldest first."""
    return [record for record in log.records() if record["event_type"] == RUN_EVENT]


class _AlreadyRecorded(Exception):
    pass


def record_run(
    log: AuditLog,
    ontology: OntologyConfig,
    run: MonitorRun,
    *,
    actor: str,
    role: str,
    now: Callable[[], datetime],
    note: str | None = None,
) -> bool:
    """Append the run's event, once. False when the same run is already on record."""
    decision_id = run_decision_id(run.run_id)

    def guard(records: list[dict[str, Any]]) -> None:
        if any(record["decision_id"] == decision_id for record in records):
            raise _AlreadyRecorded

    try:
        log.append(
            ontology=ontology,
            decision_id=decision_id,
            event_type=RUN_EVENT,
            actor=actor,
            role=role,
            created_at=now(),
            note=note,
            payload={"run": run.model_dump(mode="json")},
            guard=guard,
        )
    except _AlreadyRecorded:
        return False
    return True


def write_queue(run: MonitorRun, root: Path | None = None) -> Path:
    """The derived copies: `var/rescreen_queue.jsonl` (this run's queue) and
    `var/monitor/<run id>.json`. Neither is read back; the log is the record."""
    root = var_dir() if root is None else root
    (root / "monitor").mkdir(parents=True, exist_ok=True)
    _write(root / "monitor" / f"{run.run_id}.json", run.model_dump_json(indent=2) + "\n")
    lines = [canonical_json(item.model_dump(mode="json")).decode("utf-8") for item in run.items]
    path = root / "rescreen_queue.jsonl"
    _write(path, "".join(f"{line}\n" for line in lines))
    return path


def _write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# --- re-screening -------------------------------------------------------------------


@dataclass(frozen=True)
class RescreenResult:
    item: RescreenItem
    # RECORDED, ON_RECORD (the same decision was there), NEEDS_PICK, FAILED, SKIPPED.
    outcome: str
    decision: Decision | None = None
    # UPGRADE, DOWNGRADE or UNCHANGED against the prior outcome, or recommendation.
    transition: str | None = None
    error_type: str | None = None


def transition(prior: str, new: str) -> str:
    step = _SEVERITY[new] - _SEVERITY[prior]
    return "UPGRADE" if step > 0 else "DOWNGRADE" if step < 0 else "UNCHANGED"


def rescreen(
    run: MonitorRun,
    supervisor: Supervisor,
    con: duckdb.DuckDBPyConnection,
    *,
    actor: str,
    role: str,
    requests: dict[str, dict[str, Any]],
) -> list[RescreenResult]:
    """Screen every pending item again, on the current set, as an ordinary screen.

    By target id, with the recorded pack and depth: a pick is an index into the
    candidates of one set, and another set may order them otherwise. A screen
    that found nothing is run again by its query. Raises what the Supervisor
    raises for a request that cannot be run; a failed screen is recorded by
    the Supervisor and the run goes on.
    """
    results = []
    for item in run.items:
        if item.status != "PENDING":
            results.append(RescreenResult(item, "SKIPPED"))
            continue
        recorded = requests.get(item.prior_decision_id, {})
        common = {
            "snapshot": run.curr_set_id,
            "rule_pack": recorded.get("rule_pack") or DEFAULT_PACK,
            "max_depth": item.max_depth,
            "role": role,
            "actor": actor,
            "supersedes": item.prior_decision_id,
            "monitor_run_id": run.run_id,
        }
        if item.target_id is not None:
            request = ScreenRequest(
                query=recorded.get("query") or "", target_id=item.target_id, **common
            )
        else:
            request = ScreenRequest(
                query=recorded.get("query") or "",
                lei=recorded.get("lei"),
                registration_number=recorded.get("registration_number"),
                jurisdiction=recorded.get("jurisdiction"),
                **common,
            )
        outcome = supervisor.run(request, con)
        if isinstance(outcome, Recommended):
            prior = item.prior_outcome or item.prior_recommendation
            results.append(
                RescreenResult(
                    item,
                    "RECORDED" if outcome.is_new else "ON_RECORD",
                    outcome.decision,
                    transition(prior, outcome.decision.recommendation),
                )
            )
        elif isinstance(outcome, NeedsDisambiguation):
            results.append(RescreenResult(item, "NEEDS_PICK"))
        else:
            assert isinstance(outcome, ScreenFailed)
            results.append(RescreenResult(item, "FAILED", error_type=outcome.error_type))
    return results


def recorded_requests(log: AuditLog) -> dict[str, dict[str, Any]]:
    """Decision id -> the request it was screened with."""
    return {
        record["decision_id"]: record["payload"].get("request") or {}
        for record in log.records()
        if record["event_type"] == "RECOMMENDED"
    }


def changes_by_decision(run: MonitorRun) -> dict[str, list[str]]:
    """Change id -> the prior decisions it reaches, for the change digest."""
    touched: dict[str, list[str]] = defaultdict(list)
    for item in run.items:
        for change_id in item.change_ids:
            touched[change_id].append(item.prior_decision_id)
    return {change_id: sorted(ids) for change_id, ids in touched.items()}


def monitor_json(run: MonitorRun) -> str:
    return json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
