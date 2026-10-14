"""Every gold scenario screened into one audit log, and what the log then holds."""

import json

import pytest
from conftest import PACK
from e2e import screen_gold

from ubo_sentinel.agents.explainer import ExplainerAgent
from ubo_sentinel.agents.guardrail import GuardrailAgent
from ubo_sentinel.agents.intake import IntakeAgent
from ubo_sentinel.agents.rule_engine import RuleEngineAgent
from ubo_sentinel.agents.sanctions_match import SanctionsMatchAgent
from ubo_sentinel.agents.supervisor import PIPELINE, Recommended, ScreenFailed, Supervisor
from ubo_sentinel.agents.traversal import TraversalAgent
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.review import unscreened
from ubo_sentinel.graph.store import evidence_info
from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.rules.evaluator import compare

pytestmark = pytest.mark.integration

AGENTS = [agent.name for agent in PIPELINE]


@pytest.fixture
def log():
    return AuditLog()


def screen_all(ontology, gold, snapshot, con, log):
    supervisor = Supervisor(ontology, log)
    return {scenario.id: screen_gold(supervisor, scenario, snapshot, con) for scenario in gold}


def test_every_scenario_is_one_recorded_decision(fresh_con, ontology, gold, log):
    con, (t0,) = fresh_con("t0")
    outcomes = screen_all(ontology, gold, t0, con, log)

    for scenario in gold:
        decision = outcomes[scenario.id].decision
        assert compare(decision, scenario).diffs == [], scenario.id
        memo = Memo.model_validate(decision.memo_json)
        assert memo.uncited() == [] and decision.guardrail_warnings == [], scenario.id
        expected = [path.to_evidence_path() for path in scenario.expected_paths]
        assert all(path in memo.paths for path in expected), scenario.id

    events = log.records()
    assert [event["event_type"] for event in events] == ["RECOMMENDED"] * len(gold)
    assert len({event["decision_id"] for event in events}) == len(gold) == 28
    report = log.verify(ontology)
    assert (report.intact, report.count) == (True, 28)


def test_each_event_carries_what_replay_needs(fresh_con, ontology, gold, log):
    con, (t0,) = fresh_con("t0")
    outcomes = screen_all(ontology, gold, t0, con, log)
    registry = evidence_info(con, t0)
    by_id = {outcome.decision.decision_id: scenario for scenario, outcome in outcomes.items()}

    for event in log.records():
        payload = event["payload"]
        decision = Decision.model_validate(payload["decision"])
        assert payload["payload_sha256"] == sha256_hex(decision.canonical_payload())
        assert payload["evidence"] == registry
        assert payload["rule_pack"] == PACK.model_dump(mode="json")
        assert payload["request"]["query"] and payload["resolution"]["method"]
        # The Audit Agent writes the event while it runs, so the trace stops before it.
        agents = [step["agent"] for step in payload["trace"]]
        if by_id[event["decision_id"]] == "SCEN-23":
            assert agents == ["Intake", "Explainer", "Guardrail"]
        else:
            assert agents == AGENTS[:-1]
        # The copy under var/decisions is the decision the log holds.
        copy = log.decisions_dir / f"{event['decision_id']}.json"
        assert Decision.model_validate(json.loads(copy.read_text(encoding="utf-8"))) == decision


def test_screening_everything_again_writes_nothing(fresh_con, ontology, gold, log):
    con, (t0,) = fresh_con("t0")
    first = screen_all(ontology, gold, t0, con, log)
    before = log.path.read_bytes()
    # By the alias this time: the set it resolves to is what names a decision.
    again = screen_all(ontology, gold, "fixtures", con, log)
    assert all(not outcome.is_new for outcome in again.values())
    assert {k: o.payload_sha256 for k, o in again.items()} == {
        k: o.payload_sha256 for k, o in first.items()
    }
    assert log.path.read_bytes() == before


# --- failing closed, agent by agent ------------------------------------------------------

BREAKABLE = [
    IntakeAgent,
    TraversalAgent,
    SanctionsMatchAgent,
    RuleEngineAgent,
    ExplainerAgent,
    GuardrailAgent,
]


def broken(agent):
    class Broken(agent):
        def run(self, state, ctx):
            raise RuntimeError("disk on fire")

    return tuple(Broken if item is agent else item for item in PIPELINE)


@pytest.mark.parametrize("agent", BREAKABLE, ids=lambda agent: agent.name)
@pytest.mark.parametrize("scenario_id", ["SCEN-01", "SCEN-03"])
def test_an_agent_that_raises_never_gives_a_decision(
    fresh_con, ontology, gold, log, agent, scenario_id
):
    """SCEN-01 is CLEAR and SCEN-03 ESCALATE: neither is cleared by a failure, nor cached."""
    con, (t0,) = fresh_con("t0")
    scenario = next(item for item in gold if item.id == scenario_id)
    failed = screen_gold(Supervisor(ontology, log, agents=broken(agent)), scenario, t0, con)
    assert isinstance(failed, ScreenFailed)
    assert (failed.agent, failed.recommendation, failed.error_type) == (
        agent.name,
        "REVIEW",
        "RuntimeError",
    )
    assert [event["event_type"] for event in log.records()] == ["SCREEN_FAILED"]
    assert log.decisions() == []
    assert len(unscreened(log)) == 1

    done = screen_gold(Supervisor(ontology, log), scenario, t0, con)
    assert isinstance(done, Recommended) and done.is_new
    assert done.decision.recommendation == scenario.expected_recommendation
    # Screened again, the entity is no longer one nobody screened, wherever it failed.
    assert unscreened(log) == []


def test_an_audit_agent_that_raises_records_nothing(fresh_con, ontology, gold, log):
    from ubo_sentinel.agents.audit import AuditAgent

    con, (t0,) = fresh_con("t0")
    scenario = next(item for item in gold if item.id == "SCEN-01")
    failed = screen_gold(Supervisor(ontology, log, agents=broken(AuditAgent)), scenario, t0, con)
    assert isinstance(failed, ScreenFailed) and failed.agent == "Audit"
    assert [event["event_type"] for event in log.records()] == ["SCREEN_FAILED"]


# --- the changed list --------------------------------------------------------------------


def test_the_changed_list_through_the_whole_pipeline(fresh_con, ontology, gold, log):
    con, (t0, t1) = fresh_con("t0_t1")
    before = screen_all(ontology, gold, t0, con, log)
    after = screen_all(ontology, gold, t1, con, log)

    changed = {}
    for scenario in gold:
        old, new = before[scenario.id].decision, after[scenario.id].decision
        want = scenario.expected_recommendation_t1 or scenario.expected_recommendation
        assert new.recommendation == want, scenario.id
        assert (old.snapshot_set_id, new.snapshot_set_id) == (t0, t1)
        assert old.decision_id != new.decision_id
        if old.recommendation != new.recommendation:
            changed[scenario.id] = (old.recommendation, new.recommendation)
    assert changed == {
        "SCEN-01": ("CLEAR", "ESCALATE"),
        "SCEN-08": ("REVIEW", "CLEAR"),
        "SCEN-17": ("CLEAR", "ESCALATE"),
    }
    report = log.verify(ontology)
    assert (report.intact, report.count) == (True, 56)
    # One entity, two sets: the log links them.
    target = "lei:FXS01TARGET000000000"
    assert [d.snapshot_set_id for d in log.history(target)] == [t0, t1]
