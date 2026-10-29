"""The illustrative UK and EU packs, the pack label, and the pack scenarios (Step 13.3)."""

import json
from pathlib import Path

import pytest
import yaml
from e2e import cli

from ubo_sentinel.agents.explainer import _pack_ref, build_memo, render_memo
from ubo_sentinel.rules import pack_scenarios
from ubo_sentinel.rules.pack import RulePack, load_rule_pack, pack_path
from ubo_sentinel.rules.pack_scenarios import (
    PackScenario,
    load_pack_scenarios,
    run_pack_suite,
    shipped_packs,
)

GOLDEN = Path("eval/golden_decisions.json")


def _scenario(**fields):
    base = {
        "id": "PACK-90",
        "title": "t",
        "target": "T",
        "edges": [{"upper": "A", "lower": "T", "pct": 60}],
        "designations": [{"node": "A"}],
        "expected": {"ofac": {"recommendation": "ESCALATE", "blocked": ["A", "T"]}},
        "rationale": "r",
    }
    return PackScenario.model_validate(base | fields)


def test_the_shipped_packs_load_and_only_the_illustrative_ones_are_labelled():
    packs = shipped_packs()
    assert set(packs) == {"ofac", "uk_ofsi", "eu"}
    assert packs["ofac"].label is None
    for stem in ("uk_ofsi", "eu"):
        assert "not validated by counsel" in packs[stem].label
        assert pack_path(stem).read_text(encoding="utf-8").startswith("# illustrative")


def test_the_uk_and_eu_tests_need_more_than_half():
    packs = shipped_packs()
    assert packs["ofac"].threshold_operator == ">="
    assert packs["uk_ofsi"].threshold_operator == packs["eu"].threshold_operator == ">"
    assert packs["uk_ofsi"].aggregate_blocked_owners is False
    assert packs["uk_ofsi"].designation_lists == ["FX-UK-LIST", "gb_fcdo_sanctions"]
    assert packs["eu"].designation_lists == ["FX-EU-LIST", "eu_fsf"]


def test_a_pack_without_a_label_hashes_as_before_the_key_existed():
    ofac = load_rule_pack(pack_path("ofac"))
    assert "label" not in ofac.model_dump()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert ofac.rule_pack_hash == golden["rule_pack_hash"]
    labelled = RulePack.model_validate(ofac.model_dump() | {"label": "x"})
    assert labelled.rule_pack_hash != ofac.rule_pack_hash
    assert RulePack.model_validate(labelled.model_dump(mode="json")) == labelled


def test_the_memo_states_a_label_and_holds_none_when_the_pack_has_none():
    ofac, eu = load_rule_pack(pack_path("ofac")), load_rule_pack(pack_path("eu"))
    assert "label" not in _pack_ref(ofac).model_dump(mode="json")
    memo = build_memo(pack=eu, max_depth=5, datasets=[], query="nothing")
    assert memo.model_dump(mode="json")["rule_pack"]["label"] == eu.label
    assert f"> {eu.label}" in render_memo(memo)
    assert eu.label not in render_memo(memo.model_copy(update={"rule_pack": _pack_ref(ofac)}))


def test_every_pack_concludes_what_every_scenario_states():
    report = run_pack_suite(load_pack_scenarios(), shipped_packs())
    assert not report.unstated
    assert [o for o in report.outcomes if not o.passed] == []
    assert report.agreement == 1.0


def test_the_matrix_shows_where_the_regimes_differ():
    outcomes = run_pack_suite(load_pack_scenarios(), shipped_packs()).outcomes
    seen = {(o.scenario_id, o.pack): o.result.recommendation for o in outcomes}
    # Exactly half: OFAC blocks; the UK and EU tests need more.
    assert seen["PACK-01", "ofac"] == "ESCALATE"
    assert seen["PACK-01", "uk_ofsi"] == seen["PACK-01", "eu"] == "REVIEW"
    # 30 % + 30 %: added together by OFAC and the EU pack, not by the UK pack.
    assert seen["PACK-03", "uk_ofsi"] == "REVIEW"
    assert seen["PACK-03", "eu"] == seen["PACK-03", "ofac"] == "ESCALATE"
    # Control alone is never cleared, under any pack.
    assert {seen["PACK-06", stem] for stem in ("ofac", "uk_ofsi", "eu")} == {"REVIEW"}


def test_a_designation_on_the_pack_s_own_list_blocks_and_another_does_not():
    packs = shipped_packs()
    own, other = _scenario(), _scenario(designations=[{"node": "A", "list": "other"}])
    for pack in packs.values():
        assert own.matches(pack)[0].sanction_records[0].list_source in pack.designation_lists
        assert other.matches(pack)[0].sanction_records[0].list_source not in pack.designation_lists


def test_a_pack_a_scenario_does_not_state_fails_the_suite():
    scenario = _scenario(expected={"ofac": {"recommendation": "ESCALATE", "blocked": ["A", "T"]}})
    report = run_pack_suite([scenario], shipped_packs())
    assert report.unstated == {"PACK-90": ["eu", "uk_ofsi"]}
    assert report.agreement < 1.0


def test_a_scenario_that_states_an_unknown_pack_is_refused():
    scenario = _scenario(expected={"nope": {"recommendation": "CLEAR"}})
    with pytest.raises(ValueError, match="not in rules/"):
        run_pack_suite([scenario], shipped_packs())


def test_a_designation_must_reach_a_node_of_the_graph():
    with pytest.raises(ValueError, match="not in the graph"):
        _scenario(designations=[{"node": "Z"}], expected={"ofac": {"recommendation": "CLEAR"}})


def test_duplicate_scenario_ids_are_refused(tmp_path):
    entry = {
        "id": "PACK-01",
        "title": "t",
        "target": "T",
        "expected": {"ofac": {"recommendation": "CLEAR"}},
        "rationale": "r",
    }
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump([entry, entry]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_pack_scenarios(path)


def test_the_packs_suite_needs_no_database_and_prints_the_matrix():
    result = cli("eval", "--suite", "packs")
    assert result.exit_code == 0, result.output
    assert "PACK-01  REVIEW     ESCALATE   REVIEW" in result.output
    assert "Pack agreement" in result.output


def test_the_packs_suite_reports_json():
    result = cli("eval", "--suite", "packs", "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["ok"] is True
    assert [m["name"] for m in document["metrics"]] == ["Pack agreement"]


def test_a_failing_pack_scenario_fails_the_suite(tmp_path, monkeypatch):
    entry = {
        "id": "PACK-01",
        "title": "t",
        "target": "T",
        "edges": [{"upper": "A", "lower": "T", "pct": 60}],
        "designations": [{"node": "A"}],
        "expected": {stem: {"recommendation": "CLEAR"} for stem in ("ofac", "uk_ofsi", "eu")},
        "rationale": "r",
    }
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump([entry]), encoding="utf-8")
    monkeypatch.setattr("ubo_sentinel.cli.eval_cmd.PACK_SCENARIOS_PATH", path)
    result = cli("eval", "--suite", "packs")
    assert result.exit_code == 1
    assert "FAIL PACK-01 ofac recommendation" in result.output
    # An unreadable file is a usage error.
    path.write_text("- {id: nope}", encoding="utf-8")
    assert cli("eval", "--suite", "packs").exit_code == 2
    assert pack_scenarios.OTHER_LIST not in shipped_packs()["ofac"].designation_lists
