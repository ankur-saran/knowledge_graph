"""Replay: a recorded decision screened again, and each way it can fail to be the same."""

import json
from datetime import UTC, datetime

import pytest
from e2e import cli as run

from ubo_sentinel.agents import intake
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.explainer import ExplainerAgent
from ubo_sentinel.agents.supervisor import PIPELINE, Supervisor
from ubo_sentinel.agents.traversal import TraversalAgent
from ubo_sentinel.audit import replay as replay_module
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.replay import UnknownDecision, replay_all, replay_decision
from ubo_sentinel.cli.audit_cmd import EXIT_NOT_REPLAYABLE
from ubo_sentinel.rules import pack as pack_module

NOW = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
ESCALATE = "Halcyon Ridge Minerals Ltd"
HOMONYM = "Silverline Commodities Ltd"


@pytest.fixture
def log():
    return AuditLog()


@pytest.fixture
def screened(con, graph_t0, ontology, log):
    def run(query="", **request):
        supervisor = Supervisor(ontology, log, now=lambda: NOW)
        return supervisor.run(ScreenRequest(query=query, snapshot=graph_t0, **request), con)

    return run


@pytest.fixture
def replay(con, ontology, log):
    def run(outcome, **kwargs):
        return replay_decision(log, ontology, outcome.decision.decision_id, con, **kwargs)

    return run


def swapped(old, new):
    """A supervisor whose agents are not the ones that made the decision."""
    agents = tuple(new if agent is old else agent for agent in PIPELINE)
    return lambda ontology: Supervisor(ontology, agents=agents)


def test_a_replay_is_identical_however_the_target_was_reached(screened, replay, log, ontology, con):
    outcomes = [
        screened("Acme Trading FZE"),
        screened(ESCALATE),
        screened(HOMONYM, pick=2),
        screened("anything", lei="FXS01TARGET000000000", max_depth=3),
        screened(target_id="fx_list:s27-dup"),
        screened("Zzyzx Nonexistent Ventures"),
        screened(lei="NOSUCHLEI00000000000"),
    ]
    before = log.path.read_bytes()
    for outcome in outcomes:
        report = replay(outcome)
        assert (report.verdict, report.differing_fields) == ("IDENTICAL", []), report
        assert report.replayed_payload_sha256 == report.recorded_payload_sha256
        assert (report.evidence_source, report.recomputed) == ("event", False)
    assert replay(outcomes[1], deep=True).recomputed is True
    # A replay writes nothing.
    assert [r.verdict for r in replay_all(log, ontology, con)] == ["IDENTICAL"] * len(outcomes)
    assert log.path.read_bytes() == before


def test_two_unknown_leis_are_two_decisions(screened, log):
    """Screened without a name, each is on record: neither stands for the other."""
    one, two = screened(lei="NOSUCHLEI00000000000"), screened(lei="OTHERLEI000000000000")
    assert one.is_new and two.is_new
    assert one.decision.decision_id != two.decision.decision_id
    assert one.decision.memo_json["query_norm"] == "lei=nosuchlei00000000000"
    assert log.verify().count == 2
    assert not screened(lei=" nosuchlei00000000000 ").is_new


def test_the_event_holds_the_digests_of_what_was_read(screened, log, con, graph_t0):
    screened("Acme Trading FZE")
    evidence = log.records()[0]["payload"]["evidence"]
    assert len(evidence["silver_digest"]) == 64
    registry = dict(
        con.execute(
            "SELECT s.dataset, b.rows_digest FROM snapshot_sets s JOIN bronze_snapshots b"
            " USING (snapshot_id) WHERE s.snapshot_set_id = ?",
            [graph_t0],
        ).fetchall()
    )
    assert {name: item["rows_digest"] for name, item in evidence["snapshots"].items()} == registry
    assert len(registry) == 4


def test_a_decision_made_by_other_code_is_not_replayable_here(screened, replay, monkeypatch):
    outcome = screened("Acme Trading FZE")
    with monkeypatch.context() as patch:
        patch.setattr(replay_module, "__version__", "9.9.9")
        report = replay(outcome)
        assert report.verdict == "NOT_REPLAYABLE" and "engine 0.1.0; this is 9.9.9" in report.detail
    with monkeypatch.context() as patch:
        patch.setattr(replay_module, "PIPELINE_VERSION", 99)
        assert "pipeline version" in replay(outcome).detail
    with monkeypatch.context() as patch:
        patch.setattr(pack_module, "RULES_VERSION", 99)
        report = replay(outcome)
        assert report.verdict == "NOT_REPLAYABLE" and "rule engine has changed" in report.detail
    assert replay(outcome).identical


def test_the_recorded_pack_is_replayed_and_not_the_file(screened, replay, con, ontology, log):
    """A decision made with another pack replays with that pack's values."""
    pack = pack_module.load_rule_pack(pack_module.pack_path("ofac")).model_copy(
        update={"max_paths": 1}
    )
    supervisor = Supervisor(ontology, log, now=lambda: NOW)
    outcome = supervisor.run(ScreenRequest(query=ESCALATE, snapshot="fixtures"), con, pack)
    assert outcome.decision.reasons == ["EVIDENCE_LIMIT_EXCEEDED"]
    assert replay(outcome).identical


def test_changed_source_rows_are_found_from_the_rows(screened, replay, con):
    outcome = screened(ESCALATE)
    # A Bronze row changed behind its registry: Silver and Gold still give the decision.
    con.execute("UPDATE bronze_sanctions SET _row_hash = 'changed' WHERE _row_num = 1")
    assert replay(outcome).identical
    report = replay(outcome, deep=True)
    assert report.verdict == "SOURCE_DATA_CHANGED" and "'sanctions'" in report.detail

    # The registry made to agree with the rows: the event's digest still does not.
    con.execute("UPDATE bronze_snapshots SET rows_digest = 'changed' WHERE dataset = 'sanctions'")
    report = replay(outcome)
    assert report.verdict == "SOURCE_DATA_CHANGED" and "registry" in report.detail


def test_a_changed_graph_is_told_from_changed_code(screened, replay, con, graph_t0, monkeypatch):
    outcome = screened(ESCALATE)
    # Code that gives another memo without a new PIPELINE_VERSION: the data is as it was.
    with monkeypatch.context() as patch:

        class OtherDisclaimer(ExplainerAgent):
            def run(self, state, ctx):
                state = super().run(state, ctx)
                return state.with_(memo=state.memo.model_copy(update={"disclaimer": "Changed."}))

        patch.setattr(replay_module, "Supervisor", swapped(ExplainerAgent, OtherDisclaimer))
        report = replay(outcome)
        assert (report.verdict, report.differing_fields) == ("PAYLOAD_DIFFERS", ["memo_json"])
        assert report.recomputed and report.replayed_payload_sha256

    # A designation removed from Gold behind the registry: the replay differs,
    # and the rows say that it was the graph.
    con.execute("DELETE FROM graph_sanctions WHERE snapshot_set_id = ?", [graph_t0])
    report = replay(outcome)
    assert (report.verdict, report.detail) == ("GRAPH_CHANGED", "The Gold rows have changed.")
    assert "recommendation" in report.differing_fields

    con.execute("UPDATE gold_builds SET gold_digest = 'changed'")
    assert replay(outcome).detail.startswith("The graph was rebuilt")


def test_a_query_that_resolves_differently_is_reported_as_that(screened, replay, monkeypatch):
    outcome = screened(HOMONYM, pick=1)
    found = intake.queries.resolve_entity
    monkeypatch.setattr(
        intake.queries, "resolve_entity", lambda *args, **kwargs: found(*args, **kwargs)[::-1]
    )
    assert replay(outcome).verdict == "RESOLUTION_DIFFERS"


def test_a_replay_that_fails_is_not_identical(screened, replay, monkeypatch):
    outcome = screened("Acme Trading FZE")

    class Broken(TraversalAgent):
        def run(self, state, ctx):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(replay_module, "Supervisor", swapped(TraversalAgent, Broken))
    report = replay(outcome)
    assert (report.verdict, report.identical) == ("REPLAY_FAILED", False)
    assert "disk on fire" in report.detail


def test_an_event_without_digests_is_compared_with_the_registry(con, graph_t0, ontology, log):
    """Events written before Step 8 hold no `evidence`."""
    request = ScreenRequest(query="Acme Trading FZE", snapshot=graph_t0)
    outcome = Supervisor(ontology).run(request, con)
    decision = outcome.decision
    payload = {
        "decision": decision.model_dump(mode="json"),
        "rule_pack": pack_module.load_rule_pack(pack_module.pack_path("ofac")).model_dump(
            mode="json"
        ),
        "request": request.model_dump(mode="json"),
        "resolution": outcome.resolution.model_dump(mode="json"),
    }
    fields = {"ontology": ontology, "decision_id": decision.decision_id, "actor": "analyst"}
    fields |= {"event_type": "RECOMMENDED", "role": "analyst", "created_at": NOW}
    log.append(payload=payload, **fields)
    report = replay_decision(log, ontology, decision.decision_id, con)
    assert (report.verdict, report.evidence_source) == ("IDENTICAL", "registry")

    with pytest.raises(UnknownDecision):
        replay_decision(log, ontology, "0" * 16, con)


# --- the commands ----------------------------------------------------------------------


@pytest.fixture
def built():
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert run(*command).exit_code == 0


def test_audit_replay_prints_the_verdict_and_exits_by_it(built, monkeypatch):
    screened = run("screen", ESCALATE)
    decision_id = screened.output.split()[1].rstrip(":")

    result = run("audit", "replay", decision_id)
    assert result.exit_code == 0, result.output
    assert f"Replay identical: decision {decision_id}" in result.output
    assert run("audit", "replay", decision_id, "--deep", "--role", "reviewer").exit_code == 0
    as_json = json.loads(run("audit", "replay", decision_id, "--json").output)
    assert (as_json["verdict"], as_json["decision_id"]) == ("IDENTICAL", decision_id)

    for role in ("analyst", "engineer"):
        assert run("audit", "replay", decision_id, "--role", role).exit_code == 1
    assert run("audit", "replay", "0" * 16).exit_code == 2
    assert AuditLog().verify().count == 1

    monkeypatch.setattr(replay_module, "PIPELINE_VERSION", 99)
    refused = run("audit", "replay", decision_id)
    assert refused.exit_code == EXIT_NOT_REPLAYABLE
    assert "Replay NOT_REPLAYABLE" in refused.output and "pipeline version" in refused.output


def test_audit_replay_stops_at_a_log_that_does_not_verify(built):
    decision_id = run("screen", ESCALATE).output.split()[1].rstrip(":")
    log = AuditLog()
    log.path.write_text(log.path.read_text(encoding="utf-8").replace("ESCALATE", "CLEAR"), "utf-8")
    broken = run("audit", "replay", decision_id)
    assert broken.exit_code == 1 and "line 1" in broken.output
