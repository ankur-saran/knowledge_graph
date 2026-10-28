"""What the review UI does and shows, without Streamlit, so it can be tested as code.

The UI is one more caller of what the CLI calls: the Supervisor screens,
`review_decision` reviews, `replay_decision` replays, and the audit log is the
record. It is held to the same permission matrix, panel by panel, and every
text it shows is a role's view that has passed the render guardrail.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import duckdb

from ubo_sentinel.agents import change_monitor
from ubo_sentinel.agents.audit import DeterminismViolation
from ubo_sentinel.agents.base import ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.guardrail import check_render
from ubo_sentinel.agents.supervisor import NeedsDisambiguation, ScreenFailed, Supervisor
from ubo_sentinel.audit.log import AuditChainError, AuditLog, ChainReport
from ubo_sentinel.audit.replay import ReplayReport, replay_decision
from ubo_sentinel.audit.review import (
    NOT_CLEARABLE,
    MatchRuling,
    ReviewError,
    check_stale,
    is_superseded,
    review_decision,
    same_actor,
    settle,
)
from ubo_sentinel.graph.store import GraphNotBuilt
from ubo_sentinel.graph.views import (
    MASK,
    GraphView,
    mask_candidates,
    mask_decision,
    pseudonym_for,
)
from ubo_sentinel.models.change import MonitorRun
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.graph import EntityMatch
from ubo_sentinel.models.memo import DecisionView, Memo
from ubo_sentinel.models.ontology import OntologyConfig, require_command
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.pipeline.db import connect, db_path
from ubo_sentinel.rules.pack import UnknownRulePack

WITHHELD = "Withheld by the render guardrail: it held a value that is masked for this role."
LICENCE_NOTICE = (
    "This screen read OpenSanctions data, licensed CC BY-NC 4.0: for non-commercial use only."
)

Panel = Literal["screen", "review", "audit", "build"]

# Each panel stands for a command of the permission matrix; a role sees the
# panels whose command it may run. `audit` is shown for either audit command.
PANEL_COMMANDS: dict[Panel, tuple[str, ...]] = {
    "screen": ("screen",),
    "review": ("review",),
    "audit": ("audit-verify", "audit-replay"),
    "build": ("build-graph",),
}


def may(ontology: OntologyConfig, role: str, command: str) -> bool:
    try:
        require_command(ontology, role, command)
    except PermissionError:
        return False
    return True


def allowed_panels(ontology: OntologyConfig, role: str) -> list[Panel]:
    return [
        panel
        for panel, commands in PANEL_COMMANDS.items()
        if any(may(ontology, role, command) for command in commands)
    ]


def may_read_decisions(ontology: OntologyConfig, role: str) -> bool:
    """A role that screens, reviews or replays reads decisions; the engineer reads metadata."""
    return any(may(ontology, role, c) for c in ("screen", "review", "audit-replay"))


# --- what may be shown ------------------------------------------------------------------------


def guarded(text: str, decision: Decision, role: str, ontology: OntologyConfig) -> str | None:
    """The text, or None when the render guardrail finds a masked value in it."""
    return None if check_render(text, decision, role, ontology) else text


def candidate_secrets(
    candidates: list[EntityMatch], role: str, ontology: OntologyConfig
) -> list[str]:
    """What the role must not see of the candidates it is shown masked."""
    hidden = ontology.roles[role].masked_fields
    values: set[str] = set()
    for match in candidates:
        if match.entity_type not in hidden:
            continue
        values.add(match.entity_id)
        for name in ("legal_name", "matched_name", *hidden[match.entity_type]):
            value = getattr(match, name, None)
            if isinstance(value, str) and value and value != MASK:
                values.add(value)
    return sorted(values)


def candidate_rows(
    candidates: list[EntityMatch], role: str, ontology: OntologyConfig
) -> list[dict[str, Any]] | None:
    """The candidates as the role sees them, in the resolver's order; None if one leaks."""
    rows = [
        {
            "#": number,
            "name": match.legal_name,
            "jurisdiction": match.jurisdiction or "--",
            "type": match.entity_type,
            "status": match.status,
            "match": f"{match.match_type} {match.confidence:.2f}",
            "LEI": match.lei or "",
            "source": match.source or "",
            "id": match.entity_id,
        }
        for number, match in enumerate(mask_candidates(candidates, role, ontology), 1)
    ]
    text = repr(rows)
    if any(secret in text for secret in candidate_secrets(candidates, role, ontology)):
        return None
    return rows


def licence_notice(view: DecisionView) -> str | None:
    return LICENCE_NOTICE if "opensanctions" in view.memo.coverage.sources else None


# --- screening ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenResult:
    """What a screen came to, as the page shows it."""

    kind: Literal["decision", "pick", "failed", "error"]
    message: str = ""
    decision: Decision | None = None
    is_new: bool = False
    resolved_by: str = ""
    candidates: list[EntityMatch] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)


def screen(request: ScreenRequest, log: AuditLog, ontology: OntologyConfig) -> ScreenResult:
    """Run one screen as `ubo screen` does, and say what came of it in the CLI's words."""
    try:
        outcome = Supervisor(ontology, log).run(request)
    except UnknownSnapshotSet as exc:
        return ScreenResult("error", f"{exc} Run 'ubo ingest' first.")
    except GraphNotBuilt as exc:
        return ScreenResult("error", str(exc))
    except DeterminismViolation as exc:
        return ScreenResult("error", f"DETERMINISM VIOLATION: {exc}")
    except (UnknownRulePack, ScreenUsageError, AuditChainError, PermissionError) as exc:
        return ScreenResult("error", str(exc))
    if isinstance(outcome, NeedsDisambiguation):
        return ScreenResult(
            "pick",
            f"The query has {len(outcome.candidates)} candidates: choose one to screen.",
            candidates=outcome.candidates,
        )
    if isinstance(outcome, ScreenFailed):
        # The exception's text may quote a record; the role is shown its kind only.
        return ScreenResult(
            "failed",
            f"{outcome.recommendation} ({outcome.reason}): the screen failed in the"
            f" {outcome.agent} agent ({outcome.error_type}). No decision was recorded."
            " Treat the entity as not screened. The audit log holds the error.",
        )
    return ScreenResult(
        "decision",
        decision=outcome.decision,
        is_new=outcome.is_new,
        resolved_by=outcome.resolution.method,
        trace=outcome.trace,
    )


# --- the review ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WhatIfChoice:
    """The reviewer settles the contested matches; the rules give the outcome."""

    if_confirmed: str
    if_rejected: str

    @property
    def decisive(self) -> bool:
        return self.if_confirmed != self.if_rejected


def what_if_choice(decision: Decision) -> WhatIfChoice | None:
    if decision.recommendation != "REVIEW":
        return None
    what_if = Memo.model_validate(decision.memo_json).what_if
    if what_if is None:
        return None
    return WhatIfChoice(what_if.if_confirmed, what_if.if_rejected)


def _recommended(records: list[dict[str, Any]], decision_id: str) -> dict[str, Any] | None:
    return next(
        (
            r
            for r in records
            if r["decision_id"] == decision_id and r["event_type"] == "RECOMMENDED"
        ),
        None,
    )


def screened_by(records: list[dict[str, Any]], decision_id: str) -> str | None:
    """Who screened a decision: its maker, whom the checker may not be."""
    event = _recommended(records, decision_id)
    return event["actor"] if event else None


def review_problem(
    decision: Decision,
    action: Literal["approve", "override", "escalate"],
    *,
    actor: str,
    screener: str | None,
    outcome: Literal["CLEAR", "ESCALATE"] | None = None,
    reason: str | None = None,
    matches: MatchRuling | None = None,
    superseded: bool | None = None,
    accept_stale: bool = False,
) -> str | None:
    """Why the review cannot be made as it stands, or None: checked before the button is pressed.

    The same rules `review_decision` applies; it checks them again, under the log's lock.
    """
    if decision.status != "RECOMMENDED":
        return f"Decision {decision.decision_id} is already {decision.status}."
    if not actor.strip():
        return "Give an actor: a review is made by someone."
    if screener is not None and same_actor(screener, actor):
        return f"'{actor}' screened this decision and cannot also review it."
    try:
        _, settled, _ = settle(decision, action, outcome, reason, matches)
        check_stale(decision, settled, reason, superseded, accept_stale)
    except ReviewError as exc:
        return str(exc)
    return None


def clearable(decision: Decision) -> bool:
    return not (NOT_CLEARABLE & set(decision.reasons))


def do_review(
    log: AuditLog,
    ontology: OntologyConfig,
    decision: Decision,
    *,
    action: Literal["approve", "override", "escalate"],
    actor: str,
    role: str,
    outcome: Literal["CLEAR", "ESCALATE"] | None = None,
    reason: str | None = None,
    matches: MatchRuling | None = None,
    accept_stale: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Decision:
    """Review as `ubo review` does. Raises `ReviewError`, `PermissionError`, `AuditChainError`."""
    return review_decision(
        log,
        ontology,
        decision.decision_id,
        action=action,
        actor=actor,
        role=role,
        now=now,
        outcome=outcome,
        reason=reason,
        matches=matches,
        superseded=is_superseded(decision),
        accept_stale=accept_stale,
    )


# --- lists --------------------------------------------------------------------------------


def decision_row(
    decision: Decision, role: str, ontology: OntologyConfig, newer: bool | None = None
) -> dict[str, Any]:
    view = mask_decision(decision, role, ontology)
    row = {
        "decision": view.decision_id,
        "recommendation": view.recommendation,
        "status": view.status,
        "outcome": view.final_outcome or "",
        "subject": view.target_id or view.memo.query_norm or "",
        "reasons": ", ".join(view.reasons),
        "set": view.snapshot_set_id,
        "made": f"{view.created_at:%Y-%m-%d %H:%M}",
    }
    if newer is not None:
        row["newer data"] = "yes" if newer else ""
    return row


def queue_rows(
    waiting: list[Decision],
    role: str,
    ontology: OntologyConfig,
    superseded: Callable[[Decision], bool | None] = is_superseded,
    follows: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The review queue as `ubo review` lists it: gravest first, then oldest.

    `follows` maps a re-screen a monitor run made to the decision it follows.
    """
    newer: dict[str, bool | None] = {}
    rows = []
    for decision in waiting:
        # Superseded depends on the set alone: one database read per set.
        if decision.snapshot_set_id not in newer:
            newer[decision.snapshot_set_id] = superseded(decision)
        row = decision_row(decision, role, ontology, newer[decision.snapshot_set_id])
        if follows is not None:
            row["re-screen of"] = follows.get(decision.decision_id, "")
        rows.append(row)
    return rows


def latest_run(records: list[dict[str, Any]]) -> MonitorRun | None:
    """The newest monitor run on record."""
    runs = [r for r in records if r["event_type"] == change_monitor.RUN_EVENT]
    return MonitorRun.model_validate(runs[-1]["payload"]["run"]) if runs else None


def run_with_statuses(run: MonitorRun, log: AuditLog) -> MonitorRun:
    """The run with each item's status as the log stands now; as recorded when the
    database cannot be read (missing, or held by a writer)."""
    if not db_path().exists():
        return run
    try:
        con = connect(read_only=True)
    except duckdb.Error:
        return run
    try:
        return change_monitor.refresh_statuses(run, log, con)
    except (duckdb.Error, UnknownSnapshotSet):
        return run
    finally:
        con.close()


def rescreen_rows(
    run: MonitorRun, decisions: list[Decision], role: str, ontology: OntologyConfig
) -> list[dict[str, Any]]:
    """The re-screen queue as `ubo monitor` lists it, each subject as the role may see it."""
    by_id = {decision.decision_id: decision for decision in decisions}
    rows = []
    for number, item in enumerate(run.items, 1):
        prior = by_id.get(item.prior_decision_id)
        view = mask_decision(prior, role, ontology) if prior else None
        subject = (view.target_id or view.memo.query_norm) if view else item.query_norm
        rows.append(
            {
                "#": number,
                "subject": subject or "",
                "was": item.prior_recommendation
                + (f" / {item.prior_outcome}" if item.prior_outcome else ""),
                "status before": item.prior_status,
                "why": item.reason,
                "causes": ", ".join(item.causes),
                "found by": item.basis,
                "depth": item.max_depth,
                "prior decision": item.prior_decision_id,
                "queue": item.status,
            }
        )
    return rows


def rescreen_item(
    run: MonitorRun,
    prior_decision_id: str,
    log: AuditLog,
    ontology: OntologyConfig,
    role: str,
    actor: str,
) -> change_monitor.RescreenResult:
    """Screen one queued decision again on the run's set, as `ubo monitor --autorun` does.

    Raises `PermissionError` for a role that may not screen, and what the
    Supervisor raises for a request that cannot be run.
    """
    require_command(ontology, role, "screen")
    [item] = [i for i in run.items if i.prior_decision_id == prior_decision_id]
    one = run.model_copy(update={"items": [item]})
    con = connect(read_only=True)
    try:
        [result] = change_monitor.rescreen(
            one,
            Supervisor(ontology, log),
            con,
            actor=actor,
            role=role,
            requests=change_monitor.recorded_requests(log),
        )
    finally:
        con.close()
    return result


def unscreened_rows(failed: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Failed screens nobody ran again: when, and where it failed, never the error's text."""
    rows = []
    for record in failed:
        payload = record["payload"]
        last = (payload.get("traceback") or [""])[-1].strip()
        rows.append(
            {
                "failed screen": record["decision_id"],
                "when": record["created_at"],
                "agent": payload.get("agent", ""),
                "error type": last.split(":", 1)[0] if last else "",
                "snapshot set": payload.get("snapshot_set_id") or "",
            }
        )
    return rows


def history_rows(
    decisions: list[Decision], decision: Decision, role: str, ontology: OntologyConfig
) -> list[dict[str, Any]]:
    """The other decisions on the same entity: other sets, depths and packs."""
    if decision.target_id is None:
        return []
    return [
        decision_row(other, role, ontology)
        for other in decisions
        if other.target_id == decision.target_id and other.decision_id != decision.decision_id
    ]


def trace_rows(records: list[dict[str, Any]], decision_id: str) -> list[dict[str, Any]]:
    """How the decision was computed: per agent, its time and the queries it made."""
    event = _recommended(records, decision_id)
    if event is None:
        return []
    return [
        {
            "agent": step["agent"],
            "seconds": round(step["seconds"], 3),
            "queries": len(step["queries"]),
            "rows read": sum(call["rows"] for call in step["queries"]),
        }
        for step in event["payload"].get("trace", [])
    ]


# --- audit --------------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    report: ChainReport
    problems: list[str]

    @property
    def intact(self) -> bool:
        return not self.problems


def verify(
    log: AuditLog, ontology: OntologyConfig, role: str, expect_head: str | None = None
) -> VerifyResult:
    """`ubo audit verify`: the chain, what the events say, and a receipt kept elsewhere."""
    require_command(ontology, role, "audit-verify")
    report = log.verify(ontology)
    problems = []
    if not report.intact:
        problems.append(f"line {report.broken_line}: {report.problem}")
    elif expect_head and expect_head.strip() not in {r["hash"] for r in log.records()}:
        problems.append(f"the log does not hold the expected head {expect_head.strip()}")
    return VerifyResult(report, problems)


def replay(
    log: AuditLog, ontology: OntologyConfig, role: str, decision_id: str, deep: bool = False
) -> ReplayReport:
    """`ubo audit replay`. Raises `PermissionError`, `UnknownDecision`, `AuditChainError`."""
    require_command(ontology, role, "audit-replay")
    return replay_decision(log, ontology, decision_id, deep=deep)


# --- the evidence behind a node -------------------------------------------------------------


def real_id(decision: Decision, shown_id: str) -> str | None:
    """The entity a shown id stands for: itself, or the one a pseudonym was made from."""
    for entity in Memo.model_validate(decision.memo_json).entities:
        if shown_id in (entity.id, pseudonym_for(entity.id, entity.entity_type)):
            return entity.id
    return None


@dataclass(frozen=True)
class Evidence:
    claim: str
    citation: str
    provenance: Provenance | None


def node_evidence(decision: Decision, shown_id: str) -> list[Evidence]:
    """The claims about a node and what each cites, in the memo's order."""
    entity_id = real_id(decision, shown_id)
    if entity_id is None:
        return []
    memo = Memo.model_validate(decision.memo_json)
    found = []
    for claim in memo.claims:
        if claim.subject_id != entity_id:
            continue
        params = ", ".join(f"{k}={v}" for k, v in claim.params.items())
        label = f"{claim.code}" + (f" ({params})" if params else "")
        for citation in claim.citations or []:
            where = (
                f"{citation.provenance.source}, snapshot {citation.provenance.snapshot_id},"
                f" as of {citation.provenance.as_of}"
                if citation.provenance
                else f"dataset snapshot {citation.ref_id}"
            )
            found.append(Evidence(label, f"{citation.kind}: {where}", citation.provenance))
    return found


def source_record(
    role: str, ontology: OntologyConfig, decision: Decision, provenance: Provenance
) -> dict[str, Any] | None:
    """The published record behind a citation, as the role may see it, from the decision's set."""
    with GraphView.time_travel(role, decision.snapshot_set_id, ontology) as view:
        return view.get_source_record(provenance)


# --- next actions, as screens -----------------------------------------------------------------


@dataclass(frozen=True)
class FollowUp:
    label: str
    update: dict[str, Any]


def follow_ups(decision: Decision, role: str, ontology: OntologyConfig) -> list[FollowUp]:
    """The memo's next actions that one more screen settles, as the request for that screen.

    Each is an ordinary screen through the Supervisor: a new recorded decision
    that waits for a reviewer like any other.
    """
    memo = Memo.model_validate(decision.memo_json)
    hidden = ontology.roles[role].masked_fields
    labels = {e.id: e.entity_type for e in memo.entities}

    def shown(entity_id: str) -> str:
        label = labels.get(entity_id)
        return pseudonym_for(entity_id, label) if label in hidden else entity_id

    found: dict[str, FollowUp] = {}
    for action in memo.next_actions:
        if action.code == "RESCREEN_DEEPER" and decision.target_id:
            depth = int(action.params.get("max_depth") or decision.max_depth) + 2
            found[f"deeper:{depth}"] = FollowUp(
                f"Screen again {depth} hops deep (RESCREEN_DEEPER)",
                {"target_id": decision.target_id, "max_depth": depth, "query": "", "pick": None},
            )
        linked = action.params.get("linked_id")
        if action.code == "SCREEN_LINKED_RECORD" and isinstance(linked, str):
            found[f"linked:{linked}"] = FollowUp(
                f"Screen the linked record {shown(linked)} (SCREEN_LINKED_RECORD)",
                {"target_id": linked, "query": "", "pick": None, "max_depth": None},
            )
    return [found[key] for key in sorted(found)]


# --- what the codes mean --------------------------------------------------------------------


def glossary(memo: Memo) -> dict[str, str]:
    """Each reason and gap code in a sentence, with the rule pack's own thresholds."""
    pack = memo.rule_pack
    op = pack.threshold_operator
    return {
        "DIRECT_DESIGNATION": "The target itself is on a list the rule pack applies.",
        "DERIVED_50PCT": f"Blocked parties together own {op} {pack.ownership_threshold_pct} %"
        " of the target (OFAC 50 % rule), so it is blocked too.",
        "POSSIBLY_BLOCKED": "The target may be blocked: an owner without a stated share is"
        " presumed a majority owner, or a match is only in the review band.",
        "EFFECTIVE_EXPOSURE": f"Blocked parties hold ≥ {pack.review_exposure_pct} % of the"
        " target through all ownership paths combined.",
        "NEAR_MISS_AGGREGATE": f"Blocked owners hold ≥ {pack.near_miss_pct} % directly:"
        " close to the blocking threshold, not over it.",
        "DECISION_RELEVANT_GAP": "Data missing on an evidence path could change the answer.",
        "CONTROL_LINK": "A blocked or possibly blocked party controls an entity on the way to"
        " the target, without an ownership share.",
        "OTHER_REGIME_DESIGNATION": "A list the rule pack does not apply would catch the target.",
        "ENTITY_NOT_FOUND": "The query matched no entity: nothing was screened.",
        "EVIDENCE_LIMIT_EXCEEDED": "More evidence paths than the rule pack allows: narrow it.",
        "GUARDRAIL_FAILED": "The memo failed a check and is not to be trusted as it stands.",
        "PIPELINE_ERROR": "The screen failed: the entity was not screened.",
        "NULL_PCT_EDGE": "An ownership link states no percentage.",
        "REPORTING_EXCEPTION": "The entity declined to report its parent to GLEIF.",
        "LAPSED_LEI": "The LEI is lapsed, retired, merged, a duplicate or annulled.",
        "REVIEW_BAND_MATCH": f"A name matches a designation at {pack.review_band_low:.2f}–"
        f"{pack.match_threshold:.2f}: maybe the same entity.",
        "PCT_SUM_OVER_100": "The reported stakes in an entity add up to more than 100 %.",
        "DEPTH_LIMIT_REACHED": "The owners go on above the depth the screen walked.",
        "POSSIBLE_DUPLICATE": "A linked record of this entity has owners the screen did not see.",
    }
