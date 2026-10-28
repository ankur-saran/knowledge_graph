"""The change monitor: which recorded decisions newer data may move, and screening them again."""

from datetime import UTC, datetime

import pytest
from conftest import T0
from e2e import cli, person_pii, screen_gold
from graphs import designated, edge
from hypothesis import given, settings
from hypothesis import strategies as st
from test_memo import memo_of
from test_rules_hypothesis import NODES, PACKS, PERCENTAGES, cases, subgraph_of

from ubo_sentinel.agents import change_monitor as cm
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.review import ReviewError, review_decision
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.change import Change, RescreenItem
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.transformer import build_silver
from ubo_sentinel.rules.engine import RuleEngine

AT = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def now():
    return AT


def changed(con, ontology, tmp_path, base, **edits):
    """A new set of the fixtures family: the t0 files named in `edits`, each changed by a
    function of its text; the other datasets come from `base`. Built up to Gold."""
    folder = tmp_path / f"changed-{len(list(tmp_path.glob('changed-*')))}"
    folder.mkdir()
    for name, edit in edits.items():
        text = (T0 / f"{name}.csv").read_bytes().decode("utf-8")
        (folder / f"{name}.csv").write_bytes(edit(text).encode("utf-8"))
    snapshot_set = ingest_fixtures(con, folder, base=base).snapshot_set
    build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    return snapshot_set.snapshot_set_id


def without(*record_ids):
    """Leave out the rows whose `source_record_id` is one of these."""

    def edit(text):
        return "".join(
            line for line in text.splitlines(keepends=True) if line.split(",")[1] not in record_ids
        )

    return edit


def queue(con, set_id):
    run, diffs, _ = cm.build_queue(AuditLog(), con, resolve_set(con, set_id))
    return run, diffs


@pytest.fixture
def screened(fresh_con, ontology, gold):
    """t0, and a function that screens gold scenarios on it into the test's log."""
    con, (t0,) = fresh_con("t0")
    supervisor = Supervisor(ontology, AuditLog(), now=now)
    by_id = {scenario.id: scenario for scenario in gold}

    def screen(scenario_id, **request):
        outcome = screen_gold(supervisor, by_id[scenario_id], t0, con, actor="a.analyst", **request)
        return outcome.decision

    return con, t0, screen


# --- what is queued ------------------------------------------------------------------


def test_a_decision_is_queued_only_within_its_own_depth(screened, ontology, tmp_path):
    """SCEN-22: the designated owner is six hops up. At depth 5 the screen stopped short
    of it and a change there moves nothing; at depth 7 it is the decision."""
    con, t0, screen = screened
    shallow, deep = screen("SCEN-22"), screen("SCEN-22", max_depth=7)
    later = changed(con, ontology, tmp_path, t0, sanctions=without("s22-des"))
    run, _ = queue(con, later)
    assert [(i.prior_decision_id, i.hops, i.basis) for i in run.items] == [
        (deep.decision_id, 6, "BOTH")
    ]
    assert shallow.decision_id not in {i.prior_decision_id for i in run.items}


def test_a_change_above_a_linked_record_reaches_the_record_screened(screened, ontology, tmp_path):
    """SCEN-27: the registry's record is linked by name to the list's, which a designated
    party owns. The walk down crosses the link below the change, not only at it."""
    con, t0, screen = screened
    decision = screen("SCEN-27")
    with GraphStore.open(t0, con) as store:
        reached = queries.get_affected_by(store, ["fx_list:s27-owner"], 5)
    assert (decision.target_id, 1) in {(a.entity_id, a.hops) for a in reached}
    later = changed(con, ontology, tmp_path, t0, sanctions=without("s27-des"))
    run, _ = queue(con, later)
    [item] = run.items
    assert (item.target_id, item.causes, item.hops) == (decision.target_id, ["LIST"], 1)
    assert item.basis in {"TRAVERSAL", "BOTH"}


def test_a_query_that_found_nothing_is_queued_once_it_finds_something(
    screened, ontology, gold, tmp_path
):
    con, t0, screen = screened
    query = next(s.query for s in gold if s.id == "SCEN-23")
    nothing = screen("SCEN-23")
    assert nothing.target_id is None
    row = (
        "fx_registry,s23-new,2026-10-01,1.0,FXS23NEW000000000000,RA-FX-IE,23-NEW,"
        f"{query},,IE,ACTIVE,ISSUED,LegalEntity\n"
    )
    later = changed(con, ontology, tmp_path, t0, entities=lambda text: text + row)
    run, _ = queue(con, later)
    [item] = run.items
    assert (item.reason, item.basis, item.target_id, item.prior_decision_id) == (
        "NOW_RESOLVES",
        "RESOLVER",
        None,
        nothing.decision_id,
    )
    log = AuditLog()
    [result] = cm.rescreen(
        run,
        Supervisor(ontology, log, now=now),
        con,
        actor=cm.MONITOR_ACTOR,
        role="analyst",
        requests=cm.recorded_requests(log),
    )
    assert result.decision.target_id == "lei:FXS23NEW000000000000"
    assert [i.status for i in cm.refresh_statuses(run, log, con).items] == ["DONE"]


def test_an_entity_gone_from_the_graph_goes_to_a_person_and_is_not_screened(
    screened, ontology, tmp_path
):
    con, t0, screen = screened
    decision = screen("SCEN-01")
    later = changed(con, ontology, tmp_path, t0, entities=without("s01-target"))
    run, _ = queue(con, later)
    [item] = run.items
    assert (item.target_id, item.status, item.priority) == (decision.target_id, "TARGET_GONE", 2)
    log = AuditLog()
    [result] = cm.rescreen(
        run, Supervisor(ontology, log), con, actor="m", role="analyst", requests={}
    )
    assert result.outcome == "SKIPPED"


def test_decisions_on_a_set_that_is_not_built_are_queued_all_the_same(fresh_con, ontology, gold):
    con, (t0, t1) = fresh_con("t0_t1")
    supervisor = Supervisor(ontology, AuditLog())
    for scenario in gold[:2]:
        screen_gold(supervisor, scenario, t0, con)
    con.execute("DELETE FROM gold_builds WHERE snapshot_set_id = ?", [t0])
    run, diffs = queue(con, t1)
    assert diffs == [] and run.baselines == {t0: None}
    assert [(i.reason, i.basis) for i in run.items] == [("BASELINE_MISSING", "BASELINE")] * 2


def test_nothing_changed_queues_nothing(screened):
    con, t0, screen = screened
    screen("SCEN-01")
    run, diffs = queue(con, t0)
    assert run.items == [] and diffs == [] and run.baselines == {}


# --- the record ---------------------------------------------------------------------------


def test_a_run_is_recorded_once_and_its_queue_is_derived(fresh_con, ontology, gold, tmp_path):
    con, (t0, t1) = fresh_con("t0_t1")
    log = AuditLog()
    scenario = next(s for s in gold if s.id == "SCEN-01")
    prior = screen_gold(Supervisor(ontology, log), scenario, t0, con).decision
    run, _ = queue(con, t1)
    record = {"actor": "e.engineer", "role": "engineer", "now": now}
    assert cm.record_run(log, ontology, run, **record)
    assert not cm.record_run(log, ontology, run, **record)
    assert cm.recorded_run(log, run.run_id) == run
    assert [r["decision_id"] for r in cm.runs(log)] == [f"monitor:{run.run_id}"]
    # A run is no decision: not in the decisions, not reviewable, not recorded twice.
    assert [d.decision_id for d in log.decisions()] == [prior.decision_id]
    with pytest.raises(ReviewError):
        review_decision(
            log,
            ontology,
            f"monitor:{run.run_id}",
            action="approve",
            actor="r",
            role="reviewer",
            now=now,
        )
    with pytest.raises(ValueError, match="second record of one run"):
        log.append(
            ontology=ontology,
            decision_id=f"monitor:{run.run_id}",
            event_type="MONITOR_RUN",
            actor="x",
            role="engineer",
            created_at=AT,
            payload={},
        )
    with pytest.raises(ValueError, match="does not name its run"):
        log.append(
            ontology=ontology,
            decision_id="abc",
            event_type="MONITOR_RUN",
            actor="x",
            role="engineer",
            created_at=AT,
            payload={},
        )
    with pytest.raises(PermissionError):
        cm.record_run(log, ontology, run, actor="a", role="auditor", now=now)

    path = cm.write_queue(run, tmp_path / "var")
    assert path.read_text(encoding="utf-8").count("\n") == 1
    assert (tmp_path / "var" / "monitor" / f"{run.run_id}.json").exists()

    # The re-screen is an ordinary decision, by the monitor, linked to the one it follows.
    [result] = cm.rescreen(
        run,
        Supervisor(ontology, log),
        con,
        actor=cm.MONITOR_ACTOR,
        role="analyst",
        requests=cm.recorded_requests(log),
    )
    assert (result.outcome, result.transition) == ("RECORDED", "UPGRADE")
    assert result.decision.status == "RECOMMENDED"
    assert cm.supersedes_of(log) == {result.decision.decision_id: prior.decision_id}
    assert log.verify(ontology).intact
    again, _ = queue(con, t1)
    assert again.run_id == run.run_id and [i.status for i in again.items] == ["DONE"]
    # Anyone but the monitor may review it.
    review_decision(
        log,
        ontology,
        result.decision.decision_id,
        action="approve",
        actor="r.reviewer",
        role="reviewer",
        now=now,
    )


def item(target, prior, priority, status="RECOMMENDED"):
    return RescreenItem(
        prior_decision_id=f"d-{target}",
        target_id=target,
        prior_set_id="s",
        prior_recommendation=prior,
        prior_status=status,
        rule_pack_id="p",
        max_depth=5,
        reason="CHANGED",
        basis="FOOTPRINT",
        priority=priority,
    )


def test_the_gravest_item_comes_first():
    """A CLEAR the change may turn first, an approved one before one still waiting; an
    ESCALATE, which a delisting could only lower, last."""
    items = [
        item("e", "ESCALATE", 3),
        item("r", "REVIEW", 1),
        item("c", "CLEAR", 0),
        item("a", "CLEAR", 0, status="APPROVED"),
        item("g", "CLEAR", 2),
    ]
    assert [i.target_id for i in cm.sort_items(items)] == ["a", "c", "r", "g", "e"]
    assert [
        cm.transition(a, b)
        for a, b in [("CLEAR", "ESCALATE"), ("ESCALATE", "CLEAR"), ("REVIEW", "REVIEW")]
    ] == [
        "UPGRADE",
        "DOWNGRADE",
        "UNCHANGED",
    ]


# --- the command --------------------------------------------------------------------------


def test_the_digest_shows_an_analyst_no_person(screened, ontology, entities, tmp_path):
    """SCEN-03: a designated person's record is removed. The digest names the change by
    the person's pseudonym for an analyst, and by the id for a reviewer."""
    con, t0, screen = screened
    screen("SCEN-03")
    changed(con, ontology, tmp_path, t0, sanctions=without("s03-des-a"))
    con.close()
    analyst = cli("monitor", "--role", "analyst", "--dry-run")
    assert analyst.exit_code == 0, analyst.output
    assert "person:" in analyst.output and "Re-screen queue: 1" in analyst.output
    assert not [value for value in person_pii(entities) if value in analyst.output]
    reviewer = cli("monitor", "--role", "reviewer", "--dry-run", "--json")
    assert reviewer.exit_code == 0 and "fx_list:s03-owner-a" in reviewer.output


def test_a_real_set_the_quality_check_did_not_accept_needs_a_reason(fresh_con):
    con, (t0, t1) = fresh_con("t0_t1")
    con.execute("UPDATE snapshot_set_info SET family = 'real'")
    curr = resolve_set(con, t1)
    with pytest.raises(cm.MonitorError, match="data-quality check"):
        cm.check_curr(con, curr, allow_unaccepted=False, reason=None)
    with pytest.raises(cm.MonitorError, match="reason"):
        cm.check_curr(con, curr, allow_unaccepted=True, reason=" ")
    cm.check_curr(con, curr, allow_unaccepted=True, reason="checked by hand")
    # `real` names only an accepted set: there is none.
    with pytest.raises(UnknownSnapshotSet):
        cm.default_curr(con, "real")
    con.close()
    result = cli("monitor", "--curr", t1, "--dry-run")
    assert result.exit_code == 2 and "--allow-unaccepted" in result.output


def test_the_newest_set_of_the_family_is_compared_by_default(fresh_con):
    con, (t0, t1) = fresh_con("t0_t1")
    # `fixtures` stays on t0 (the newest complete set); the monitor compares with t1.
    assert resolve_set(con, "fixtures").snapshot_set_id == t0
    assert cm.default_curr(con, None).snapshot_set_id == t1
    assert cm.default_curr(con, "fixtures").snapshot_set_id == t1


def test_autorun_needs_a_role_that_may_screen(fresh_db):
    fresh_db("t0_t1")
    result = cli("monitor", "--autorun", "--role", "engineer")
    assert result.exit_code == 1 and "not permitted to run 'screen'" in result.output
    result = cli("monitor", "--role", "auditor")
    assert result.exit_code == 1 and "--dry-run" in result.output


# --- the footprint never misses a decision that changes -------------------------------


def outcome(result):
    return (
        result.recommendation,
        result.reasons,
        result.blocked_set,
        result.possibly_blocked_set,
        sorted((g.code, g.subject_id, g.decision_relevant) for g in result.data_gaps),
    )


@settings(max_examples=300, deadline=None)
@given(cases(), PACKS, st.data())
def test_a_decision_the_change_moves_is_always_in_the_footprint(case, pack, data):
    """Whatever the graph, the pack and the one change, if the rules conclude otherwise
    after it, the change touches what the decision's memo shows the rules read."""
    subgraph, matches = case
    mutated = data.draw(mutation_of(subgraph, matches))
    after_graph, after_matches, change = mutated
    memo, before = memo_of(subgraph, matches, pack)
    after = RuleEngine(pack).evaluate(after_graph, after_matches)
    if outcome(after) != outcome(before):
        named, upward = cm.footprint(memo, subgraph.target_id)
        assert cm.reaching(named, upward, [change]) == [change]


def mutation_of(subgraph, matches):
    @st.composite
    def draw_one(draw):
        edges = list(subgraph.edges)
        kinds = ["add_edge", "add_match"]
        kinds += ["remove_edge", "restake"] if edges else []
        kinds += ["remove_match"] if matches else []
        kind = draw(st.sampled_from(kinds))
        domain, new_matches = "edge", list(matches)
        if kind == "add_edge":
            upper, lower = draw(
                st.tuples(st.sampled_from(NODES), st.sampled_from(NODES)).filter(
                    lambda p: p[0] != p[1]
                )
            )
            rel = draw(st.sampled_from(["OWNS", "CONSOLIDATED_BY", "CONTROLS"]))
            pct = None if rel == "CONTROLS" else draw(st.sampled_from(PERCENTAGES))
            new = edge(upper, lower, pct, rel)
            edges = [e for e in edges if e.id != new.id] + [new]
            anchor = lower
        elif kind in {"remove_edge", "restake"}:
            old = draw(st.sampled_from(edges))
            upper, lower = (
                (old.object_id, old.subject_id)
                if old.rel_type == "CONSOLIDATED_BY"
                else (old.subject_id, old.object_id)
            )
            edges = [e for e in edges if e.id != old.id]
            if kind == "restake":
                pct = None if old.rel_type == "CONTROLS" else draw(st.sampled_from(PERCENTAGES))
                edges.append(edge(upper, lower, pct, old.rel_type, old.is_ultimate))
            anchor = lower
        elif kind == "add_match":
            domain = "designation"
            anchor = draw(st.sampled_from(NODES))
            new_matches.append(designated(anchor))
        else:
            domain = "designation"
            index = draw(st.integers(0, len(matches) - 1))
            anchor = matches[index].entity_id
            del new_matches[index]
        change = Change(
            change_id="c", domain=domain, kind="CHANGED", cause="LIST", key="k", anchor_ids=[anchor]
        )
        return subgraph_of(edges), new_matches, change

    return draw_one()
