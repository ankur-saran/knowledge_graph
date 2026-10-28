"""The review UI: the same permission matrix, review rules and masking as the CLI.

The panel logic is tested as code; the page itself runs in-process through
Streamlit's AppTest, which starts no server and opens no socket.
"""

import _socket
import socket
from pathlib import Path
from typing import get_args

import pytest
from e2e import person_pii, screen_gold
from streamlit.testing.v1 import AppTest

from ubo_sentinel.agents import change_monitor as cm
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.agents.traversal import TraversalAgent
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.review import unscreened
from ubo_sentinel.graph.views import MASK, mask_decision
from ubo_sentinel.models.evidence import GapCode, ReasonCode
from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.ui import panels

APP = str(Path(__file__).resolve().parents[2] / "src" / "ubo_sentinel" / "ui" / "app.py")


@pytest.fixture(autouse=True)
def loopback_socketpair(monkeypatch):
    """AppTest runs an asyncio loop, whose self-pipe on Windows is a socket pair joined
    over 127.0.0.1. That pair alone is let through; every other connect is refused."""
    pair = socket.socketpair

    def loopback_pair(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(socket.socket, "connect", _socket.socket.connect)
            return pair(*args, **kwargs)

    monkeypatch.setattr(socket, "socketpair", loopback_pair)


@pytest.fixture
def t0(fresh_db):
    (set_id,) = fresh_db("t0")
    return set_id


@pytest.fixture
def scenarios(gold):
    return {s.id: s for s in gold}


@pytest.fixture
def screened(t0, ontology, scenarios):
    """Screen a gold scenario as `a.analyst` into the test's log; returns the decision."""

    def screen(sid, **request):
        supervisor = Supervisor(ontology, AuditLog())
        return screen_gold(supervisor, scenarios[sid], t0, actor="a.analyst", **request).decision

    return screen


# --- the permission matrix -----------------------------------------------------------------


def test_each_role_sees_the_panels_of_its_commands(ontology):
    expected = {
        "analyst": ["screen"],
        "reviewer": ["screen", "review", "audit"],
        "auditor": ["audit"],
        "engineer": ["audit", "build"],
    }
    assert {role: panels.allowed_panels(ontology, role) for role in ontology.roles} == expected
    for role, shown in expected.items():
        commands = ontology.roles[role].commands
        for panel in shown:
            assert any(command in commands for command in panels.PANEL_COMMANDS[panel])
    assert [r for r in ontology.roles if panels.may_read_decisions(ontology, r)] == [
        "analyst",
        "reviewer",
        "auditor",
    ]
    assert panels.allowed_panels(ontology, "intern") == []


# --- the review rules, before the button is pressed ------------------------------------------


def problem(decision, action, actor="r.reviewer", **kwargs):
    return panels.review_problem(decision, action, actor=actor, screener="a.analyst", **kwargs)


def test_each_cell_of_the_outcome_table(screened):
    clear, escalate, review = screened("SCEN-01"), screened("SCEN-02"), screened("SCEN-05")
    assert (clear.recommendation, escalate.recommendation, review.recommendation) == (
        "CLEAR",
        "ESCALATE",
        "REVIEW",
    )
    for decision in (clear, escalate):
        assert problem(decision, "approve") is None
        assert "needs a reason" in problem(decision, "override")
        assert problem(decision, "override", reason="the list is wrong") is None
        assert problem(decision, "escalate") is None
    assert "needs an outcome" in problem(review, "approve", reason="checked")
    assert "needs a reason" in problem(review, "approve", outcome="CLEAR")
    assert problem(review, "approve", outcome="CLEAR", reason="checked") is None
    assert "none to override" in problem(review, "override", reason="x")
    assert problem(review, "escalate") is None
    assert "only when approving a REVIEW" in problem(clear, "approve", matches="confirm")


def test_maker_and_checker_are_two_people(screened):
    decision = screened("SCEN-01")
    assert "cannot also review" in problem(decision, "approve", actor=" A.Analyst")
    assert "Give an actor" in problem(decision, "approve", actor="  ")


def test_nothing_screened_cannot_be_cleared(screened):
    decision = screened("SCEN-23")
    assert decision.reasons == ["ENTITY_NOT_FOUND"] and not panels.clearable(decision)
    assert "cannot be cleared" in problem(decision, "approve", outcome="CLEAR", reason="r")
    assert problem(decision, "approve", outcome="ESCALATE", reason="r") is None


def test_a_stale_clear_needs_to_be_accepted_with_a_reason(screened):
    decision = screened("SCEN-01")
    assert "newer data exists" in problem(decision, "approve", superseded=True)
    assert "needs a reason" in problem(decision, "approve", superseded=True, accept_stale=True)
    stale_ok = problem(decision, "approve", superseded=True, accept_stale=True, reason="known")
    assert stale_ok is None
    assert problem(decision, "escalate", superseded=True) is None


def test_the_reviewer_settles_the_match_and_the_rules_give_the_outcome(screened):
    decision = screened("SCEN-14")
    choice = panels.what_if_choice(decision)
    assert (choice.if_confirmed, choice.if_rejected, choice.decisive) == ("ESCALATE", "CLEAR", True)
    assert problem(decision, "approve", matches="confirm", reason="same entity") is None
    contradicts = problem(decision, "approve", matches="confirm", outcome="CLEAR", reason="r")
    assert "the rules give ESCALATE" in contradicts
    assert panels.what_if_choice(screened("SCEN-01")) is None


def test_a_review_through_the_panel_is_the_cli_review(screened, ontology):
    decision = screened("SCEN-02")
    log = AuditLog()
    reviewed = panels.do_review(
        log, ontology, decision, action="approve", actor="r.reviewer", role="reviewer"
    )
    assert (reviewed.status, reviewed.final_outcome) == ("APPROVED", "ESCALATE")
    assert problem(reviewed, "approve") == f"Decision {decision.decision_id} is already APPROVED."
    with pytest.raises(PermissionError):
        panels.do_review(
            log, ontology, screened("SCEN-01"), action="approve", actor="x", role="analyst"
        )


# --- screening ----------------------------------------------------------------------------


def test_a_screen_says_what_the_cli_says(t0, ontology):
    log = AuditLog()
    picked = panels.screen(ScreenRequest(query="Silverline Commodities Ltd"), log, ontology)
    assert picked.kind == "pick" and len(picked.candidates) == 2
    done = panels.screen(ScreenRequest(query="Acme Trading FZE"), log, ontology)
    assert done.kind == "decision" and done.is_new and done.decision.recommendation == "CLEAR"
    again = panels.screen(ScreenRequest(query="acme trading fze"), log, ontology)
    assert not again.is_new and again.decision == done.decision
    trace = panels.trace_rows(log.records(), done.decision.decision_id)
    assert [row["agent"] for row in trace][:2] == ["Intake", "Traversal"]
    assert all(row["queries"] >= 0 and row["seconds"] >= 0 for row in trace)
    unknown = panels.screen(ScreenRequest(query="x", snapshot="nope"), log, ontology)
    assert unknown.kind == "error" and "ubo ingest" in unknown.message
    refused = panels.screen(ScreenRequest(query="x", role="auditor"), log, ontology)
    assert refused.kind == "error" and "not permitted" in refused.message


def test_a_failed_screen_shows_its_kind_and_never_its_text(t0, ontology, monkeypatch):
    def fail(self, state, ctx):
        raise RuntimeError("Secret Person Name")

    monkeypatch.setattr(TraversalAgent, "run", fail)
    log = AuditLog()
    failed = panels.screen(ScreenRequest(query="Acme Trading FZE"), log, ontology)
    assert failed.kind == "failed" and "RuntimeError" in failed.message
    assert "Secret" not in failed.message
    (row,) = panels.unscreened_rows(unscreened(log))
    assert (row["agent"], row["error type"]) == ("Traversal", "RuntimeError")
    assert "Secret" not in repr(row)


def test_masked_candidates_keep_their_order_and_hide_the_person(t0, ontology):
    found = panels.screen(ScreenRequest(query="Silverline Commodities Ltd"), AuditLog(), ontology)
    rows = panels.candidate_rows(found.candidates, "analyst", ontology)
    assert [row["id"] for row in rows] == [match.entity_id for match in found.candidates]
    person = found.candidates[0].model_copy(update={"entity_type": "Person"})
    (row,) = panels.candidate_rows([person], "analyst", ontology)
    assert row["name"] == MASK and row["id"].startswith("person:")
    assert person.legal_name in panels.candidate_secrets([person], "analyst", ontology)
    assert panels.candidate_rows([person], "reviewer", ontology)[0]["name"] == person.legal_name


def test_next_actions_become_screens(screened, ontology):
    deeper = screened("SCEN-22")
    (follow,) = panels.follow_ups(deeper, "analyst", ontology)
    assert follow.update["max_depth"] == 7 and follow.update["target_id"] == deeper.target_id
    request = ScreenRequest(query="", snapshot="fixtures", actor="a.analyst")
    result = panels.screen(request.model_copy(update=follow.update), AuditLog(), ontology)
    assert result.decision.recommendation == "ESCALATE"
    (linked,) = panels.follow_ups(screened("SCEN-27"), "analyst", ontology)
    assert linked.update["target_id"] == "fx_list:s27-dup"
    assert panels.follow_ups(screened("SCEN-01"), "analyst", ontology) == []


# --- lists, audit and evidence ------------------------------------------------------------


def test_the_queue_reads_the_database_once_per_set(screened, ontology):
    decisions = [screened(sid) for sid in ("SCEN-01", "SCEN-02", "SCEN-05")]
    calls = []
    rows = panels.queue_rows(decisions, "analyst", ontology, lambda d: calls.append(d) or False)
    assert len(calls) == 1 and [row["newer data"] for row in rows] == ["", "", ""]


def test_history_shows_the_other_decisions_on_the_entity(screened, ontology):
    shallow, deep = screened("SCEN-22"), screened("SCEN-22", max_depth=7)
    rows = panels.history_rows([shallow, deep], shallow, "analyst", ontology)
    assert [row["decision"] for row in rows] == [deep.decision_id]


def test_verify_and_replay_follow_the_matrix(screened, ontology):
    decision = screened("SCEN-01")
    log = AuditLog()
    head = log.records()[-1]["hash"]
    assert panels.verify(log, ontology, "engineer", head).intact
    assert "expected head" in panels.verify(log, ontology, "auditor", "f" * 64).problems[0]
    with pytest.raises(PermissionError):
        panels.verify(log, ontology, "analyst")
    assert panels.replay(log, ontology, "auditor", decision.decision_id).identical
    with pytest.raises(PermissionError):
        panels.replay(log, ontology, "engineer", decision.decision_id)


def test_the_evidence_behind_a_masked_node(screened, ontology):
    decision = screened("SCEN-03")
    view = mask_decision(decision, "analyst", ontology)
    person = next(e.id for e in view.memo.entities if e.id.startswith("person:"))
    found = panels.node_evidence(decision, person)
    assert found and all(item.claim for item in found)
    cited = next(item for item in found if item.provenance is not None)
    assert panels.source_record("analyst", ontology, decision, cited.provenance)["masked"]
    full = panels.source_record("reviewer", ontology, decision, cited.provenance)
    assert "masked" not in full
    assert panels.node_evidence(decision, "nobody") == []


def test_every_code_is_explained_with_the_packs_thresholds(screened, ontology):
    memo = mask_decision(screened("SCEN-05"), "analyst", ontology).memo
    words = panels.glossary(memo)
    assert set(get_args(ReasonCode)) | set(get_args(GapCode)) <= set(words)
    assert f"{memo.rule_pack.near_miss_pct}" in words["NEAR_MISS_AGGREGATE"]


def test_the_licence_is_named_when_the_set_holds_opensanctions(screened, ontology):
    view = mask_decision(screened("SCEN-01"), "analyst", ontology)
    assert panels.licence_notice(view) is None
    coverage = view.memo.coverage.model_copy(update={"sources": ["gleif", "opensanctions"]})
    listed = view.model_copy(update={"memo": view.memo.model_copy(update={"coverage": coverage})})
    assert "CC BY-NC 4.0" in panels.licence_notice(listed)


# --- the page ---------------------------------------------------------------------------


def page(role="analyst", actor=None):
    app = AppTest.from_file(APP, default_timeout=120)
    app.session_state["role"] = role
    app.session_state["actor"] = actor or role
    return app.run()


def screen_on(app, query):
    app.text_input(key="query").input(query)
    app.button(key="screen-submit").click()
    return app.run()


def shown_text(app):
    parts = [e.value for kind in ("markdown", "code", "caption", "info", "error", "warning")
             for e in getattr(app, kind)]  # fmt: skip
    parts += [frame.value.to_string() for frame in app.dataframe]
    parts += [str(item.value) for item in app.json]
    return "\n".join(map(str, parts))


def test_the_analyst_picks_a_candidate_and_a_rerun_records_nothing(t0):
    app = screen_on(page("analyst", "a.analyst"), "Silverline Commodities Ltd")
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["Screen"]
    assert "2 candidates" in app.info[0].value
    app.radio(key="pick").set_value(1)
    app.button(key="pick-submit").click()
    app.run()
    assert any(":green[CLEAR]" in m.value for m in app.markdown)
    assert "Audit head:" in shown_text(app)
    count = len(AuditLog().records())
    app.run()
    app.run()
    assert len(AuditLog().records()) == count == 1


def test_the_reviewer_is_held_to_maker_checker_then_reviews(screened):
    decision = screened("SCEN-02")
    app = page("reviewer", "a.analyst")
    assert [tab.label for tab in app.tabs] == ["Screen", "Review", "Audit"]
    assert app.selectbox(key="review-decision").value == decision.decision_id
    assert any("cannot also review" in w.value for w in app.warning)
    assert app.button(key="review-submit").disabled
    app.text_input(key="actor").input("r.reviewer")
    app.run()
    assert not app.button(key="review-submit").disabled
    app.button(key="review-submit").click()
    app.run()
    assert "APPROVED, outcome ESCALATE" in app.success[0].value
    assert AuditLog().decision(decision.decision_id).status == "APPROVED"


def test_a_review_without_a_reason_cannot_be_recorded(screened):
    screened("SCEN-05")
    app = page("reviewer", "r.reviewer")
    assert app.radio(key="outcome").options == ["ESCALATE", "CLEAR"]
    assert app.button(key="review-submit").disabled
    app.text_area(key="reason").input("shares checked with the registry")
    app.run()
    assert not app.button(key="review-submit").disabled


def test_the_auditor_replays_and_the_engineer_reads_metadata(screened):
    screened("SCEN-01")
    auditor = page("auditor")
    assert [tab.label for tab in auditor.tabs] == ["Audit"]
    auditor.button(key="replay").click()
    auditor.run()
    assert [s.value for s in auditor.success] == ["Replay identical"]
    auditor.button(key="verify").click()
    auditor.run()
    assert "Chain intact (1 records)" in [s.value for s in auditor.success]

    engineer = page("engineer")
    assert [tab.label for tab in engineer.tabs] == ["Audit", "Build info"]
    # Metadata only: no decision to choose or replay.
    assert [box.key for box in engineer.selectbox] == ["role"]
    assert "gold_digest" in shown_text(engineer)


def test_the_analyst_page_shows_no_person(t0, entities, scenarios):
    pii = person_pii(entities)
    for sid in ("SCEN-03", "SCEN-11"):
        app = screen_on(page("analyst", "a.analyst"), scenarios[sid].query)
        assert not app.exception
        text = shown_text(app)
        assert "BLOCKED" in text or "REVIEW" in text or "ESCALATE" in text
        assert not [value for value in pii if value in text], sid


def test_a_failed_screen_is_shown_by_kind_and_logged_once(t0, monkeypatch):
    def fail(self, state, ctx):
        raise RuntimeError("Secret Person Name")

    monkeypatch.setattr(TraversalAgent, "run", fail)
    app = screen_on(page("analyst", "a.analyst"), "Acme Trading FZE")
    assert "RuntimeError" in app.error[0].value and "Secret" not in shown_text(app)
    app.run()
    app.run()
    assert [r["event_type"] for r in AuditLog().records()] == ["SCREEN_FAILED"]


# --- the re-screen queue ------------------------------------------------------------------


@pytest.fixture
def monitored(fresh_db, ontology, scenarios):
    """SCEN-01, 03 and 17 screened on t0, the t1 list loaded, and a monitor run recorded."""
    t0, t1 = fresh_db("t0_t1")
    log = AuditLog()
    supervisor = Supervisor(ontology, log)
    prior = {
        sid: screen_gold(supervisor, scenarios[sid], t0, actor="a.analyst").decision
        for sid in ("SCEN-01", "SCEN-03", "SCEN-17")
    }
    con = connect(read_only=True)
    run, _, _ = cm.build_queue(log, con, resolve_set(con, t1))
    con.close()
    cm.record_run(log, ontology, run, actor="e", role="engineer", now=supervisor.now)
    return prior, run


def test_the_rescreen_queue_is_the_latest_run_with_its_statuses(monitored, ontology):
    prior, run = monitored
    log = AuditLog()
    assert panels.latest_run(log.records()) == run
    rows = panels.rescreen_rows(run, log.decisions(), "reviewer", ontology)
    assert {row["prior decision"] for row in rows} == {
        prior["SCEN-01"].decision_id,
        prior["SCEN-17"].decision_id,
    }
    assert {row["queue"] for row in rows} == {"PENDING"}
    with pytest.raises(PermissionError):
        panels.rescreen_item(run, rows[0]["prior decision"], log, ontology, "engineer", "e")
    result = panels.rescreen_item(run, rows[0]["prior decision"], log, ontology, "reviewer", "r")
    assert result.decision.status == "RECOMMENDED"
    statuses = [item.status for item in panels.run_with_statuses(run, AuditLog()).items]
    assert sorted(statuses) == ["DONE", "PENDING"]
    follows = {result.decision.decision_id: rows[0]["prior decision"]}
    waiting = [d for d in AuditLog().decisions() if d.status == "RECOMMENDED"]
    shown = panels.queue_rows(waiting, "reviewer", ontology, lambda d: None, follows)
    assert [r["re-screen of"] for r in shown if r["re-screen of"]] == [rows[0]["prior decision"]]


def test_the_reviewer_rescreens_from_the_review_panel(monitored):
    prior, run = monitored
    app = page("reviewer", "r.reviewer")
    assert not app.exception
    assert f"Re-screen queue (2), monitor run {run.run_id}" in shown_text(app)
    count = len(AuditLog().records())
    app.button(key="rescreen-submit").click()
    app.run()
    assert not app.exception
    assert "Re-screened: decision" in app.success[0].value
    assert len(AuditLog().records()) == count + 1
