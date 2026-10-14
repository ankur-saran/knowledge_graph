"""RECOMMENDED → APPROVED | OVERRIDDEN | ESCALATED, through the command, by every role."""

import pytest
from e2e import cli, decision_id_of, start_cli

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog

pytestmark = pytest.mark.integration

ROLES = ["analyst", "reviewer", "auditor", "engineer"]
# The arguments that make each action a valid one on a CLEAR recommendation.
ACTIONS = {
    "approve": [],
    "override": ["--reason", "Owner listed last week."],
    "escalate": [],
}
CLEAR = "Acme Trading FZE"


@pytest.fixture
def recommended(fresh_db, ontology):
    """A CLEAR decision screened by `a.analyst`. Returns its id."""
    fresh_db("t0")
    request = ScreenRequest(query=CLEAR, actor="a.analyst")
    return Supervisor(ontology, AuditLog()).run(request).decision.decision_id


def review(decision_id, role, action, *more, actor="r.reviewer"):
    return cli("review", decision_id, "--role", role, "--actor", actor, "--action", action, *more)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("role", ROLES)
def test_only_a_reviewer_moves_a_recommendation(recommended, role, action):
    log = AuditLog()
    before = log.path.read_bytes()
    result = review(recommended, role, action, *ACTIONS[action])
    if role == "reviewer":
        assert result.exit_code == 0, result.output
        assert [e["event_type"] for e in log.records()][-1] == {
            "approve": "APPROVED",
            "override": "OVERRIDDEN",
            "escalate": "ESCALATED",
        }[action]
    else:
        assert result.exit_code == 1 and "not permitted" in result.output
        assert log.path.read_bytes() == before


@pytest.mark.parametrize("first", ACTIONS)
def test_a_reviewed_decision_is_reviewed_once(recommended, first):
    assert review(recommended, "reviewer", first, *ACTIONS[first]).exit_code == 0
    log = AuditLog()
    before = log.path.read_bytes()
    for action in ACTIONS:
        again = review(recommended, "reviewer", action, *ACTIONS[action], actor="another")
        assert again.exit_code == 2 and "already" in again.output
    assert log.path.read_bytes() == before


def test_what_a_review_needs(recommended):
    log = AuditLog()
    before = log.path.read_bytes()
    no_reason = review(recommended, "reviewer", "override")
    assert no_reason.exit_code == 2 and "needs a reason" in no_reason.output
    own = review(recommended, "reviewer", "approve", actor="a.analyst")
    assert own.exit_code == 2 and "cannot also review" in own.output
    assert log.path.read_bytes() == before


@pytest.mark.slow
def test_two_reviewers_at_once_make_one_review(recommended):
    """Two processes, one decision: the check under the log's lock lets one through."""
    children = [
        start_cli(
            "review", recommended, "--role", "reviewer", "--actor", who, "--action", "escalate"
        )
        for who in ("r.one", "r.two")
    ]
    results = [(child.wait(timeout=120), child.stdout.read()) for child in children]
    codes = sorted(code for code, _ in results)
    assert codes == [0, 2], results
    log = AuditLog()
    assert log.verify().intact
    assert [e["event_type"] for e in log.records()] == ["RECOMMENDED", "ESCALATED"]


@pytest.mark.slow
def test_screens_in_several_processes_keep_one_chain(fresh_db):
    fresh_db("t0")
    queries = ["Acme Trading FZE", "Vostrek Maritime LLC", "Halcyon Ridge Minerals Ltd"]
    children = [start_cli("screen", query, "--actor", f"a.{n}") for n, query in enumerate(queries)]
    outputs = [(child.wait(timeout=120), child.stdout.read()) for child in children]
    assert all(code == 0 for code, _ in outputs), outputs
    log = AuditLog()
    report = log.verify()
    assert (report.intact, report.count) == (True, 3)
    assert {decision_id_of(output) for _, output in outputs} == {
        d.decision_id for d in log.decisions()
    }


def test_a_decision_on_older_data_is_not_cleared_unawares(recommended, ontology):
    """After the t1 list is loaded, SCEN-01's t0 CLEAR is ESCALATE on the newer data."""
    assert cli("ingest", "--source", "fixtures", "--path", "fixtures/snapshot_t1").exit_code == 0
    shown = cli("review", recommended, "--role", "reviewer")
    assert shown.exit_code == 0
    refused = review(recommended, "reviewer", "approve")
    assert refused.exit_code == 2 and "newer data exists" in refused.output
    assert cli("normalize").exit_code == 0 and cli("build-graph").exit_code == 0
    newer = cli("screen", CLEAR, "--snapshot", "latest", "--actor", "a.analyst")
    assert ": ESCALATE  (" in newer.output
    # The reviewer is shown the decision on the newer data beside the old one.
    shown = cli("review", recommended, "--role", "reviewer")
    assert "Other decisions on this entity (1):" in shown.output
    assert decision_id_of(newer.output) in shown.output

    done = review(recommended, "reviewer", "approve", "--accept-stale")
    assert done.exit_code == 2 and "needs a reason" in done.output
    done = review(
        recommended, "reviewer", "approve", "--accept-stale", "--reason", "Owner checked by hand."
    )
    assert done.exit_code == 0, done.output
    payload = AuditLog().records()[-1]["payload"]
    assert (payload["superseded"], payload["accepted_stale"], payload["outcome"]) == (
        True,
        True,
        "CLEAR",
    )
