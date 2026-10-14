"""Same inputs, same decision: in two databases, in other processes, after a rebuild."""

import hashlib
import json

import duckdb
import pytest
from conftest import PACK, build_graph
from e2e import cli, run_python, screen_gold

from ubo_sentinel.agents.evaluator import compare_golden, golden_of, run_pipeline_suite
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli.eval_cmd import GOLDEN_PATH
from ubo_sentinel.graph.store import GraphStore, evidence_info
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.pipeline import gold as gold_layer

pytestmark = pytest.mark.integration


def fingerprint(con, ontology, gold, snapshot):
    """Every scenario's decision id and canonical payload, as one hash. Records nothing."""
    supervisor = Supervisor(ontology)
    digest = hashlib.sha256()
    for scenario in gold:
        decision = screen_gold(supervisor, scenario, snapshot, con).decision
        digest.update(decision.decision_id.encode())
        digest.update(canonical_json(decision.canonical_payload()))
    return digest.hexdigest()


def built(path, ontology):
    con = duckdb.connect(str(path))
    set_id = build_graph(con, ontology)
    return con, set_id


def test_two_databases_built_apart_give_the_same_bytes(tmp_path, ontology, gold):
    one, one_set = built(tmp_path / "one.duckdb", ontology)
    two, two_set = built(tmp_path / "two.duckdb", ontology)
    try:
        assert one_set == two_set
        assert evidence_info(one, one_set) == evidence_info(two, two_set)
        assert (
            gold_layer.load_build(one, one_set)["gold_digest"]
            == (gold_layer.load_build(two, two_set)["gold_digest"])
        )
        assert fingerprint(one, ontology, gold, one_set) == fingerprint(
            two, ontology, gold, two_set
        )
    finally:
        one.close()
        two.close()


_CHILD = """
import hashlib
from pathlib import Path
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.models import load_gold_scenarios, load_ontology
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.transformer import build_silver
import sys; sys.path.insert(0, "tests")
from e2e import screen_gold

ontology = load_ontology(Path("ontology/ontology.yaml"))
con = connect()
snapshot_set = ingest_fixtures(con, None).snapshot_set
build_silver(con, snapshot_set, ontology)
build_gold(con, snapshot_set)
digest = hashlib.sha256()
supervisor = Supervisor(ontology)
for scenario in load_gold_scenarios(Path("eval/gold_scenarios.yaml")):
    decision = screen_gold(supervisor, scenario, snapshot_set.snapshot_set_id, con).decision
    digest.update(decision.decision_id.encode())
    digest.update(canonical_json(decision.canonical_payload()))
print(snapshot_set.snapshot_set_id, digest.hexdigest())
"""


@pytest.mark.slow
@pytest.mark.parametrize("seed", ["1", "987654"])
def test_another_process_with_another_hash_seed_gives_the_same_bytes(
    tmp_path, ontology, gold, seed
):
    """A replay in one process shares its dict and set order with the screen it checks."""
    con, set_id = built(tmp_path / "here.duckdb", ontology)
    try:
        here = f"{set_id} {fingerprint(con, ontology, gold, set_id)}"
    finally:
        con.close()
    there = run_python(
        _CHILD,
        UBO_DB=tmp_path / "there" / "ubo.duckdb",
        UBO_VAR=tmp_path / "there" / "var",
        PYTHONHASHSEED=seed,
    )
    assert there.strip() == here


def test_a_rebuild_gives_the_same_graph_and_the_same_decisions(fresh_db, ontology, gold):
    fresh_db("t0")
    with GraphStore.open("fixtures") as store:
        before = (store.evidence_info(), store.gold_digest)
    log = AuditLog()
    supervisor = Supervisor(ontology, log)
    for scenario in gold:
        screen_gold(supervisor, scenario, "fixtures")

    assert cli("normalize", "--rebuild").exit_code == 0
    assert cli("build-graph").exit_code == 0
    with GraphStore.open("fixtures") as store:
        assert (store.evidence_info(), store.gold_digest) == before
    # The same inputs: every decision is the one on record, and nothing is written.
    size = log.path.stat().st_size
    assert not any(screen_gold(supervisor, s, "fixtures").is_new for s in gold)
    assert log.path.stat().st_size == size


def test_the_decisions_are_the_golden_ones(fresh_con, ontology, gold):
    """A change to what a screen produces that raised no version is caught across commits.

    `ubo eval --suite pipeline --update-golden` writes the file again; a reviewer
    of that change sees which payloads moved.
    """
    con, (t0,) = fresh_con("t0")
    reports = run_pipeline_suite(Supervisor(ontology), gold, t0, con)
    current = golden_of(reports, t0, PACK.rule_pack_hash)
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert compare_golden(golden, current) == []


def test_a_payload_that_moved_under_its_id_is_named():
    golden = {
        "snapshot_set_id": "s",
        "rule_pack_hash": "p",
        "decisions": {
            "SCEN-01": {"decision_id": "a", "payload_sha256": "1"},
            "SCEN-02": {"decision_id": "b", "payload_sha256": "2"},
        },
    }
    current = json.loads(json.dumps(golden))
    assert compare_golden(golden, current) == []
    current["decisions"]["SCEN-01"]["payload_sha256"] = "changed"
    current["decisions"]["SCEN-02"]["decision_id"] = "moved"
    problems = compare_golden(golden, current)
    assert "SCEN-01" in problems[0] and "PIPELINE_VERSION" in problems[0]
    assert "SCEN-02" in problems[1] and "a version moved" in problems[1]
    other_pack = compare_golden(golden, current | {"rule_pack_hash": "q"})
    assert "the fixtures or the rule pack" in other_pack[0]
