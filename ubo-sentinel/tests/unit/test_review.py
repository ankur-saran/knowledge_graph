"""Human review: what a reviewer may make of a recommendation, and who may."""

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import PIPELINE, Supervisor
from ubo_sentinel.agents.traversal import TraversalAgent
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.review import ReviewError, pending, review_decision, unscreened
from ubo_sentinel.cli.app import app
from ubo_sentinel.models.canonical import sha256_hex

NOW = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)

# One query per recommendation; see eval/gold_scenarios.yaml.
CLEAR = "Acme Trading FZE"
ESCALATE = "Halcyon Ridge Minerals Ltd"
# SCEN-14: a name in the review band; confirmed it is ESCALATE, rejected CLEAR.
UNSETTLED = "Fairweather Bulk Carriers Ltd"
# A REVIEW with no unsettled match: the traversal stopped at the depth limit.
TRUNCATED = "Kittiwake Packaging Ltd"
NOT_FOUND = "Zzyzx Nonexistent Ventures"


@pytest.fixture
def log():
    return AuditLog()


@pytest.fixture
def screened(con, graph_t0, ontology, log):
    """Screen a query as an analyst and return the recorded decision."""

    def run(query, **request):
        supervisor = Supervisor(ontology, log, now=lambda: NOW)
        request = {"snapshot": graph_t0, "actor": "a.analyst"} | request
        return supervisor.run(ScreenRequest(query=query, **request), con).decision

    return run


@pytest.fixture
def review(log, ontology):
    def run(decision, action, **fields):
        fields = {"actor": "r.reviewer", "role": "reviewer", "now": lambda: LATER} | fields
        return review_decision(log, ontology, decision.decision_id, action=action, **fields)

    return run


@pytest.mark.parametrize(
    ("query", "action", "fields", "status", "outcome"),
    [
        (CLEAR, "approve", {}, "APPROVED", "CLEAR"),
        (CLEAR, "override", {"reason": "Owner listed last week."}, "OVERRIDDEN", "ESCALATE"),
        (CLEAR, "escalate", {}, "ESCALATED", "ESCALATE"),
        (ESCALATE, "approve", {}, "APPROVED", "ESCALATE"),
        (ESCALATE, "override", {"reason": "Stake sold in August."}, "OVERRIDDEN", "CLEAR"),
        (ESCALATE, "escalate", {"reason": "For the MLRO."}, "ESCALATED", "ESCALATE"),
        (TRUNCATED, "approve", {"outcome": "CLEAR", "reason": "Filing seen."}, "APPROVED", "CLEAR"),
        (TRUNCATED, "approve", {"outcome": "ESCALATE", "reason": "x"}, "APPROVED", "ESCALATE"),
        (TRUNCATED, "escalate", {}, "ESCALATED", "ESCALATE"),
    ],
)
def test_a_review_ends_in_an_outcome(screened, review, log, query, action, fields, status, outcome):
    decision = screened(query)
    reviewed = review(decision, action, **fields)
    assert (reviewed.status, reviewed.final_outcome) == (status, outcome)
    assert (reviewed.reviewed_by, reviewed.reviewed_at) == ("r.reviewer", LATER)
    assert reviewed.review_reason == fields.get("reason")
    # A review changes who decided, never what was recommended.
    assert reviewed.recommendation == decision.recommendation
    assert reviewed.canonical_payload() == decision.canonical_payload()

    event = log.records()[-1]
    assert (event["event_type"], event["actor"], event["role"]) == (
        status,
        "r.reviewer",
        "reviewer",
    )
    # The review is bound to the payload that was reviewed.
    assert event["payload"] == {
        "outcome": outcome,
        "recommendation": decision.recommendation,
        "payload_sha256": sha256_hex(decision.canonical_payload()),
        "basis": None,
        "superseded": None,
    }
    assert log.verify().intact


@pytest.mark.parametrize(
    ("query", "action", "fields", "message"),
    [
        (CLEAR, "approve", {"outcome": "ESCALATE"}, "override it instead"),
        (CLEAR, "override", {}, "needs a reason"),
        (CLEAR, "override", {"reason": "   "}, "needs a reason"),
        (CLEAR, "override", {"reason": "x", "outcome": "CLEAR"}, "approve it to keep it"),
        (ESCALATE, "escalate", {"outcome": "CLEAR"}, "not cleared"),
        # A REVIEW recommends nothing: there is nothing to agree with or to override.
        (TRUNCATED, "approve", {"reason": "x"}, "needs an outcome"),
        (TRUNCATED, "approve", {"outcome": "CLEAR"}, "needs a reason"),
        (TRUNCATED, "override", {"reason": "x", "outcome": "CLEAR"}, "none to override"),
        (TRUNCATED, "approve", {"outcome": "CLEAR", "reason": "x", "matches": "reject"}, "no uns"),
        (CLEAR, "approve", {"matches": "confirm"}, "only when approving a REVIEW"),
    ],
)
def test_a_review_the_recommendation_does_not_allow(
    screened, review, log, query, action, fields, message
):
    decision = screened(query)
    with pytest.raises(ReviewError, match=message):
        review(decision, action, **fields)
    assert log.decision(decision.decision_id).status == "RECOMMENDED"
    assert log.verify().count == 1


def test_what_was_not_screened_cannot_be_cleared(screened, review):
    decision = screened(NOT_FOUND)
    assert decision.reasons == ["ENTITY_NOT_FOUND"]
    with pytest.raises(ReviewError, match="ENTITY_NOT_FOUND cannot be cleared.*CHECK_QUERY"):
        review(decision, "approve", outcome="CLEAR", reason="Typo, surely.")
    reviewed = review(decision, "approve", outcome="ESCALATE", reason="Nothing known of it.")
    assert (reviewed.status, reviewed.final_outcome) == ("APPROVED", "ESCALATE")


def test_the_reviewer_settles_the_match_and_the_rules_give_the_outcome(screened, review, log):
    confirmed = review(screened(UNSETTLED), "approve", matches="confirm", reason="Same DOB.")
    assert (confirmed.status, confirmed.final_outcome) == ("APPROVED", "ESCALATE")
    assert log.records()[-1]["payload"]["basis"] == "MATCHES_CONFIRMED"

    # The same entity, screened deeper, is another decision.
    other = screened(UNSETTLED, max_depth=6)
    with pytest.raises(ReviewError, match="the rules give CLEAR"):
        review(other, "approve", matches="reject", outcome="ESCALATE", reason="x")
    rejected = review(other, "approve", matches="reject", reason="Another person.")
    assert rejected.final_outcome == "CLEAR"
    assert log.records()[-1]["payload"]["basis"] == "MATCHES_REJECTED"


def test_only_a_reviewer_reviews_and_only_once(screened, review, log, ontology):
    decision = screened(CLEAR)
    for role in ("analyst", "engineer"):
        with pytest.raises(PermissionError, match="may not mark"):
            review(decision, "approve", role=role)
    with pytest.raises(PermissionError, match="read-only"):
        review(decision, "approve", role="auditor")
    # Whoever made the recommendation does not also approve it.
    with pytest.raises(ReviewError, match="cannot also review"):
        review(decision, "approve", actor="a.analyst")
    with pytest.raises(ReviewError, match="No decision"):
        review_decision(
            log, ontology, "0" * 16, action="approve", actor="r", role="reviewer", now=lambda: NOW
        )
    assert log.verify().count == 1

    review(decision, "approve")
    with pytest.raises(ReviewError, match="already APPROVED .by r.reviewer"):
        review(decision, "escalate", actor="another.reviewer")
    # A second log object on the same file sees the review too: the check is under the lock.
    with pytest.raises(ReviewError, match="already APPROVED"):
        review_decision(
            AuditLog(),
            ontology,
            decision.decision_id,
            action="escalate",
            actor="b",
            role="reviewer",
            now=lambda: NOW,
        )
    assert [event["event_type"] for event in log.records()] == ["RECOMMENDED", "APPROVED"]


def test_screening_again_returns_the_reviewed_decision(screened, review, log):
    decision = screened(CLEAR)
    review(decision, "approve")
    again = screened(CLEAR)
    assert (again.status, again.final_outcome) == ("APPROVED", "CLEAR")
    assert log.verify().count == 2


def test_the_queue_is_the_gravest_first_and_shows_what_was_never_screened(
    screened, review, log, con, graph_t0, ontology
):
    clear, escalate, unsettled = screened(CLEAR), screened(ESCALATE), screened(UNSETTLED)
    assert [d.decision_id for d in pending(log)] == [
        escalate.decision_id,
        unsettled.decision_id,
        clear.decision_id,
    ]
    review(escalate, "approve")
    assert escalate.decision_id not in [d.decision_id for d in pending(log)]
    assert unscreened(log) == []

    class Broken(TraversalAgent):
        def run(self, state, ctx):
            raise RuntimeError("disk on fire")

    agents = tuple(Broken if agent is TraversalAgent else agent for agent in PIPELINE)
    request = ScreenRequest(query=TRUNCATED, snapshot=graph_t0)
    Supervisor(ontology, log, agents=agents).run(request, con)
    assert [record["note"] for record in unscreened(log)] == ["RuntimeError: disk on fire"]
    # Run again and it is a decision: no longer unscreened.
    Supervisor(ontology, log).run(request, con)
    assert unscreened(log) == []


# --- the command -----------------------------------------------------------------------

runner = CliRunner()


def run(*args):
    return runner.invoke(app, [str(arg) for arg in args])


@pytest.fixture
def built():
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert run(*command).exit_code == 0


def decision_id_of(output):
    return output.split()[1].rstrip(":")


def test_review_shows_a_decision_and_records_what_the_reviewer_decides(built):
    screened = run("screen", ESCALATE, "--actor", "a.analyst")
    decision_id = decision_id_of(screened.output)

    # `--role` has no default: a review is never made by accident.
    assert run("review", decision_id).exit_code == 2
    assert run("review", decision_id, "--role", "analyst").exit_code == 1

    queue = run("review", "--role", "reviewer")
    assert queue.exit_code == 0 and "1 decisions wait for a review." in queue.output
    assert f"{decision_id}  ESCALATE" in queue.output

    shown = run("review", decision_id, "--role", "reviewer")
    assert shown.exit_code == 0
    # A reviewer sees the persons an analyst does not.
    assert "Dmitri Volkanov" in shown.output and "(status RECOMMENDED)" in shown.output
    assert AuditLog().verify().count == 1

    act = ["review", decision_id, "--role", "reviewer", "--action", "override"]
    assert run(*act, "--reason", "Sold.").exit_code == 2  # no --actor
    assert run(*act, "--actor", "a.analyst", "--reason", "Sold.").exit_code == 2
    assert run(*act, "--actor", "r.reviewer").exit_code == 2  # no reason
    done = run(*act, "--actor", "r.reviewer", "--reason", "Stake sold in August.")
    assert done.exit_code == 0, done.output
    assert f"Decision {decision_id}: OVERRIDDEN, outcome CLEAR" in done.output
    assert "Audit head: " in done.output and "(#2)" in done.output

    again = run(*act, "--actor", "r.reviewer", "--reason", "Again.")
    assert again.exit_code == 2 and "already OVERRIDDEN" in again.output
    assert "Reviewed: OVERRIDDEN, outcome CLEAR, by r.reviewer" in again.output
    assert "0 decisions wait" in run("review", "--role", "reviewer").output
    assert run("review", "0" * 16, "--role", "reviewer").exit_code == 2


def test_review_needs_no_graph_and_settles_a_match(built, monkeypatch):
    decision_id = decision_id_of(run("screen", UNSETTLED).output)
    # The database is gone: a review reads the log only.
    monkeypatch.setenv("UBO_DB", "nowhere/ubo.duckdb")
    base = ["review", decision_id, "--role", "reviewer", "--actor", "r.reviewer"]
    both = run(*base, "--action", "approve", "--confirm-matches", "--reject-matches")
    assert both.exit_code == 2
    done = run(*base, "--action", "approve", "--confirm-matches", "--reason", "Same person.")
    assert done.exit_code == 0, done.output
    assert "APPROVED, outcome ESCALATE" in done.output
    assert AuditLog().records()[-1]["payload"]["superseded"] is None


def test_a_review_records_that_newer_data_existed(built):
    decision_id = decision_id_of(run("screen", CLEAR, "--actor", "a.analyst").output)
    assert run("ingest", "--source", "fixtures", "--path", "fixtures/snapshot_t1").exit_code == 0
    assert "[newer data exists]" in run("review", "--role", "reviewer").output
    done = run("review", decision_id, "--role", "reviewer", "--actor", "r", "--action", "approve")
    assert done.exit_code == 0, done.output
    assert AuditLog().records()[-1]["payload"]["superseded"] is True
