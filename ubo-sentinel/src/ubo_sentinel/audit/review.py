"""Human review: the one place a recommended decision becomes a reviewed one.

    RECOMMENDED → APPROVED | OVERRIDDEN | ESCALATED

A review ends in an outcome, CLEAR or ESCALATE. REVIEW is something the rules
say when they cannot conclude; a reviewer cannot say it back. The CLI and the
UI both call `review_decision`, and what it checks is checked again inside the
log's lock, so two reviewers cannot both review one decision.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from ubo_sentinel.audit.log import REVIEW_EVENTS, AuditLog
from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.decision import (
    Decision,
    DecisionStatus,
    FinalOutcome,
    not_found_subject,
)
from ubo_sentinel.models.evidence import ReasonCode
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.models.ontology import OntologyConfig

Action = Literal["approve", "override", "escalate"]
# How a reviewer settled the matches the rules could not: all confirmed, or all rejected.
MatchRuling = Literal["confirm", "reject"]

# A decision with one of these reasons says that nothing was screened, or that
# its memo is not to be trusted. A reviewer cannot clear on it; the entity is
# screened again, or escalated.
NOT_CLEARABLE: frozenset[ReasonCode] = frozenset(
    {"ENTITY_NOT_FOUND", "EVIDENCE_LIMIT_EXCEEDED", "GUARDRAIL_FAILED"}
)

_SEVERITY = {"ESCALATE": 0, "REVIEW": 1, "CLEAR": 2}


class ReviewError(ValueError):
    """The review cannot be made as asked. Nothing was written."""


def settle(
    decision: Decision,
    action: Action,
    outcome: FinalOutcome | None = None,
    reason: str | None = None,
    matches: MatchRuling | None = None,
) -> tuple[DecisionStatus, FinalOutcome, str | None]:
    """The status and the outcome a review gives a decision, and what the outcome rests on.

    Raises `ReviewError` for a review the recommendation does not allow.
    """
    recommended = decision.recommendation
    reason = (reason or "").strip()
    basis = None
    if matches is not None and (action != "approve" or recommended != "REVIEW"):
        raise ReviewError("Matches are confirmed or rejected only when approving a REVIEW.")

    if action == "escalate":
        if outcome not in (None, "ESCALATE"):
            raise ReviewError("An escalated decision is not cleared.")
        status, outcome = "ESCALATED", "ESCALATE"
    elif action == "override":
        if recommended == "REVIEW":
            raise ReviewError(
                "A REVIEW recommends no outcome, so there is none to override:"
                " approve it with an outcome, or escalate it."
            )
        other: FinalOutcome = "ESCALATE" if recommended == "CLEAR" else "CLEAR"
        if outcome not in (None, other):
            raise ReviewError(f"Overriding {recommended} gives {other}; approve it to keep it.")
        if not reason:
            raise ReviewError("An override needs a reason.")
        status, outcome = "OVERRIDDEN", other
    elif recommended == "REVIEW":
        if matches is not None:
            memo = _memo(decision)
            what_if = memo.what_if if memo else None
            if what_if is None:
                raise ReviewError("This decision has no unsettled match to confirm or reject.")
            ruled = what_if.if_confirmed if matches == "confirm" else what_if.if_rejected
            basis = "MATCHES_CONFIRMED" if matches == "confirm" else "MATCHES_REJECTED"
            if ruled != "REVIEW":
                if outcome not in (None, ruled):
                    raise ReviewError(f"With the matches settled so, the rules give {ruled}.")
                outcome = ruled
        if outcome is None:
            raise ReviewError("Approving a REVIEW needs an outcome: CLEAR or ESCALATE.")
        if not reason:
            raise ReviewError("Approving a REVIEW needs a reason: the rules did not conclude.")
        status = "APPROVED"
    else:
        if outcome not in (None, recommended):
            raise ReviewError(f"Approving {recommended} gives {recommended}; override it instead.")
        status, outcome = "APPROVED", recommended

    blocking = sorted(NOT_CLEARABLE & set(decision.reasons))
    if outcome == "CLEAR" and blocking:
        memo = _memo(decision)
        codes = [item.code for item in memo.next_actions] if memo else []
        hint = f" Next: {', '.join(codes)}." if codes else ""
        raise ReviewError(
            f"A decision with {', '.join(blocking)} cannot be cleared:"
            f" screen again, or escalate.{hint}"
        )
    return status, outcome, basis


def _memo(decision: Decision) -> Memo | None:
    try:
        return Memo.model_validate(decision.memo_json)
    except ValueError:
        return None


def same_actor(one: str, other: str) -> bool:
    """Two actor names that stand for one person: "a.analyst" and " A.Analyst"."""
    return one.strip().casefold() == other.strip().casefold()


def review_decision(
    log: AuditLog,
    ontology: OntologyConfig,
    decision_id: str,
    *,
    action: Action,
    actor: str,
    role: str,
    now: Callable[[], datetime],
    outcome: FinalOutcome | None = None,
    reason: str | None = None,
    matches: MatchRuling | None = None,
    superseded: bool | None = None,
    accept_stale: bool = False,
) -> Decision:
    """Review a recommended decision and return it as reviewed.

    `superseded` says whether a newer snapshot set of the same family existed
    when the review was made; None when that is not known. A superseded
    decision is not cleared unless the reviewer says so (`accept_stale`) and
    why: the newer data may list a party that the older data did not.

    Raises `ReviewError`, `PermissionError` for a role without the right, and
    `AuditChainError`.
    """
    decision = log.decision(decision_id)
    if decision is None:
        raise ReviewError(f"No decision '{decision_id}' is on record.")
    status, outcome, basis = settle(decision, action, outcome, reason, matches)
    if outcome == "CLEAR" and superseded:
        if not accept_stale:
            raise ReviewError(
                f"Decision {decision_id} was made on snapshot set {decision.snapshot_set_id},"
                " and newer data exists: screen the entity again on it, or escalate."
                " To clear on the older data all the same, accept it as stale and give a reason."
            )
        if not (reason or "").strip():
            raise ReviewError("Clearing a decision made on older data needs a reason.")

    def guard(records: list[dict[str, Any]]) -> None:
        events = [record for record in records if record["decision_id"] == decision_id]
        reviewed = next((e for e in events if e["event_type"] in REVIEW_EVENTS), None)
        if reviewed is not None:
            raise ReviewError(
                f"Decision {decision_id} is already {reviewed['event_type']}"
                f" (by {reviewed['actor']})."
            )
        screened_by = next(e["actor"] for e in events if e["event_type"] == "RECOMMENDED")
        if same_actor(screened_by, actor):
            raise ReviewError(
                f"'{actor}' screened decision {decision_id} and cannot also review it."
            )

    log.append(
        ontology=ontology,
        decision_id=decision_id,
        event_type=status,
        actor=actor,
        role=role,
        created_at=now(),
        note=(reason or "").strip() or None,
        payload={
            "outcome": outcome,
            "recommendation": decision.recommendation,
            # What the reviewer was shown: the review speaks for this payload and no other.
            "payload_sha256": sha256_hex(decision.canonical_payload()),
            "basis": basis,
            "superseded": superseded,
            # True when the reviewer cleared in the knowledge that newer data existed.
            "accepted_stale": bool(superseded and accept_stale and outcome == "CLEAR"),
        },
        guard=guard,
    )
    return log.decision(decision_id)


def pending(log: AuditLog) -> list[Decision]:
    """The decisions waiting for a review: the gravest first, then the oldest."""
    waiting = [decision for decision in log.decisions() if decision.status == "RECOMMENDED"]
    return sorted(waiting, key=lambda d: (_SEVERITY[d.recommendation], d.created_at, d.decision_id))


def unscreened(log: AuditLog) -> list[dict[str, Any]]:
    """The last failed screen of each subject that has no decision.

    A screen that failed and was not run again is an entity nobody screened.
    """
    decided: set[str] = set()
    asked: set[tuple] = set()
    failed: dict[str, dict[str, Any]] = {}
    for record in log.records():
        payload = record["payload"]
        if record["event_type"] == "RECOMMENDED":
            decided.add(record["decision_id"])
            subject = _asked_for(payload.get("request"), payload["decision"]["snapshot_set_id"])
            if subject is not None:
                asked.add(subject)
        elif record["event_type"] == "SCREEN_FAILED":
            failed[record["decision_id"]] = record
    return [
        record
        for decision_id, record in failed.items()
        if decision_id not in decided
        and _asked_for(record["payload"].get("request"), record["payload"].get("snapshot_set_id"))
        not in asked
    ]


def _asked_for(request: dict[str, Any] | None, snapshot_set_id: str | None) -> tuple | None:
    """What a screen was asked for, on which set; None when the event does not say.

    A screen that fails before Intake has resolved the query has no target, so
    its id is not the id of the decision the same request gives when it is run
    again. The request is what the two have in common.
    """
    if request is None or snapshot_set_id is None:
        return None
    return (
        not_found_subject(
            request.get("query") or "",
            {
                "lei": (request.get("lei") or "").strip().upper(),
                "registration_number": (request.get("registration_number") or "").strip(),
            },
        ),
        request.get("target_id"),
        request.get("pick"),
        request.get("rule_pack"),
        request.get("max_depth"),
        snapshot_set_id,
    )
