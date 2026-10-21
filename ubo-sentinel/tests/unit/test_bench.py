"""`ubo bench`: screens by LEI on a built set, timed, split by agent."""

import json

import pytest
from e2e import cli
from real_format import SAMPLES, lei

from ubo_sentinel.agents.bench import (
    BenchInputError,
    BenchReport,
    Screen,
    peak_rss_bytes,
    percentile,
    read_leis,
    run_bench,
)
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog

LEIS = [lei("ORV"), lei("HALV"), lei("BRANCH")]


@pytest.fixture
def real_set():
    result = cli("refresh", "--path", SAMPLES)
    assert result.exit_code == 0, result.output


@pytest.fixture
def lei_file(tmp_path):
    path = tmp_path / "leis.txt"
    path.write_text("# the sample world\n" + "\n".join(LEIS) + "\n", encoding="utf-8")
    return path


def test_every_lei_is_screened_each_round(real_set, con, ontology, tmp_path):
    report = run_bench(
        Supervisor(ontology, AuditLog(tmp_path / "log")), LEIS, "real", con, "set", rounds=2
    )
    assert [(s.lei, s.round) for s in report.screens] == [(x, r) for r in (1, 2) for x in LEIS]
    assert [s.outcome for s in report.screens[:3]] == ["ESCALATE", "REVIEW", "CLEAR"]
    assert not report.errors and report.within_budget
    first = report.screens[0]
    assert {"Intake", "RuleEngine"} <= set(first.agents) and first.rows > 0 and first.paths >= 1
    summary = report.summary()
    assert summary["screens"] == 6 and summary["cold_seconds"] == round(first.seconds, 4)
    assert summary["warm_seconds"]["max"] <= summary["seconds"]["max"]
    assert set(summary["agents"]) == set(first.agents)


def test_an_lei_not_in_the_graph_is_an_error(real_set, con, ontology, tmp_path):
    missing = "FXRNOPE0000000000000"
    report = run_bench(Supervisor(ontology, AuditLog(tmp_path)), [missing], "real", con, "set")
    (screen,) = report.errors
    assert screen.outcome == "NOT_IN_GRAPH" and "No entity" in screen.detail


def test_the_budget():
    screens = [Screen("A", 1, seconds, "CLEAR") for seconds in (1.0, 2.0, 20.0)]
    assert not BenchReport("set", screens, 15.0, None).within_budget
    assert BenchReport("set", screens, 30.0, None).within_budget
    assert BenchReport("set", screens, 15.0, None).summary()["peak_rss_mb"] is None
    assert percentile([], 0.95) == 0.0


def test_peak_memory_is_read():
    assert peak_rss_bytes() > 10 * 2**20


def test_lei_files(tmp_path):
    text = tmp_path / "a.txt"
    text.write_text(f"{LEIS[0].lower()}  # a comment\n\n{LEIS[0]}\n{LEIS[1]}\n", encoding="utf-8")
    assert read_leis(text) == LEIS[:2]
    entries = tmp_path / "a.yaml"
    entries.write_text(
        f"entries:\n  - id: RS-01\n    lei: {LEIS[0]}\n  - id: RS-02\n    query: x\n",
        encoding="utf-8",
    )
    assert read_leis(entries) == LEIS[:1]
    plain = tmp_path / "b.yml"
    plain.write_text(f"- lei: {LEIS[2]}\n", encoding="utf-8")
    assert read_leis(plain) == LEIS[2:]
    for name, content in (("empty.txt", "# none\n"), ("bad.yaml", "a: [")):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        with pytest.raises(BenchInputError):
            read_leis(path)
    (tmp_path / "scalar.yaml").write_text("entries: 3\n", encoding="utf-8")
    with pytest.raises(BenchInputError, match="a list of entries"):
        read_leis(tmp_path / "scalar.yaml")
    with pytest.raises(BenchInputError, match="Cannot read"):
        read_leis(tmp_path / "none.txt")


def test_the_command(real_set, lei_file):
    result = cli("bench", "--lei-file", lei_file, "--rounds", "2")
    assert result.exit_code == 0, result.output
    assert "Cold (first)" in result.output and "within the 15s budget" in result.output
    assert "Intake" in result.output

    as_json = cli("bench", "--lei-file", lei_file, "--json")
    summary = json.loads(as_json.output)
    assert summary["screens"] == 3 and summary["errors_detail"] == []

    over = cli("bench", "--lei-file", lei_file, "--budget", "0")
    assert over.exit_code == 1 and "ABOVE the 0s budget" in over.output


def test_the_command_records_nothing_unless_asked(real_set, lei_file):
    log = AuditLog()
    assert cli("bench", "--lei-file", lei_file).exit_code == 0
    assert log.verify().count == 0
    assert cli("bench", "--lei-file", lei_file, "--record").exit_code == 0
    assert log.verify().count == len(LEIS)


def test_the_command_fails_on_errors_and_bad_input(real_set, tmp_path):
    missing = tmp_path / "missing.txt"
    missing.write_text("FXRNOPE0000000000000\n", encoding="utf-8")
    result = cli("bench", "--lei-file", missing)
    assert result.exit_code == 1 and "did not end in a recommendation" in result.output
    assert cli("bench", "--lei-file", tmp_path / "none.txt").exit_code == 2
    assert cli("bench", "--lei-file", missing, "--rule-pack", "nope").exit_code == 2
    assert cli("bench", "--lei-file", missing, "--snapshot", "fixtures").exit_code == 2
