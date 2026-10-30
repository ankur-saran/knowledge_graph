"""What changed between two snapshot sets, and what is to be screened again because of it."""

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ubo_sentinel.models.provenance import NonEmptyStr

# Why a change may move a decision. LIST: a designation was added, removed or
# changed (program, list, dates, in force). LINK: a designation now reaches
# another entity, or a link between two records came or went. OWNERSHIP: an
# owner, parent or controller, or a stake. ENTITY: a field a rule reads (LEI
# status, jurisdiction, a reporting exception, whether a record has owners).
Cause = Literal["LIST", "LINK", "OWNERSHIP", "ENTITY"]
ChangeKind = Literal["ADDED", "REMOVED", "CHANGED"]
# The table a change was found in.
Domain = Literal["designation", "edge", "link", "entity", "exception", "set_gap"]

# Why a decision is on the queue: a change reaches it, a query that found
# nothing now finds something, or there was no baseline to compare with (the
# older set is not built): unknown is queued, never skipped.
QueueReason = Literal["CHANGED", "NOW_RESOLVES", "BASELINE_MISSING"]
# Which test found it: the decision's own memo (what the rules read), the walk
# down the graph from each change, both, or the resolver.
Basis = Literal["FOOTPRINT", "TRAVERSAL", "BOTH", "RESOLVER", "BASELINE"]
# PENDING: waits for a re-screen. DONE: a decision on the same entity, pack and
# depth is on record for the current set. TARGET_GONE: the entity is not in the
# current graph (merged into another record, or removed), so a person must look.
QueueStatus = Literal["PENDING", "DONE", "TARGET_GONE"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Change(_Model):
    """One row that differs between two sets, in the fields a rule reads."""

    change_id: NonEmptyStr
    domain: Domain
    kind: ChangeKind
    cause: Cause
    # The row's key within its table.
    key: NonEmptyStr
    # The entities whose screens the change may move, sorted.
    anchor_ids: list[str]
    # The compared fields that differ (CHANGED only), sorted.
    fields: list[str] = []
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class EntityHistoryStep(_Model):
    """What changed about one entity from one built set of its family to the next newer one.

    `refused` says why two sets could not be compared (built by other code);
    the step is then empty, never silently missing.
    """

    prev_set_id: NonEmptyStr
    curr_set_id: NonEmptyStr
    changes: list[Change]
    refused: str | None = None


class SetDiff(_Model):
    prev_set_id: NonEmptyStr
    curr_set_id: NonEmptyStr
    family: NonEmptyStr
    # In domain, key and kind order.
    changes: list[Change]
    # SHA-256 over the changes: the same two sets give the same digest anywhere.
    diff_digest: NonEmptyStr

    def counts(self) -> dict[str, dict[str, int]]:
        """Cause -> kind -> number of changes."""
        found: dict[str, Counter] = {}
        for change in self.changes:
            found.setdefault(change.cause, Counter())[change.kind] += 1
        return {cause: dict(sorted(kinds.items())) for cause, kinds in sorted(found.items())}


class RescreenItem(_Model):
    """A decision that newer data may move, and why."""

    prior_decision_id: NonEmptyStr
    # None for a screen that found nothing; `query_norm` names it then.
    target_id: str | None
    query_norm: str = ""
    prior_set_id: NonEmptyStr
    prior_recommendation: str
    prior_status: str
    prior_outcome: str | None = None
    rule_pack_id: NonEmptyStr
    max_depth: int = Field(ge=1)
    reason: QueueReason
    causes: list[Cause] = []
    # The changes that reach the decision, sorted.
    change_ids: list[str] = []
    basis: Basis
    # The fewest hops from a change down to the target (TRAVERSAL), when known.
    hops: int | None = None
    # Lower is graver; the queue is in (priority, target, decision) order.
    priority: int = Field(ge=0)
    status: QueueStatus = "PENDING"


class MonitorRun(_Model):
    """One run of the monitor: the sets compared and the queue it found.

    `run_id` is derived from what was compared and what was found, so running
    the monitor again on the same data names the same run, and records nothing.
    """

    run_id: NonEmptyStr
    family: NonEmptyStr
    curr_set_id: NonEmptyStr
    # Baseline set -> the digest of its diff with the current set; None when it
    # could not be compared (not built).
    baselines: dict[str, str | None]
    counts: dict[str, dict[str, dict[str, int]]]
    items: list[RescreenItem]
    queue_digest: NonEmptyStr
