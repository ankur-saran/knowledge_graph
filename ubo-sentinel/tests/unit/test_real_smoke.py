"""The real-smoke suite: invariants on any refresh; a change on the verified set fails."""

import json

import pytest
import yaml
from e2e import cli
from real_format import SAMPLES, lei

from ubo_sentinel.agents.real_smoke import SmokeEntry, load_smoke, run_smoke
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.ontology import load_ontology


@pytest.fixture
def real_set():
    result = cli("refresh", "--path", SAMPLES)
    assert result.exit_code == 0, result.output
    with GraphStore.open("real") as store:
        return store.set_id


def entries(set_id, **overrides):
    """The sample world's entries, as `eval/real_smoke.yaml` writes them for real LEIs."""
    rows = [
        {
            "id": "RS-01",
            "lei": lei("ORV"),
            "rationale": "OFAC lists it by its LEI",
            "expect": {"recommendation_not": ["CLEAR"], "reasons_include": ["DIRECT_DESIGNATION"]},
            "verified_on_set": set_id,
            "verified_recommendation": "ESCALATE",
        },
        {
            "id": "RS-02",
            "lei": lei("HALV"),
            "rationale": "60 % owned by a company the EU alone designates",
            "expect": {
                "recommendation_in": ["REVIEW", "ESCALATE"],
                "reasons_exclude": ["DIRECT_DESIGNATION"],
            },
            "verified_on_set": set_id,
            "verified_recommendation": "REVIEW",
        },
        {
            "id": "RS-03",
            "query": "Orvasko Bulk Logistics LLC",
            "resolves_to": f"lei:{lei('ORV')}",
            "rationale": "its legal name finds it",
        },
    ]
    for row in rows:
        row.update(overrides.get(row["id"], {}))
    return rows


def write(tmp_path, rows):
    path = tmp_path / "real_smoke.yaml"
    path.write_text(yaml.safe_dump({"entries": rows}, sort_keys=False), encoding="utf-8")
    return path


def run(set_id, rows, tmp_path):
    parsed = [SmokeEntry.model_validate(row) for row in rows]
    supervisor = Supervisor(load_ontology(ONTOLOGY_PATH), AuditLog(tmp_path / "log"))
    with GraphStore.open(set_id) as store:
        return run_smoke(supervisor, parsed, store)


def test_the_invariants_hold_on_the_verified_set(real_set, tmp_path):
    reports = run(real_set, entries(real_set), tmp_path)
    assert [r.passed for r in reports] == [True, True, True]
    assert [r.recommendation for r in reports[:2]] == ["ESCALATE", "REVIEW"]
    assert reports[2].resolved_to == f"lei:{lei('ORV')}"
    assert not any(r.drift for r in reports)


def test_a_change_on_the_verified_set_fails_and_elsewhere_is_drift(real_set, tmp_path):
    changed = {"RS-02": {"verified_recommendation": "ESCALATE"}}
    (_, failed, _) = run(real_set, entries(real_set, **changed), tmp_path)
    assert failed.failures == ["REVIEW on its verified set, where it was ESCALATE"]

    elsewhere = {"RS-02": {"verified_recommendation": "ESCALATE", "verified_on_set": "0" * 16}}
    (_, drifted, _) = run(real_set, entries(real_set, **elsewhere), tmp_path)
    assert drifted.passed and drifted.drift == f"ESCALATE on {'0' * 16}, REVIEW on {real_set}"


def test_a_broken_invariant_fails(real_set, tmp_path):
    broken = {
        "RS-01": {
            "expect": {
                "recommendation_in": ["CLEAR"],
                "recommendation_not": ["ESCALATE"],
                "reasons_include": ["DERIVED_50PCT"],
                "reasons_exclude": ["DIRECT_DESIGNATION"],
            }
        },
        "RS-03": {"resolves_to": "lei:FXRNOPE0000000000000"},
    }
    first, _, third = run(real_set, entries(real_set, **broken), tmp_path)
    assert first.failures == [
        "ESCALATE is not one of ['CLEAR']",
        "ESCALATE must not be",
        "reason DERIVED_50PCT missing",
        "reason DIRECT_DESIGNATION present",
    ]
    assert third.failures == [
        f"'Orvasko Bulk Logistics LLC' resolved to lei:{lei('ORV')}, not lei:FXRNOPE0000000000000"
    ]


def test_an_lei_not_in_the_graph_fails(real_set, tmp_path):
    missing = {"RS-01": {"lei": "FXRNOPE0000000000000"}}
    (first, _, _) = run(real_set, entries(real_set, **missing), tmp_path)
    assert first.failures[0].startswith("not screened: No entity")


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"id": "RS-09", "rationale": "x"}, "screens an LEI or resolves a query"),
        ({"id": "RS-09", "rationale": "x", "query": "a"}, "needs resolves_to"),
        (
            {"id": "RS-09", "rationale": "x", "lei": lei("ORV"), "verified_on_set": "a"},
            "go together",
        ),
        (
            {
                "id": "RS-09",
                "rationale": "x",
                "query": "a",
                "resolves_to": "b",
                "verified_on_set": "a",
                "verified_recommendation": "CLEAR",
            },
            "needs an lei",
        ),
        ({"id": "R-1", "rationale": "x", "lei": lei("ORV")}, "should match pattern"),
    ],
)
def test_an_entry_must_be_complete(row, message):
    with pytest.raises(ValueError, match=message):
        SmokeEntry.model_validate(row)


def test_a_file_must_be_readable(tmp_path):
    path = write(tmp_path, entries("x") + entries("x")[:1])
    with pytest.raises(ValueError, match="entry ids repeat"):
        load_smoke(path)
    with pytest.raises(ValueError, match="Cannot read"):
        load_smoke(tmp_path / "none.yaml")


def test_the_suite_in_ubo_eval(real_set, tmp_path):
    path = write(tmp_path, entries(real_set))
    result = cli("eval", "--suite", "real-smoke", "--snapshot", "real", "--smoke", path)
    assert result.exit_code == 0, result.output
    assert "RS-01  PASS  ESCALATE" in result.output
    assert "Real smoke invariants" in result.output

    drifting = entries(real_set, **{"RS-02": {"verified_recommendation": "CLEAR"}})
    drifting[1]["verified_on_set"] = "0" * 16
    shown = cli(
        "eval", "--suite", "real-smoke", "--snapshot", "real", "--smoke", write(tmp_path, drifting)
    )
    assert shown.exit_code == 0 and "DRIFT (not a failure)" in shown.output

    failing = entries(real_set, **{"RS-02": {"verified_recommendation": "CLEAR"}})
    as_json = cli(
        "eval",
        "--suite",
        "real-smoke",
        "--snapshot",
        "real",
        "--smoke",
        write(tmp_path, failing),
        "--json",
    )
    assert as_json.exit_code == 1
    document = json.loads(as_json.output)
    assert document["problems"] == [
        "real-smoke: RS-02 REVIEW on its verified set, where it was CLEAR"
    ]


def test_which_suites_run_on_which_family(real_set, tmp_path):
    for command in (["ingest", "--source", "fixtures"], ["normalize", "--snapshot", "fixtures"]):
        assert cli(*command).exit_code == 0
    assert cli("build-graph", "--snapshot", "fixtures").exit_code == 0
    path = write(tmp_path, entries(real_set))
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "query_name,lei,jurisdiction,expected_entity_id,variant_type\n"
        f"Orvasko Bulk Logistics LLC,,,lei:{lei('ORV')},exact\n",
        encoding="utf-8",
    )
    both = cli("eval", "--snapshot", "real", "--smoke", path, "--labels", labels)
    assert both.exit_code == 0, both.output
    assert "Real smoke" in both.output and "Entity resolution" in both.output
    assert "Gold scenarios" not in both.output

    for suite in ("rules", "pipeline"):
        refused = cli("eval", "--suite", suite, "--snapshot", "real")
        assert refused.exit_code == 2 and "family 'real'" in refused.output
    refused = cli("eval", "--suite", "real-smoke", "--snapshot", "fixtures")
    assert refused.exit_code == 2 and "family 'fixtures'" in refused.output
    missing = cli("eval", "--suite", "real-smoke", "--snapshot", "real", "--smoke", tmp_path / "x")
    assert missing.exit_code == 2 and "No real-smoke entries" in missing.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("entries: [{id: RS-01}]\n", encoding="utf-8")
    unreadable = cli("eval", "--suite", "real-smoke", "--snapshot", "real", "--smoke", bad)
    assert unreadable.exit_code == 2 and "Cannot read" in unreadable.output
