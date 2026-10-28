"""The change monitor through the commands: list and graph changes, the queue, the re-screens."""

import json

import pytest
from e2e import cli, screen_gold, start_cli

from ubo_sentinel.agents import change_monitor as cm
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.review import review_decision
from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.db import connect

pytestmark = pytest.mark.integration


def screen_all(gold, ontology, set_id):
    """Every gold scenario screened on one set, as an analyst would, into the test's log."""
    supervisor = Supervisor(ontology, AuditLog())
    con = connect(read_only=True)
    decisions = {
        s.id: screen_gold(supervisor, s, set_id, con, actor="a.analyst").decision for s in gold
    }
    con.close()
    return decisions


def monitor_json(*args):
    result = cli("monitor", "--json", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def expected_on(scenario, set_name):
    """The outcome a scenario expects on t0, t1 (the list) or t2 (the graph)."""
    t1 = scenario.expected_recommendation_t1 or scenario.expected_recommendation
    return {"t0": scenario.expected_recommendation, "t1": t1}.get(
        set_name, scenario.expected_recommendation_t2 or t1
    )


@pytest.mark.parametrize(
    ("template", "screened_on", "queued", "causes"),
    [
        ("t0_t1", 0, {"SCEN-01", "SCEN-08", "SCEN-17"}, {"LIST"}),
        ("t0_t1_t2", 1, {"SCEN-10", "SCEN-14", "SCEN-16"}, {"OWNERSHIP", "LINK", "ENTITY"}),
    ],
)
def test_the_queue_is_exactly_what_changed_and_each_rescreen_is_as_expected(
    fresh_db, ontology, gold, template, screened_on, queued, causes
):
    set_ids = fresh_db(template)
    prior = screen_all(gold, ontology, set_ids[screened_on])
    by_target = {s.target_id: s for s in gold}
    by_decision = {d.decision_id: scenario for scenario, d in prior.items()}

    first = monitor_json()
    run = first["run"]
    assert first["recorded"] and run["curr_set_id"] == set_ids[-1]
    assert {by_decision[i["prior_decision_id"]] for i in run["items"]} == queued
    assert {cause for i in run["items"] for cause in i["causes"]} == causes
    assert all(i["status"] == "PENDING" for i in run["items"])

    events = len(AuditLog().records())
    result = cli("monitor", "--autorun", "--role", "analyst")
    assert result.exit_code == 0, result.output
    assert "Run already on record" in result.output
    log = AuditLog()
    rescreens = cm.supersedes_of(log)
    assert len(log.records()) == events + len(queued)
    set_name = ["t0", "t1", "t2"][len(set_ids) - 1]
    for decision_id, prior_id in rescreens.items():
        decision = log.decision(decision_id)
        scenario = by_target[decision.target_id]
        assert decision.snapshot_set_id == set_ids[-1]
        assert decision.recommendation == expected_on(scenario, set_name), scenario.id
        assert prior[scenario.id].decision_id == prior_id
        assert log.events_of(decision_id)[0]["actor"] == cm.MONITOR_ACTOR

    # Again: the same run, every item done, nothing appended.
    again = cli("monitor", "--autorun", "--role", "analyst")
    assert again.exit_code == 0 and len(AuditLog().records()) == events + len(queued)
    assert "already on record" in again.output and "skipped: DONE" in again.output
    assert AuditLog().verify(ontology).intact

    # The review queue says which decision each re-screen follows.
    listed = cli("review", "--role", "reviewer")
    assert listed.output.count("[re-screen of ") == len(queued)


@pytest.mark.parametrize(("template", "screened_on"), [("t0_t1", 0), ("t0_t1_t2", 1)])
def test_no_decision_that_changes_is_missed(fresh_db, ontology, gold, template, screened_on):
    """The oracle: every decision screened again on the newer set; each that changes
    must be on the queue (G6). Here each queued decision changes too."""
    set_ids = fresh_db(template)
    screen_all(gold, ontology, set_ids[screened_on])
    con = connect(read_only=True)
    evaluation = cm.evaluate_monitor(
        AuditLog(), Supervisor(ontology), con, resolve_set(con, set_ids[-1])
    )
    con.close()
    assert evaluation.changed and evaluation.missed == []
    assert (evaluation.recall, evaluation.precision) == (1.0, 1.0)
    result = cli("eval", "--suite", "monitor", "--snapshot", set_ids[screened_on], "--json")
    assert result.exit_code == 0, result.output
    metrics = {m["name"]: m for m in json.loads(result.output)["metrics"]}
    assert metrics["Re-screen recall"]["value"] == 1.0 and metrics["Re-screen recall"]["met"]


def test_an_approved_clear_is_queued_first(fresh_db, ontology, gold):
    t0, _ = fresh_db("t0_t1")
    prior = screen_all(gold, ontology, t0)
    log = AuditLog()
    review_decision(
        log,
        ontology,
        prior["SCEN-17"].decision_id,
        action="approve",
        actor="r.reviewer",
        role="reviewer",
        now=Supervisor(ontology).now,
    )
    items = monitor_json("--dry-run")["run"]["items"]
    assert items[0]["prior_decision_id"] == prior["SCEN-17"].decision_id
    assert (items[0]["prior_status"], items[0]["prior_outcome"], items[0]["priority"]) == (
        "APPROVED",
        "CLEAR",
        0,
    )
    # The older decision is still refused a clear: newer data exists (9.0).
    stale = cli(
        "review",
        prior["SCEN-01"].decision_id,
        "--role",
        "reviewer",
        "--actor",
        "r.reviewer",
        "--action",
        "approve",
    )
    assert stale.exit_code == 2 and "newer data exists" in stale.output


def test_two_autoruns_at_once_make_one_decision_per_item(fresh_db, ontology, gold):
    t0, t1 = fresh_db("t0_t1")
    screen_all(gold, ontology, t0)
    assert cli("monitor").exit_code == 0
    children = [start_cli("monitor", "--autorun", "--role", "analyst") for _ in range(2)]
    for child in children:
        output, _ = child.communicate(timeout=300)
        assert child.returncode == 0, output
    log = AuditLog()
    assert log.verify(ontology).intact
    on_t1 = [d for d in log.decisions() if d.snapshot_set_id == t1]
    assert len(on_t1) == 3 == len({d.target_id for d in on_t1})


def test_a_set_that_is_not_built_is_named_with_the_command_to_run(fresh_db):
    fresh_db("t0_t1")
    con = connect()
    t1 = resolve_set(con, "latest").snapshot_set_id
    con.execute("DELETE FROM gold_builds WHERE snapshot_set_id = ?", [t1])
    con.close()
    result = cli("monitor")
    assert result.exit_code == 1 and f"ubo build-graph --snapshot {t1}" in result.output
